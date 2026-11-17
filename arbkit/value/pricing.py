"""Valuation of exact token results in USD and in the chain's native coin.

Exact integer amounts are never replaced. Valuation adds float fields and an explicit `price_source`:
  native        the profit token is the wrapped native coin; priced with the native Binance 1-minute open
  stable        a USD stablecoin from chains/<chain>/chain.toml, valued at 1.00
  binance:SYM   a listed token with its own Binance symbol (cbBTC, ARB, AERO, …)
  leg_implied   converted through the transaction's own swap legs to a priced token, at execution rates
  unpriced      nothing above applies; the amount is reported but excluded from USD totals
net_usd = gross_usd - network fee - builder/validator tip paid inside the tx - order-flow kickbacks to known
recipients (Blink). Forwarded proceeds to unknown recipients stay inside gross (ownership unverified).
Prices are the Binance 1-minute kline OPEN of the block's minute, cached per symbol and UTC day under
data/prices/open/. The open is final the moment the minute starts, so a block classified live, seconds after it was
made, gets the same price as any later re-run; the close (used until 2026-11-01) is still moving while the minute runs
and live rows were priced from a provisional close or a neighbour minute (Base 53365381: ETH $2,271.04 live vs
$2,270.61 re-run). Network access only happens for missing minutes; `offline=True` never fetches.
"""

from __future__ import annotations

import json
import time
from collections import deque
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

import httpx

from arbkit.chain.spec import ChainSpec
from arbkit.scan.userops import ENTRYPOINTS
from arbkit.value.fees import units
from mevlab.decode.signatures import NATIVE_ETH

BINANCE_HOSTS = ("https://data-api.binance.vision", "https://api.binance.com")
MAX_GAP_MINUTES = 15


def day_of(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


class PriceBook:
    def __init__(self, cache_dir: Path, offline: bool = False):
        self.cache_dir = Path(cache_dir)
        self.offline = offline
        self.series: dict[str, dict[int, float]] = {}
        self.failed: dict[tuple[str, str], float] = {}  # (symbol, day) -> monotonic time of the failed fetch
        self.retry_after = 300.0  # a long-running follower retries a failed day after five minutes

    def set(self, symbol: str, ts: int, price: float) -> None:
        self.series.setdefault(symbol, {})[ts // 60 * 60] = price

    def _path(self, symbol: str, day: str) -> Path:
        return self.cache_dir / "open" / symbol / f"{day}.json"

    def _load_day(self, symbol: str, day: str) -> bool:
        path = self._path(symbol, day)
        if not path.exists():
            return False
        try:
            rows = json.loads(path.read_text())
        except ValueError:
            return False
        series = self.series.setdefault(symbol, {})
        for minute, close in rows:
            series[int(minute)] = float(close)
        return True

    async def _fetch_day(self, symbol: str, day: str) -> bool:
        start = int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
        rows: list[tuple[int, float]] = []
        async with httpx.AsyncClient(timeout=20) as client:
            for host in BINANCE_HOSTS:
                try:
                    rows = []
                    for offset in (0, 1000 * 60):
                        params = {
                            "symbol": symbol,
                            "interval": "1m",
                            "startTime": (start * 1000) + offset * 1000,
                            "endTime": (start + 86_400) * 1000 - 1,
                            "limit": 1000,
                        }
                        response = await client.get(f"{host}/api/v3/klines", params=params)
                        if response.status_code != 200:
                            raise httpx.HTTPError(f"status {response.status_code}")
                        for k in response.json():
                            rows.append(
                                (int(k[0]) // 1000, float(k[1]))
                            )  # the minute's open: final once it starts
                    break
                except (httpx.HTTPError, ValueError):
                    rows = []
                    continue
        if not rows:
            return False
        path = self._path(symbol, day)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sorted(set(rows))))
        return self._load_day(symbol, day)

    async def ensure(self, symbol: str, timestamps) -> None:
        timestamps = list(timestamps)
        days = {day_of(ts) for ts in timestamps}
        today = day_of(int(datetime.now(timezone.utc).timestamp()))
        for day in sorted(days):
            failed_at = self.failed.get((symbol, day))
            if failed_at is not None and (self.offline or time.monotonic() - failed_at < self.retry_after):
                continue
            if self._load_day(symbol, day):
                # a cache of the current day only reaches the minute it was fetched: refresh it when a block's
                # minute is not in it yet (its open is final as soon as the minute starts)
                wanted = max(ts for ts in timestamps if day_of(ts) == day) // 60 * 60
                have = max((m for m in self.series.get(symbol, {}) if day_of(m) == day), default=0)
                if day != today or wanted <= have or self.offline:
                    continue
            if self.offline or not await self._fetch_day(symbol, day):
                self.failed[(symbol, day)] = time.monotonic()

    def price_exact(self, symbol: str, ts: int) -> float | None:
        """The close of exactly this minute, no neighbour fallback."""
        return (self.series.get(symbol) or {}).get(ts // 60 * 60)

    def price_at(self, symbol: str, ts: int) -> float | None:
        series = self.series.get(symbol)
        if not series:
            return None
        minute = ts // 60 * 60
        if minute in series:
            return series[minute]
        for gap in range(1, MAX_GAP_MINUTES + 1):
            for candidate in (minute - 60 * gap, minute + 60 * gap):
                if candidate in series:
                    return series[candidate]
        return None


def drop_partial_edges(edges):
    """Drop a leg whose amount of a token is under 1 % of the largest amount of that token moved in the cycle: the trace
    split one deposit into several transfers and paired a dust part with the whole output (Base 0x94386a7a: 0.0044 cbETH
    "for" 10.545 WETH implied 2,376 WETH per cbETH and a $42.0k gross for $27 of real profit)."""
    biggest: dict[str, int] = {}
    for t_in, a_in, t_out, a_out in edges:
        biggest[t_in] = max(biggest.get(t_in, 0), a_in)
        biggest[t_out] = max(biggest.get(t_out, 0), a_out)
    kept = [e for e in edges if e[1] * 100 >= biggest[e[0]] and e[3] * 100 >= biggest[e[2]]]
    return kept or edges


def max_priced_leg_usd(spec, book, row, ts) -> float | None:
    """USD size of the largest leg amount in a token priced without the trade's own rates (wrapped native, stablecoins,
    Binance-listed tokens)."""
    trace = row.get("trace") or {}
    best = None
    for leg in (trace.get("cycle_path") or []) + (trace.get("legs") or []):
        for key_t, key_a in (("token_in", "amount_in"), ("token_out", "amount_out")):
            token, amount = leg.get(key_t), leg.get(key_a)
            if not token or not amount:
                continue
            usd, _, source = _token_usd(spec, book, token, amount, ts, [])
            if usd is not None and not str(source).startswith("leg_implied"):
                best = usd if best is None else max(best, usd)
    return best


def transfer_tax_tokens(row) -> set[str]:
    """Tokens whose payout from the operator is an exact round share (0.5–20 %, to the basis point) of one of the
    cycle's own transfers of that token: a fee-on-transfer tax taken out of the operator's transfer, not a gain it paid on."""
    trace = row.get("trace") or {}
    amounts: dict[str, list[int]] = {}
    for leg in trace.get("cycle_path") or []:
        for key_t, key_a in (("token_in", "amount_in"), ("token_out", "amount_out")):
            try:
                amounts.setdefault(str(leg.get(key_t) or "").lower(), []).append(int(leg.get(key_a) or 0))
            except (TypeError, ValueError):
                continue
    out = set()
    for token, payees in (trace.get("payouts_by_token") or {}).items():
        for paid in payees.values():
            p = int(paid)
            for a in amounts.get(token.lower(), []):
                for base in (a, a + p):
                    if base <= 0 or p <= 0:
                        continue
                    bps = 10_000 * p / base
                    if 50 <= bps <= 2_000 and abs(bps - round(bps)) < 1e-6 and round(bps) % 50 == 0:
                        out.add(token.lower())
    return out


def leg_edges(row) -> list[tuple[str, int, str, int]]:
    """(token_in, amount_in, token_out, amount_out) from the operator's own cycle path, else from the trace legs.
    The cycle path first: the trace legs hold every venue's flows of the transaction, a hook's side swap or another
    trader's leg included, and an exchange rate read from those priced a hook's fee residue at $46 (Base 0x2eabba74)
    and a $0.09 gain at $21.20 (Base 0xbc36a108) — manual review 2026-10-31."""
    trace = row.get("trace") or {}
    edges = []
    for leg in trace.get("cycle_path") or []:
        try:
            a_in, a_out = int(leg["amount_in"]), int(leg["amount_out"])
        except (KeyError, TypeError, ValueError):
            continue
        if leg.get("token_in") and leg.get("token_out") and a_in > 0 and a_out > 0:
            edges.append((leg["token_in"].lower(), a_in, leg["token_out"].lower(), a_out))
    if edges:
        return drop_partial_edges(edges)
    for leg in trace.get("legs") or []:
        try:
            a_in, a_out = int(leg["amount_in"]), int(leg["amount_out"])
        except (KeyError, TypeError, ValueError):
            continue
        if leg.get("token_in") and leg.get("token_out") and a_in > 0 and a_out > 0:
            edges.append((leg["token_in"].lower(), a_in, leg["token_out"].lower(), a_out))
    if not edges:
        for leg in trace.get("cycle_path") or []:
            try:
                a_in, a_out = int(leg["amount_in"]), int(leg["amount_out"])
            except (KeyError, TypeError, ValueError):
                continue
            if a_in > 0 and a_out > 0:
                edges.append((leg["token_in"].lower(), a_in, leg["token_out"].lower(), a_out))
    return edges


def implied_amount(token: str, amount: int, edges, priced: set[str]) -> tuple[str, Fraction, int] | None:
    """Convert `amount` of `token` into a priced token through the shortest chain of legs.

    Returns (priced_token, amount_in_priced_token_raw, hops). Rates are the executed exchange rates of this
    transaction, used in both directions."""
    token = token.lower()
    if token in priced:
        return token, Fraction(amount), 0
    graph: dict[str, list[tuple[str, Fraction]]] = {}
    for t_in, a_in, t_out, a_out in edges:
        graph.setdefault(t_in, []).append((t_out, Fraction(a_out, a_in)))
        graph.setdefault(t_out, []).append((t_in, Fraction(a_in, a_out)))
    queue = deque([(token, Fraction(amount), 0)])
    seen = {token}
    while queue:
        current, value, hops = queue.popleft()
        for nxt, rate in graph.get(current, []):
            if nxt in seen:
                continue
            seen.add(nxt)
            converted = value * rate
            if nxt in priced:
                return nxt, converted, hops + 1
            queue.append((nxt, converted, hops + 1))
    return None


def _token_usd(spec: ChainSpec, book: PriceBook, token: str | None, amount, ts: int, edges):
    """(usd, native_amount, source) for an exact amount of a token, or (None, None, 'unpriced')."""
    if token is None or amount is None:
        return None, None, "unpriced"
    token = token.lower()
    amount = int(amount)
    native_usd = book.price_at(spec.native_price_symbol, ts)
    by_addr = spec.token_by_address

    def direct(tok: str, raw: Fraction):
        if tok == spec.weth:
            if native_usd is None:
                return None, None, None
            native = float(raw / 10**18)
            return native * native_usd, native, "native"
        info = by_addr.get(tok)
        if info is None or not info.price:
            return None, None, None
        value = float(raw / 10**info.decimals)
        if info.price == "usd":
            usd = value
            return usd, (usd / native_usd if native_usd else None), "stable"
        if info.price == "native":
            if native_usd is None:
                return None, None, None
            return value * native_usd, value, "native"
        price = book.price_at(info.price, ts)
        if price is None:
            return None, None, None
        usd = value * price
        return usd, (usd / native_usd if native_usd else None), f"binance:{info.symbol}"

    usd, native, source = direct(token, Fraction(amount))
    if source:
        return usd, native, source
    priced = {spec.weth} | {t.address for t in spec.tokens if t.price}
    found = implied_amount(token, amount, edges, priced)
    if found is None:
        return None, None, "unpriced"
    target, raw, hops = found
    usd, native, source = direct(target, raw)
    if not source:
        return None, None, "unpriced"
    symbol = "native" if target == spec.weth else by_addr[target].symbol
    return usd, native, f"leg_implied:{symbol}:{hops}"


CEX_DEX_MIN_NOTIONAL_USD = 1_000.0  # below this the edge is noise and the trade is retail-sized
CEX_DEX_MIN_EDGE_BPS = 15.0  # Binance 1-minute open vs the block second: ~5-10 bps of noise on majors
# Only the first user transactions of a block (or the first of an estimated flashblock) qualify: when the CEX price
# moves inside the Binance minute, every swap of the block shows the same "edge" (Ethereum 26137380: twelve retail
# swaps through one router at 40-55 bps), so a mid-block edge says nothing about who rebalanced the pool.
CEX_DEX_TOP_USER_INDEX = 2
CEX_DEX_MAX_WALLETS_PER_BLOCK = (
    2  # three or more wallets "beating" Binance in one block is a stale minute, not three rebalances
)


def _direct_only(source: str | None) -> bool:
    return bool(source) and (source in {"native", "stable"} or source.startswith("binance:"))


def cex_dex_candidate(spec: ChainSpec, book: PriceBook, row: dict, ts: int) -> dict | None:
    """The on-chain leg of a CEX-DEX rebalancing (the arbitrageur's side of LVR): a single-route swap at the top of
    the block (first three user transactions, or first in its estimated flashblock) whose execution price beats the
    Binance 1-minute price by more than the noise band. Both tokens must have a direct price (native, stable or a
    Binance symbol); leg-implied prices would be circular. The off-chain leg is unobservable, so the figure is the
    edge versus the CEX price, not a profit."""
    flow = row.get("swap_flow")
    if not flow or ts is None:
        return None
    usd_in, _, src_in = _token_usd(spec, book, flow["token_in"], flow["amount_in_raw"], ts, [])
    usd_out, _, src_out = _token_usd(spec, book, flow["token_out"], flow["amount_out_raw"], ts, [])
    if (
        usd_in is None
        or usd_out is None
        or not _direct_only(src_in)
        or not _direct_only(src_out)
        or usd_in < CEX_DEX_MIN_NOTIONAL_USD
    ):
        return None
    edge = usd_out - usd_in
    bps = 10_000 * edge / usd_in
    top = (
        row.get("user_index_in_block") is not None and row["user_index_in_block"] <= CEX_DEX_TOP_USER_INDEX
    ) or bool(row.get("first_in_slot"))
    if bps < CEX_DEX_MIN_EDGE_BPS or not top:
        return None
    if spec.ordering_policy == "fcfs":
        # First-come chains (Arbitrum, Robinhood) have no top of block worth the name, so the position filter does
        # not separate a rebalance from a stale Binance minute. The edge must still hold against the next minute's
        # open (the end of the block's minute): a CEX price that caught up within a minute was the DEX leading, not the
        # CEX being arbitraged. Live, that minute does not exist yet, so candidates appear only when a block is re-run
        # (Arbitrum 512920692: five wallets, one pool, 47-50 bps against one stale minute).
        symbols = {spec.native_price_symbol} | {
            info.price
            for token in (flow["token_in"], flow["token_out"])
            for info in [spec.token_by_address.get(str(token).lower())]
            if info and info.price not in (None, "usd", "native")
        }
        if any(book.price_exact(symbol, ts + 60) is None for symbol in symbols):
            return None  # the next minute is not in the book: persistence cannot be shown
        usd_in_next, _, _ = _token_usd(spec, book, flow["token_in"], flow["amount_in_raw"], ts + 60, [])
        usd_out_next, _, _ = _token_usd(spec, book, flow["token_out"], flow["amount_out_raw"], ts + 60, [])
        if (
            usd_in_next is None
            or usd_out_next is None
            or 10_000 * (usd_out_next - usd_in_next) / usd_in_next < CEX_DEX_MIN_EDGE_BPS
        ):
            return None
    return {
        "notional_usd": round(usd_in, 2),
        "received_usd": round(usd_out, 2),
        "edge_usd": round(edge, 2),
        "edge_bps": round(bps, 1),
        "position": "top_of_block"
        if (row.get("user_index_in_block") or 0) <= CEX_DEX_TOP_USER_INDEX
        else "top_of_flashblock",
        "price_sources": [src_in, src_out],
        "venues": flow.get("venues"),
        "dexes": flow.get("dexes"),
        "note": "execution price vs Binance 1m open; candidate for the on-chain leg of a CEX-DEX rebalancing, not a measured profit",
    }


UNPRICED_DUST_SHARE = (
    0.05  # an unpriced net change up to 5 % of the token's volume through the operator is dust
)


def value_sandwich(spec: ChainSpec, book: PriceBook, sandwich: dict, ts: int, edges, native_usd) -> None:
    """USD result of a sandwich bundle over every token the operator ended up holding.

    The operator's net changes over front + back (`net_raw`) can span several tokens: the real gain plus dust of
    the other pool token, or a gain in one token against a loss in another. Every entry with a price is valued
    and summed; the profit token shown is the priced token with the largest USD gain, not the largest raw
    integer, which ranked an 18-decimal dust balance above a 6-decimal stablecoin gain and left the bundle
    unpriced. Tokens without a price are listed in `unpriced_tokens`; when no gain can be priced the bundle
    stays `unpriced`.
    """
    nets = {str(t).lower(): int(v) for t, v in (sandwich.get("net_raw") or {}).items() if int(v)}
    if not nets and sandwich.get("profit_token") and sandwich.get("profit_raw"):
        nets = {sandwich["profit_token"].lower(): int(sandwich["profit_raw"])}
    valued: dict[str, tuple[float, float | None, str]] = {}
    unpriced: dict[str, str] = {}
    for token, raw in nets.items():
        usd, native, source = _token_usd(spec, book, token, abs(raw), ts, edges)
        if usd is None or not _direct_only(source):
            # a price implied by the bundle's own legs is not trusted for a net balance: dust of a token priced through
            # a thin pool had made a JIT bundle that netted 1.01 USDC + 0.0021 WETH read as $8,471 (Ethereum 26318713)
            unpriced[token] = str(raw)
        else:
            sign = 1 if raw > 0 else -1
            valued[token] = (sign * usd, sign * native if native is not None else None, source)
    sandwich["unpriced_tokens"] = unpriced
    # The result is the sum of every net change. Leaving out an unpriced loss overstates it: Base JIT bundles read
    # +$1-3k from the USDC side alone while the token sold for it went uncounted (2026-11-04). A material unpriced loss
    # (more than UNPRICED_DUST_SHARE of that token's volume through the operator) leaves the result unknown; an unpriced
    # gain left out only makes the result a lower bound.
    moved = {str(t).lower(): int(v) for t, v in (sandwich.get("moved_raw") or {}).items()}
    material = sorted(
        t
        for t, raw in unpriced.items()
        if int(raw) < 0 and (not moved.get(t) or -int(raw) > UNPRICED_DUST_SHARE * moved[t])
    )
    if material:
        sandwich["unpriced_losses"] = material
    gains = {t: v for t, v in valued.items() if v[0] > 0}
    if not gains or material:
        sandwich["profit_usd"], sandwich["profit_native"], sandwich["price_source"], sandwich["net_usd"] = (
            None,
            None,
            "unpriced",
            None,
        )
        return
    best = max(gains, key=lambda t: gains[t][0])
    info = spec.token_by_address.get(best)
    if best != str(sandwich.get("profit_token") or "").lower() and (info is not None or best == spec.weth):
        sandwich["profit_token"], sandwich["profit_raw"] = best, str(nets[best])
        symbol, decimals = (info.symbol, info.decimals) if info is not None else (spec.native_symbol, 18)
        sandwich["profit_symbol"], sandwich["profit_decimals"] = symbol, decimals
        sandwich["profit"] = units(nets[best], decimals)
    sandwich["profit_usd"] = sum(v[0] for v in valued.values())
    natives = [v[1] for v in valued.values()]
    sandwich["profit_native"] = sum(natives) if all(n is not None for n in natives) else None
    sources = sorted({v[2] for v in valued.values()})
    sandwich["price_source"] = sources[0] if len(sources) == 1 else "mixed:" + "+".join(sources)
    bundle_fee = sandwich.get("bundle_fee_wei")
    tips = int(sandwich.get("bundle_builder_tip_wei") or 0)
    if native_usd is not None and bundle_fee is not None:
        sandwich["net_usd"] = sandwich["profit_usd"] - (bundle_fee + tips) / 1e18 * native_usd
    else:
        sandwich["net_usd"] = None


INVENTORY_MIN_USD = 0.10
INVENTORY_MIN_SHARE = 0.01  # mixed assets: the gain must beat Binance-minute noise on a volatile leg
INVENTORY_MIN_SHARE_STABLE = 0.002  # every asset a USD stablecoin: no price noise to beat
INVENTORY_EXCLUDED = {
    "settlement_like",
    "bundled_operation",
    "debt_operation",
    "lp_action",
    "pool_exploit",
    "share_operation",
    "hook_mechanism",
    "reverted",
    "round_trip",
}


def inventory_arbitrage(spec: ChainSpec, book: PriceBook, row: dict, ts: int) -> None:
    """Family `inventory_arbitrage`: a route that does not close in one token but ends worth more than it started.

    The operator pays one asset and receives another through >= 2 decoded swaps at >= 2 distinct pools; every other
    token it touched nets to zero at the venues; every asset that does not net to zero is priced (stablecoins at
    $1, the native coin and declared majors at the block's Binance price — never an implied price); and the
    market value of what it received beats what it paid by >= 1 % (0.2 % when all are USD stablecoins) and $0.10.
    BSC 130965616 #5 backran a user's BRKL buy: WBNB -> USDT -> BRKL -> BUSD -> USDC, 478.23 USDT into the loop and
    690.92 USDC out (+$212), which a same-token cycle rule never sees. Brontes counts the stablecoin-to-stablecoin
    case as atomic arbitrage and research classifiers count gains that cover losses at market rates; here the family is
    shown on its own, outside the cycle totals, because its result depends on market prices rather than closing to the
    unit. The figure is the gross at market prices before gas and tips.

    Not this family (read 2026-11-01 on the six stores, 161 rows):
      - a user's swap: the sender's own wallet pays or receives the tokens through a router, aggregator or smart wallet
        (`sender_wallet_trades`), or the callee is an ERC-4337 EntryPoint (Ethereum 26317003 #52 split 55,503 USDC to
        USDT through a router and the Sky PSM: its adapter's DAI, burned at once, read as an $11,484 gain; 1inch users
        whose route crossed a mispriced token; Base user operations);
      - an asset the decoded swaps do not explain: every asset that does not net to zero must be the swaps' own net
        in that asset (within 2 %), else the operator group took in someone else's flow (the PSM adapter above);
      - one pool off the market, its output kept: when a single swap's own market-value gain is half the result or
        more and later swaps sell less than half of what it bought, the trade is one venue against an outside price, a CEX-DEX
        candidate, not a route (Ethereum 26315600 #2 sold 0.774 WETH on Ekubo at $2,393 against $2,259 and kept the
        USDC; its other legs were a separate dust loop). A stale pool whose output is sold on through another pool is
        the route itself (BSC 130945368 #3: BTCB -> CAKE at a stale BTCB/CAKE pool, CAKE -> ETH -> WBNB -> BTCB,
        0.0082692 BTCB back as 0.011986)."""
    if (
        row.get("assessment") not in ("not_arb", "needs_review")
        or (row.get("mev_type") or "none") != "none"
        or row.get("arb_class") in INVENTORY_EXCLUDED
        or row.get("sandwich")
    ):
        return
    if row.get("sender_wallet_trades") or str(row.get("tx_to") or "").lower() in ENTRYPOINTS:
        return
    trace = row.get("trace") or {}
    if trace.get("status") != "ok" or trace.get("verdict") not in ("no_taker", "gain_no_route"):
        return
    event = row.get("event_label") or {}
    if (
        int(event.get("n_legs") or row.get("n_legs") or 0) < 2
        or len(set(event.get("venues") or row.get("venues") or [])) < 2
    ):
        return
    weth = spec.weth.lower()

    def norm(token) -> str:
        t = str(token or "").lower()
        return weth if t in (NATIVE_ETH, "eth") else t

    deltas: dict[str, int] = {}
    for t, v in (trace.get("asset_deltas") or {}).items():
        if int(v):
            deltas[norm(t)] = deltas.get(norm(t), 0) + int(v)
    if not any(v > 0 for v in deltas.values()) or not any(v < 0 for v in deltas.values()):
        return
    legs = [lg for lg in trace.get("legs") or [] if lg.get("token_in") and lg.get("token_out")]
    swaps_net: dict[str, int] = {}
    for lg in legs:
        swaps_net[norm(lg["token_in"])] = swaps_net.get(norm(lg["token_in"]), 0) - int(
            lg.get("amount_in") or 0
        )
        swaps_net[norm(lg["token_out"])] = swaps_net.get(norm(lg["token_out"]), 0) + int(
            lg.get("amount_out") or 0
        )
    for token, raw in deltas.items():
        n = swaps_net.get(token, 0)
        if not n or (n > 0) != (raw > 0) or abs(n - raw) > 0.02 * max(abs(n), abs(raw)):
            return  # an asset the decoded swaps do not explain

    def market(token, raw) -> tuple[float | None, bool]:
        usd, _, source = _token_usd(spec, book, token, abs(int(raw)), ts, [])
        if usd is None or source not in ("stable", "native") and not str(source).startswith("binance:"):
            return None, False
        return usd, source == "stable"

    paid = received = 0.0
    stable = True
    for token, raw in deltas.items():
        usd, is_stable = market(token, raw)
        if usd is None:
            return  # an unpriced asset that did not net to zero: no value judgement
        stable = stable and is_stable
        if raw > 0:
            received += usd
        else:
            paid += usd
    gain = received - paid
    share = INVENTORY_MIN_SHARE_STABLE if stable else INVENTORY_MIN_SHARE
    if gain < INVENTORY_MIN_USD or gain < share * paid:
        return
    for i, lg in enumerate(legs):
        vin, _ = market(norm(lg["token_in"]), lg.get("amount_in") or 0)
        vout, _ = market(norm(lg["token_out"]), lg.get("amount_out") or 0)
        if vin is None or vout is None or vout - vin < 0.5 * gain:
            continue
        at, out = int(lg.get("order", i)), norm(lg["token_out"])
        sold_on = sum(
            int(x.get("amount_in") or 0)
            for j, x in enumerate(legs)
            if int(x.get("order", j)) > at and norm(x["token_in"]) == out
        )
        if sold_on < 0.5 * int(lg.get("amount_out") or 0):  # a dust sale of it later is not a route
            row["reasons"] = [*row.get("reasons", []), "inventory_gain_in_one_pool"]
            return  # one pool off the market price and its output kept: a CEX-DEX candidate, not a route
    row["inventory_arb"] = {
        "gain_usd": round(gain, 6),
        "paid_usd": round(paid, 6),
        "received_usd": round(received, 6),
        "stable_pair": stable,
        "deltas": {t: str(v) for t, v in deltas.items()},
    }
    row["mev_type"] = "inventory_arbitrage"


PAIR_LOSS_SHARE = (
    0.5  # the predecessor lost at least half the route's gain: the gain is mostly that loss coming back
)


def pair_with_predecessor(spec: ChainSpec, book: PriceBook, row: dict, ts: int) -> None:
    """An inventory route whose gain is the loss of the transaction just before it, sent together with it (the same gas
    price to the wei, wallet or contract; `engine.annotate_paired_predecessor`), is one operator's two-transaction round
    trip: the pair's joint result is what anyone made, and it is not arbitrage. Kept on the row as
    `inventory_arb_rejected` with both halves and the joint figure; the row leaves the family.

    The predecessor's result is its sender's and callee's token nets in its receipt at market prices (the same prices
    as the route's); an unpriced net token leaves the route as it was (no value judgement)."""
    pair = row.get("paired_predecessor")
    inv = row.get("inventory_arb")
    if not pair or not inv or row.get("mev_type") != "inventory_arbitrage":
        return
    weth = spec.weth.lower()
    result = 0.0
    for token, raw in (pair.get("party_net_raw") or {}).items():
        token = weth if token in (NATIVE_ETH, "eth") else token
        usd, _, source = _token_usd(spec, book, token, abs(int(raw)), ts, [])
        if usd is None or source not in ("stable", "native") and not str(source).startswith("binance:"):
            return
        result += usd if int(raw) > 0 else -usd
    if result > -PAIR_LOSS_SHARE * inv["gain_usd"]:
        return
    row["inventory_arb_rejected"] = {
        **inv,
        "reason": "gain_is_predecessor_loss",
        "predecessor": pair["tx_hash"],
        "link": pair["link"],
        "predecessor_result_usd": round(result, 6),
        "joint_usd": round(inv["gain_usd"] + result, 6),
    }
    del row["inventory_arb"]
    row["mev_type"] = "none"
    row["reasons"] = [*row.get("reasons", []), f"inventory_pair_with_predecessor:{pair['tx_index']}"]


async def value_rows(spec: ChainSpec, rows, ts_by_block: dict[int, int], book: PriceBook) -> None:
    timestamps = [ts for ts in ts_by_block.values()]
    needed = {spec.native_price_symbol} | {
        t.price for t in spec.tokens if t.price not in (None, "usd", "native")
    }
    for symbol in sorted(needed):
        await book.ensure(symbol, timestamps)
    for row in rows:
        ts = ts_by_block.get(row["block_number"])
        row["native_usd"] = book.price_at(spec.native_price_symbol, ts) if ts is not None else None
        native_usd = row["native_usd"]
        fee = row["fees"]["total_fee_wei"]
        row["fee_usd"] = fee / 1e18 * native_usd if fee is not None and native_usd is not None else None
        tip = int(row.get("builder_tip_wei") or 0)
        row["builder_tip_usd"] = tip / 1e18 * native_usd if native_usd is not None else None
        kickback = int(row.get("kickback_wei") or 0)
        row["kickback_usd"] = kickback / 1e18 * native_usd if native_usd is not None else None
        row["gross_usd"], row["gross_native"], row["price_source"] = None, None, "unpriced"
        row["net_usd"] = None
        edges = leg_edges(row)
        if row["assessment"] in {"trace_supported", "trace_supported_loss"} and ts is not None:
            usd, native, source = _token_usd(
                spec, book, row.get("profit_token"), row.get("gross_profit_raw"), ts, edges
            )
            # Every token the operator ended up with counts: a second gain in another asset was dropped (BSC 0x833c591d
            # WBNB, Ethereum 0xefe235dc USDT, Arbitrum 0xeffd3dfd whose WETH profit was left out and ZRO dust kept).
            taxed = transfer_tax_tokens(row)
            if str(row.get("profit_token") or "").lower() in taxed:
                # the "gain" in this token is a transfer tax the token sent to its fee wallet out of the operator's
                # transfer (Arbitrum 0xd5d22227 / 0x97a822ae: exactly 3 % of "Up" to 0xbbc3…): no profit in it
                usd, native, source = None, None, "unpriced"
                row["reasons"] = [*row.get("reasons", []), "profit_token_is_transfer_tax"]
            extras = {}
            for token, raw in ((row.get("trace") or {}).get("extra_gains") or {}).items():
                if token.lower() in taxed:
                    continue
                if int(raw) > 0 and token.lower() != str(row.get("profit_token") or "").lower():
                    e_usd, e_native, e_source = _token_usd(spec, book, token, raw, ts, edges)
                    extras[token.lower()] = {
                        "raw": str(raw),
                        "usd": e_usd,
                        "native": e_native,
                        "price_source": e_source,
                    }
            if extras:
                row["extra_gains_valued"] = extras
                best = max(extras, key=lambda t: extras[t]["usd"] or 0)
                if extras[best]["usd"] is not None and (usd is None or extras[best]["usd"] > usd):
                    # the larger gain is the profit; the former profit token becomes an extra
                    old = (
                        str(row.get("profit_token") or "").lower(),
                        row.get("gross_profit_raw"),
                        usd,
                        native,
                        source,
                    )
                    row["profit_token"], row["gross_profit_raw"] = best, extras[best]["raw"]
                    usd, native, source = (
                        extras[best]["usd"],
                        extras[best]["native"],
                        extras[best]["price_source"],
                    )
                    row["extra_gains_valued"] = {
                        **{t: v for t, v in extras.items() if t != best},
                        **(
                            {old[0]: {"raw": old[1], "usd": old[2], "native": old[3], "price_source": old[4]}}
                            if old[0]
                            else {}
                        ),
                    }
                    row["reasons"] = [*row.get("reasons", []), "profit_token_by_value"]
                usd = (
                    None
                    if usd is None
                    else usd + sum(v["usd"] for v in row["extra_gains_valued"].values() if v["usd"])
                )
                if native is not None:
                    native += sum(v["native"] for v in row["extra_gains_valued"].values() if v["native"])
            # A price implied by the trade's own legs cannot make the gain larger than half of what the operator put in:
            # such a figure is a broken exchange rate (Arbitrum 0xcdbc8010 priced ZRO at $822.9 instead of ~$1.83).
            if usd is not None and str(source).startswith("leg_implied"):
                # an implied-price gross cannot exceed half the dollar size of the trade's largest priced leg (or of its
                # entry): such a figure is a broken exchange rate (Arbitrum 0xcdbc8010 ZRO at $822.9; Base 0x94386a7a
                # 0.0078 cbETH at $42.0k), never a profit. The row stays counted, its dollars unpriced.
                path = (row.get("trace") or {}).get("cycle_path") or []
                sizes = [max_priced_leg_usd(spec, book, row, ts)]
                if path:
                    sizes.append(
                        _token_usd(spec, book, path[0].get("token_in"), path[0].get("amount_in"), ts, [])[0]
                    )
                sizes = [x for x in sizes if x]
                if sizes and abs(usd) > 0.5 * max(sizes):
                    row["reasons"] = [
                        *row.get("reasons", []),
                        f"implausible_implied_price:{usd:.4g}>half_of_trade_{max(sizes):.4g}",
                    ]
                    usd, native, source = None, None, "unpriced"
            row["gross_usd"], row["gross_native"], row["price_source"] = usd, native, source
            if usd is not None and row["assessment"] == "trace_supported":
                if row.get("operator_scope") == "embedded":
                    row["net_usd"] = None  # the executor's gas share inside another caller's tx is unknown
                elif row["fee_usd"] is not None:
                    row["net_usd"] = (
                        usd - row["fee_usd"] - (row["builder_tip_usd"] or 0) - (row["kickback_usd"] or 0)
                    )
        if row.get("swap_flow") and row["assessment"] == "not_arb" and ts is not None:
            candidate = cex_dex_candidate(spec, book, row, ts)
            row["cex_dex"] = candidate
            if candidate and row.get("mev_type") in (None, "none"):
                row["mev_type"] = "cex_dex_candidate"
            elif not candidate and row.get("mev_type") == "cex_dex_candidate":
                row["mev_type"] = "none"  # repriced under a stricter rule
        for liq in row.get("liquidations") or []:
            if ts is None:
                break
            d_usd, _, d_src = _token_usd(
                spec,
                book,
                liq.get("debt_asset") if not liq.get("debt_market_is_ctoken") else None,
                liq.get("debt_repaid_raw"),
                ts,
                edges,
            )
            c_usd, _, c_src = _token_usd(
                spec,
                book,
                liq.get("collateral_asset") if not liq.get("collateral_in_ctoken_shares") else None,
                liq.get("collateral_seized_raw"),
                ts,
                edges,
            )
            if c_usd is None and liq.get("collateral_usd_from_protocol") is not None:
                c_usd, c_src = liq["collateral_usd_from_protocol"], "protocol_oracle"
            liq["debt_usd"], liq["collateral_usd"] = d_usd, c_usd
            liq["price_sources"] = [d_src, c_src]
            liq["bonus_usd"] = round(c_usd - d_usd, 2) if c_usd is not None and d_usd is not None else None
        sandwich = row.get("sandwich")
        if sandwich and sandwich.get("role") == "back" and ts is not None:
            value_sandwich(spec, book, sandwich, ts, edges, native_usd)
        jit = row.get("jit")
        if jit and jit.get("role") == "remove" and jit.get("net_raw") is not None and ts is not None:
            value_sandwich(
                spec, book, jit, ts, edges, native_usd
            )  # same measurement: the provider's net over add + remove
        if ts is not None:
            inventory_arbitrage(spec, book, row, ts)
            pair_with_predecessor(spec, book, row, ts)

    # Several wallets with the same edge in one block: the Binance minute was stale, nobody was rebalancing.
    by_block: dict[int, list[dict]] = {}
    for row in rows:
        if row.get("cex_dex"):
            by_block.setdefault(row["block_number"], []).append(row)
    for number, group in by_block.items():
        wallets = {str(r.get("tx_from") or "").lower() for r in group}
        if len(wallets) > CEX_DEX_MAX_WALLETS_PER_BLOCK:
            for r in group:
                r["cex_dex"] = None
                r["cex_dex_dropped"] = f"shared_edge:{len(wallets)}_wallets_in_block"
                if r.get("mev_type") == "cex_dex_candidate":
                    r["mev_type"] = "none"
