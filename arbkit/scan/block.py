"""Classify every transaction of one block: event layer, traces for the candidates, venue checks, positions,
sandwiches and other families, builder payments made in separate transactions."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arbkit.chain.spec import ChainSpec
from arbkit.scan.bundle import BlockTraceRpc, decode_extra_data, fetch_trace, validate_bundle
from arbkit.scan.families import annotate_block, family_of
from arbkit.scan.liquidity import created_pools, liquidity_evidence
from arbkit.scan.positions import TriggerContext, assign_positions
from arbkit.scan.sandwich import (
    _pool_directions,
    find_cyclic_sandwiches,
    find_sandwiches,
    operator_result,
    round_trip_verdict,
)
from arbkit.scan.trace import apply_trace, builder_tip, mark_pool_exploit
from arbkit.scan.userops import annotate_operations
from arbkit.scan.venues import verify_cycle_venues
from arbkit.value.fees import receipt_fees
from mevlab.decode.lifecycle import liquidity_change
from mevlab.decode.signatures import TRANSFER_TOPIC, selector
from mevlab.decode.swaps import SWAP_DECODERS
from mevlab.hexutil import addr, to_int
from mevlab.pools.prices import PoolPriceTracker
from mevlab.receipt.classify import classify_tx
from mevlab.rpc import RpcError
from mevlab.trace.calls import hook_liquidity_only
from mevlab.trace.cashflows import native_flows
from mevlab.trace.movements import receipt_backed_movements
from mevlab.trace.venues import venues_of


def receipt_route_hint(tx, receipt, weth):
    """Request traces when receipts suggest a route missed by Swap-event decoders.

    This only schedules verification; it never establishes an arbitrage label.
    """
    weth = weth.lower()
    flows = receipt_backed_movements([], receipt.get("logs", []), weth)
    exclude = {str(tx.get("from", "")).lower(), str(tx.get("to", "")).lower(), weth}
    wrap_topics = {
        "0xe1fffcc4923d04b559f4d29a8bfc6cda04eb5b0d3c460751c2402c5c5cc9109c",
        "0x7fcf532c15f0a6db0bd6d0e038bea71d30d808c7d98cb3bf7268a95bf5081b65",
    }
    native_conversion = len({m.token for m in flows}) >= 2 and any(
        log.get("address", "").lower() == weth and log.get("topics", [None])[0] in wrap_topics
        for log in receipt.get("logs", [])
        if log.get("topics")
    )
    return (
        native_conversion
        or len(venues_of(flows, exclude)) >= 2
        or (
            len({m.token for m in flows}) >= 2
            and any(m.kind in {"receipt_mint", "receipt_burn"} for m in flows)
        )
    )


def wants_trace(row, mode):
    if row["fees"]["is_system"] or row["arb_class"] == "reverted" or mode == "none":
        return False
    if mode == "all":
        return True
    if mode == "swaps":
        return row["n_legs"] > 0 or row["arb_class"] == "unknown" or row.get("receipt_route_hint", False)
    return (
        row["is_arb"]
        or row["n_legs"] >= 2
        or row["arb_class"] == "unknown"
        or row["gross_unreliable"]
        or row.get("receipt_route_hint", False)
    )


def require_conserved_gross(spec: ChainSpec, row, trace, receipt) -> None:
    """A cycle's gross is the operator's measured balance change: rebuilt independently from the raw receipt Transfer
    logs, WETH deposits/withdrawals, successful native calls and claim logs for the operator group (plus the payouts
    the trace names), it must equal the trace layer's figure to the unit, or the number is not known and the row is
    held for review. This is the global guard behind every rule about who the operator is: whatever mechanic folds a
    wrong address into the group (a token's tax distributor, a vault, a converter), that address's other flows break
    the balance and the row stops counting instead of showing a made-up gross. Rows whose result the trace layer
    did not produce (event-layer singleton amounts) are left to their own checks."""
    if (
        row.get("assessment") not in ("trace_supported", "trace_supported_loss")
        or row.get("gross_source") != "trace"
    ):
        return
    from arbkit.verify.audit import cash_check

    # The profit token only: an extra gain in a second token is measured before payouts while the audit adds back
    # only the named payees' receipts, so a tip funded partly from the sender's balance (BSC 0x7e8ac906: WBNB route
    # gain to the collector, 48 Club tip in BNB from the EOA) differs by design. Checking extras held 2 confirmed cycles.
    claims = {str(row.get("profit_token") or "").lower(): row.get("gross_profit_raw")}
    bad = []
    for token, raw in claims.items():
        if not token:
            continue
        try:
            check = cash_check(
                {**row, "profit_token": token, "gross_profit_raw": raw}, receipt, trace, spec.weth
            )
        except (KeyError, TypeError, ValueError):
            return
        if check.get("gross_matches") is False:
            bad.append(f"{token}:{raw}!={check.get('gross_from_receipts')}")
    if bad:
        reason = "gross_not_reproduced_from_receipts:" + ";".join(bad)
        row["review_reasons"] = sorted(
            {*(row.get("review_reasons") or []), "gross_not_reproduced_from_receipts"}
        )
        row["reasons"] = [*row["reasons"], reason]
        row["assessment"] = "needs_review"
        row["is_arb"] = False
        return
    # A second gain the receipts do not reproduce never counts: pricing adds extra gains to the gross and makes the
    # most valuable one the profit token, so an unreproduced one would be a made-up figure. Ethereum 26316628 #231:
    # DAI bought by a USDC->DAI->USDS converter and burned at once read as a 0.49 DAI extra and became the "profit"
    # of a cycle that made 0.0000487 ETH. The row's own profit, reproduced above, stands.
    extras = dict((row.get("trace") or {}).get("extra_gains") or {})
    dropped = []
    for token, raw in list(extras.items()):
        if row["assessment"] != "trace_supported" or int(raw or 0) <= 0:
            continue
        try:
            check = cash_check(
                {**row, "profit_token": str(token).lower(), "gross_profit_raw": str(raw)},
                receipt,
                trace,
                spec.weth,
            )
        except (KeyError, TypeError, ValueError):
            continue
        if check.get("gross_matches") is False:
            dropped.append(f"{token}:{raw}!={check.get('gross_from_receipts')}")
            extras.pop(token)
    if dropped:
        row["trace"] = {**row["trace"], "extra_gains": extras}
        row["reasons"] = [*row["reasons"], "extra_gain_not_reproduced:" + ";".join(dropped)]


def annotate_paired_predecessor(
    rows: list[dict], tx_by_index: dict[int, dict], by_hash: dict[str, dict]
) -> None:
    """Note the transaction right before a possible inventory route when the two look sent together: the same gas price
    to the wei (or the same wallet or contract) and a pool in common that the route moves back the other way. The
    pricing stage (`pricing.pair_with_predecessor`) values that transaction's own result and, when the route's gain is
    mostly that transaction's loss, the pair is one operator's two-transaction round trip, not an arbitrage.

    BSC 0x976aed19 (sender 0x2996b250): 128 of its 132 inventory rows in 48 h (2026-11-06/07) came one position after
    a transaction of one of six contracts of its own build (same storage layout: Pancake V2/V3 routers, V2 factory,
    WBNB), each paying exactly the same gas price as the route that followed it (1.6-2.4 gwei, a different price for
    every pair; 5 % of other BSC backruns match their trigger's price). The predecessors lost $13,415 through the
    pools the routes then reversed for $13,089: a $7.1k/day "inventory gain" whose pairs netted -$326 before gas.
    The facts are recorded here; the receipt-level nets of the predecessor's sender and callee are what is valued."""
    by_index = {r["tx_index"]: r for r in rows}

    def gas_price(i: int) -> int | None:
        tx = tx_by_index.get(i) or {}
        receipt = by_hash.get(str(tx.get("hash") or "").lower()) or by_hash.get(tx.get("hash")) or {}
        value = receipt.get("effectiveGasPrice") or tx.get("gasPrice")
        return int(value, 16) if isinstance(value, str) else (int(value) if value is not None else None)

    for row in rows:
        trace = row.get("trace") or {}
        if (
            trace.get("status") != "ok"
            or trace.get("verdict") not in ("no_taker", "gain_no_route")
            or row["n_legs"] < 2
        ):
            continue
        prev = by_index.get(row["tx_index"] - 1)
        if prev is None or not prev.get("venues") or prev["fees"]["is_system"]:
            continue
        row_logs = (by_hash.get(row["tx_hash"]) or {}).get("logs", [])
        prev_logs = (by_hash.get(prev["tx_hash"]) or {}).get("logs", [])
        reversed_pools = []
        for v in sorted(set(row["venues"]) & set(prev["venues"])):
            mine, theirs = _pool_directions(row_logs, v), _pool_directions(prev_logs, v)
            if mine and theirs and mine[0] != 0 and mine[0] == -theirs[0]:
                reversed_pools.append(v)
        if not reversed_pools:
            continue
        mine_from, mine_to = str(row.get("tx_from") or "").lower(), str(row.get("tx_to") or "").lower()
        prev_from, prev_to = str(prev.get("tx_from") or "").lower(), str(prev.get("tx_to") or "").lower()
        gp, prev_gp = gas_price(row["tx_index"]), gas_price(prev["tx_index"])
        link = (
            "same_wallet"
            if mine_from and mine_from == prev_from
            else "same_contract"
            if mine_to and mine_to == prev_to
            else "same_gas_price"
            if gp is not None and gp == prev_gp
            else None
        )
        if link is None:
            continue
        party = {prev_from, prev_to} - {""}
        net: dict[str, int] = {}
        receipt = by_hash.get(prev["tx_hash"]) or by_hash.get(prev["tx_hash"].lower()) or {}
        for lg in receipt.get("logs", []):
            topics = [str(t).lower() for t in lg.get("topics") or []]
            if len(topics) != 3 or topics[0] != TRANSFER_TOPIC.lower():
                continue
            sender, receiver = "0x" + topics[1][-40:], "0x" + topics[2][-40:]
            data = lg.get("data", "0x")
            value = int(data[:66], 16) if data not in ("0x", "") else 0
            token = str(lg.get("address", "")).lower()
            if receiver in party and sender not in party:
                net[token] = net.get(token, 0) + value
            if sender in party and receiver not in party:
                net[token] = net.get(token, 0) - value
        row["paired_predecessor"] = {
            "tx_hash": prev["tx_hash"],
            "tx_index": prev["tx_index"],
            "link": link,
            "gas_price_wei": str(prev_gp) if prev_gp is not None else None,
            "reversed_pools": sorted(reversed_pools),
            "party": sorted(party),
            "party_net_raw": {t: str(v) for t, v in net.items() if v},
        }


def mark_beaten(rows: list[dict]) -> None:
    """A losing cycle's winner: the nearest earlier counted cycle in the block, by another wallet and contract, that
    made one of the loser's own swaps (same pool, same direction) first. When one bot wins an opportunity the next bot to execute it loses; the loser is kept
    in its own section with a link to the winner (`beaten_by`), never netted against what the winners extracted.
    A loss cycle the same operator reverses after a victim is a sandwich front instead (find_cyclic_sandwiches)."""

    def swaps(row) -> set[tuple[str, str, str]]:  # (pool, token in, token out) of every decoded leg
        return {
            (str(lg["venue"]).lower(), str(lg["token_in"]).lower(), str(lg["token_out"]).lower())
            for lg in (row.get("trace") or {}).get("legs") or []
            if isinstance(lg, dict) and lg.get("venue") and lg.get("token_in") and lg.get("token_out")
        }

    ordered = sorted(rows, key=lambda r: r["tx_index"])
    for i, loser in enumerate(ordered):
        if loser.get("assessment") != "trace_supported_loss":
            continue
        mine = swaps(loser)
        for winner in reversed(ordered[:i]):
            # the same swap on the same pool, earlier: the winner took the price gap the loser was sent to take
            if (
                winner.get("assessment") == "trace_supported"
                and winner.get("tx_from") != loser.get("tx_from")
                and (winner.get("tx_to") or "") != (loser.get("tx_to") or "")
                and mine & swaps(winner)
            ):
                loser["beaten_by"] = {
                    "tx_hash": winner["tx_hash"],
                    "tx_index": winner["tx_index"],
                    "operator": winner.get("tx_to") or winner.get("tx_from"),
                }
                winner["beat"] = [*winner.get("beat", []), loser["tx_hash"]]
                break


def mev_type_of(row) -> str:
    if row["assessment"] == "trace_supported":
        if row["arb_class"] == "embedded_cycle":
            return "embedded_cycle"
        return {
            "backrun": "atomic_backrun",
            "top_of_block": "atomic_top_of_block",
            "top_of_flashblock": "atomic_top_of_flashblock",
        }.get(row.get("trigger_type"), "atomic_mid_block")
    sandwich = row.get("sandwich")
    if sandwich:
        return "sandwich_" + sandwich["role"]
    if row["assessment"] == "trace_supported_loss":
        return "loss_cycle"
    if row["assessment"] == "event_candidate":
        return "candidate_cycle"
    if row["assessment"] == "needs_review":
        event = row.get("event_label") or {}
        return "candidate_cycle" if (event.get("is_arb") or row.get("n_legs", 0) >= 2) else "none"
    return family_of(row) or "none"


class BlockContext:
    """State carried across contiguous blocks: pool prices and the cross-block trigger memory."""

    def __init__(self, spec: ChainSpec):
        self.spec = spec
        self.tracker = PoolPriceTracker()
        self.triggers = TriggerContext(spec.trigger_window_blocks)
        self.previous: tuple[int, str] | None = None

    def reset(self):
        self.tracker = PoolPriceTracker()
        self.triggers.reset()
        self.previous = None


def singleton_liquidity_only(receipt, spec: ChainSpec) -> bool:
    """The receipt holds at least one liquidity event, all of them ModifyLiquidity events of a registered singleton
    manager, and a swap: the one shape in which a Uniswap V4 hook's own liquidity event (a fee collect, an
    auto-compound, a range rebalance made while serving the swap) can hold a cycle as an lp_action. The trace then
    decides whose event it was before the event layer labels the row."""
    logs = receipt.get("logs") or []
    changes = [lg for lg in logs if liquidity_change(lg)]
    if not changes:
        return False
    managers = {addr(m) for m in spec.pool_managers}
    if any(addr(str(lg.get("address", ""))) not in managers for lg in changes):
        return False
    return any(str((lg.get("topics") or [""])[0]).lower() in SWAP_DECODERS for lg in logs)


@dataclass
class BlockScan:
    """One block being classified: its header facts, its receipts by transaction hash and the traces fetched so far."""

    spec: ChainSpec
    block: dict
    by_hash: dict
    resolver: Any
    trace_rpc: Any  # BlockTraceRpc, or None without a trace endpoint
    trace_mode: str
    trace_dir: Path | None
    refresh: bool
    traces: dict[str, dict] = field(default_factory=dict)

    @property
    def miner(self) -> str | None:
        return addr(self.block["miner"]) if self.block.get("miner") else None

    @property
    def tracing(self) -> bool:
        return self.trace_rpc is not None and self.trace_mode != "none"

    def trace_path(self, tx_hash: str) -> Path | None:
        return self.trace_dir / f"{self.block['hash']}_{tx_hash}.json.gz" if self.trace_dir else None

    async def fetch(self, tx_hash: str) -> dict:
        return await fetch_trace(self.trace_rpc, tx_hash, self.trace_path(tx_hash), self.refresh)

    async def fetch_missing(self, tx_hashes) -> None:
        """The traces of these transactions, when tracing and not fetched yet; a failed fetch is skipped."""
        if not self.tracing:
            return
        for tx_hash in tx_hashes:
            if tx_hash in self.traces:
                continue
            try:
                self.traces[tx_hash] = await self.fetch(tx_hash)
            except (RpcError, ValueError):
                continue


async def classify_block(
    spec: ChainSpec,
    block,
    receipts,
    resolver,
    ctx: BlockContext | None = None,
    trace_rpc=None,
    trace_mode="candidates",
    trace_dir=None,
    refresh=False,
):
    """Classify every transaction of one block, then what only the whole block shows:

    1. each transaction: event layer, trace verdict for the candidates, venue checks (`classify_transaction`)
    2. sandwiches (front, victims, back) and the operator's result over them
    3. beaten attempts, positions (backrun, top of block, ...), other families (liquidations, snipes, JIT, CEX-DEX)
    4. JIT liquidity results
    5. MEV type per row, refunds paid to a backrun's trigger, builder payments made in separate transactions"""
    ctx = ctx or BlockContext(spec)
    number = to_int(block["number"])
    by_hash = validate_bundle(block, receipts, number)
    scan = BlockScan(
        spec,
        block,
        by_hash,
        resolver,
        BlockTraceRpc(trace_rpc, block) if trace_rpc is not None else None,
        trace_mode,
        trace_dir,
        refresh,
    )
    rows = []
    tx_by_index = {}
    for tx in block["transactions"]:
        row = await classify_transaction(scan, ctx, tx)
        tx_by_index[row["tx_index"]] = tx
        rows.append(row)
    await mark_sandwiches(scan, rows)
    mark_beaten(rows)
    assign_positions(spec, rows, tx_by_index, ctx.triggers, number)
    # Other MEV families on the rows the cycle classifier left alone: liquidations, launch snipes, JIT liquidity,
    # and the swap flows the pricing stage turns into CEX-DEX candidates (docs/METHOD.md, "Other families").
    annotate_block(spec, rows, block, by_hash, tx_by_index)
    annotate_paired_predecessor(rows, tx_by_index, by_hash)
    await measure_jit(scan, rows)
    for row in rows:
        row["mev_type"] = mev_type_of(row)
        record_trigger_refund(row, tx_by_index)
    await attach_separate_payments(
        spec,
        rows,
        block,
        by_hash,
        scan.miner,
        scan.trace_rpc if trace_mode != "none" else None,
        trace_dir,
        refresh,
    )
    return rows


async def classify_transaction(scan: BlockScan, ctx: BlockContext, tx: dict) -> dict:
    """One transaction's row: the event layer's label, fees, then (for candidates) the trace verdict, the venue checks
    and the conservation check of the counted gross."""
    spec, block, resolver = scan.spec, scan.block, scan.resolver
    receipt = scan.by_hash[tx["hash"].lower()]
    # A Uniswap V4 hook may collect, compound or rebalance its own liquidity inside the swap it serves (Robinhood
    # 84259655 #4, BSC 83600063 #2): those ModifyLiquidity events are the pool's, not the operator's, and must not
    # hold the row as an lp_action. Only the trace tells whose they are, so it is read before the event layer runs.
    early_trace = None
    hook_liquidity = False
    if scan.tracing and singleton_liquidity_only(receipt, spec):
        try:
            early_trace = await scan.fetch(tx["hash"])
        except (RpcError, ValueError):
            early_trace = None
        hook_liquidity = early_trace is not None and hook_liquidity_only(early_trace, set(spec.pool_managers))
    label = await classify_tx(
        tx,
        receipt,
        resolver,
        base_fee_per_gas=to_int(block.get("baseFeePerGas")) if block.get("baseFeePerGas") is not None else 0,
        weth=spec.weth,
        family=spec.family,
        tracker=ctx.tracker,
        system_tx_types=spec.system_tx_types,
        hook_liquidity=hook_liquidity,
    )
    row = label.model_dump()
    row["event_label"] = label.model_dump()
    row["gross_profit_raw"] = str(label.gross_profit_raw) if label.gross_profit_raw is not None else None
    row["gross_source"] = "events"
    row["fees"] = receipt_fees(tx, receipt, block, spec, await operator_fee(spec, receipt, resolver))
    row["assessment"] = "event_candidate" if label.is_arb else "not_arb"
    annotate_operations(row, tx, receipt)
    row["explorer_url"] = spec.explorer_url(label.tx_hash)
    row["block_producer"] = scan.miner
    row["block_builder_tag"] = decode_extra_data(block.get("extraData"))
    row["trace"] = {"status": "not_requested"}
    row["known_recipient_received_wei"] = None
    row["kickback_wei"] = None
    row["builder_tip_wei"] = None
    row["receipt_route_hint"] = row["n_legs"] <= 1 and receipt_route_hint(tx, receipt, spec.weth)
    if wants_trace(row, scan.trace_mode):
        if scan.trace_rpc is None:
            row["trace"] = {"status": "unavailable"}
        else:
            try:
                trace = early_trace if early_trace is not None else await scan.fetch(label.tx_hash)
                apply_trace(spec, row, trace, tx, receipt, scan.miner, resolver)
                await verify_cycle_venues(spec, row, resolver, receipt)
                require_conserved_gross(spec, row, trace, receipt)
                scan.traces[label.tx_hash] = trace
            except (RpcError, ValueError) as exc:
                row["trace"] = {"status": "unavailable", "reason": str(exc)}
    if row["assessment"] != "not_arb" or row["arb_class"] != "pool_exploit":
        mark_pool_exploit(row)  # untraced rows too: an exploit is never a candidate cycle
    if label.arb_class == "lp_action":
        row["liquidity"] = await liquidity_evidence(receipt.get("logs", []), resolver)
    if row["arb_class"] == "lp_action" and not row.get("venues"):
        created = created_pools(receipt.get("logs", []))
        if created:  # the transaction sets a new pool's first price (positions.created_pools)
            row["created_pools"] = created
    return row


async def operator_fee(spec: ChainSpec, receipt: dict, resolver) -> int | None:
    """OP Stack operator fee (Isthmus): asked of the GasPriceOracle when the receipt announces one, else None."""
    if not (
        spec.family == "op-stack"
        and any(to_int(receipt.get(k)) for k in ("operatorFeeScalar", "operatorFeeConstant"))
    ):
        return None
    try:
        data = selector("getOperatorFee(uint256)") + f"{to_int(receipt['gasUsed']):064x}"
        return to_int(await resolver.rpc.eth_call("0x420000000000000000000000000000000000000f", data))
    except (RpcError, AttributeError, ValueError):
        return None


async def mark_sandwiches(scan: BlockScan, rows: list[dict]) -> None:
    """Find sandwiches (classic and cyclic), measure the operator's result over front + back, and tag every leg."""
    spec, by_hash, miner = scan.spec, scan.by_hash, scan.miner
    rows_by_hash = {r["tx_hash"]: r for r in rows}
    for bundle in [
        *find_sandwiches(rows, by_hash),
        *find_cyclic_sandwiches(rows, by_hash, scan.traces, spec.weth),
    ]:
        legs = (bundle["front"], bundle["back"])
        await scan.fetch_missing(legs)
        wallet = rows_by_hash[bundle["front"]]["tx_from"]
        leg_venues = {str(v).split(":")[0] for h in legs for v in (rows_by_hash[h].get("venues") or [])}
        builders = spec.recipients_of_kind("builder")
        result = operator_result(
            bundle, by_hash, spec.weth, scan.traces, wallet, leg_venues, producer=miner, builders=builders
        )
        verdict = round_trip_verdict(result)
        if verdict is not None:  # a position change that brackets a victim, not a sandwich (docs/METHOD.md)
            rows_by_hash[bundle["back"]]["sandwich_rejected"] = {
                "bundle_id": bundle["bundle_id"],
                "front": bundle["front"],
                "victims": bundle["victims"],
                "reason": verdict,
                "net_raw": result["net_raw"],
                "moved_raw": result["moved_raw"],
            }
            continue
        fee_wei = sum(rows_by_hash[h]["fees"]["total_fee_wei"] or 0 for h in legs)
        tips = 0
        for tx_hash in legs:
            trace = scan.traces.get(tx_hash)
            if trace:
                # a sandwich leg is the searcher's own transaction: everything it pays the producer, plus what it pays
                # a declared builder address (BSC's 48 Club / BlockRazor; the same sum `apply_trace` books on rows)
                paid = sum(
                    int(v.get("eth_weth_received_wei") or 0)
                    for v in (rows_by_hash[tx_hash].get("known_recipient_payments") or {}).values()
                    if v.get("kind") == "builder"
                )
                tip = builder_tip(trace, None, miner) + paid
                rows_by_hash[tx_hash]["builder_tip_wei"] = str(tip)
                tips += tip
        if bundle.get("kind") == "cyclic":
            # the two cycles are one sandwich: neither is an arbitrage nor a loss on its own (find_cyclic_sandwiches)
            for tx_hash in legs:
                leg = rows_by_hash[tx_hash]
                leg["assessment"], leg["is_arb"] = "not_arb", False
                leg["reasons"] = [*leg["reasons"], f"cyclic_sandwich_leg:{bundle['bundle_id']}"]
        for role, hashes in (
            ("front", [bundle["front"]]),
            ("victim", bundle["victims"]),
            ("back", [bundle["back"]]),
        ):
            for tx_hash in hashes:
                info = {
                    "bundle_id": bundle["bundle_id"],
                    "role": role,
                    "pool": bundle["pool"],
                    "operator": bundle["operator"],
                    "operator_kind": bundle["operator_kind"],
                    "front": bundle["front"],
                    "back": bundle["back"],
                    "victims": bundle["victims"],
                }
                if (
                    bundle.get("link") == "holder"
                ):  # legs sent by different wallets, tied by whose tokens they spent
                    info["link"] = "holder"
                if role == "back":
                    info.update(result, bundle_fee_wei=fee_wei, bundle_builder_tip_wei=str(tips))
                rows_by_hash[tx_hash]["sandwich"] = info


async def measure_jit(scan: BlockScan, rows: list[dict]) -> None:
    """JIT liquidity: the provider's net over its add and remove transactions, measured like a sandwich's (receipt
    transfers plus the traces' native legs, which V4 native-ETH pools need), so the family carries a result. Ethereum
    JIT bundles had paid builders more than the counted cycles (2026-11-01) with no figure for what they made."""
    jit_pairs: dict[str, dict[str, dict]] = {}
    for r in rows:
        if r.get("jit"):
            jit_pairs.setdefault(r["jit"]["bundle_id"], {})[r["jit"]["role"]] = r
    for bundle_id, pair in jit_pairs.items():
        add_r, rem_r = pair.get("add"), pair.get("remove")
        if not add_r or not rem_r:
            continue
        legs = (add_r["tx_hash"], rem_r["tx_hash"])
        await scan.fetch_missing(legs)
        bundle = {
            "front": legs[0],
            "back": legs[1],
            "bundle_id": bundle_id,
            "pool": rem_r["jit"]["pool"],
            "victims": rem_r["jit"]["victims"],
            "operator": str(rem_r.get("tx_to") or rem_r.get("tx_from") or "").lower(),
        }
        result = operator_result(
            bundle,
            scan.by_hash,
            scan.spec.weth,
            scan.traces,
            rem_r.get("tx_from"),
            {str(rem_r["jit"]["pool"]).split(":")[0].lower()},
            producer=scan.miner,
        )
        tips = 0
        for r in (add_r, rem_r):
            trace = scan.traces.get(r["tx_hash"])
            if trace:
                tip = builder_tip(trace, None, scan.miner)  # the provider's own transaction
                r["builder_tip_wei"] = str(tip)
                tips += tip
        rem_r["jit"].update(
            result,
            bundle_fee_wei=sum(r["fees"]["total_fee_wei"] or 0 for r in (add_r, rem_r)),
            bundle_builder_tip_wei=str(tips),
        )


def record_trigger_refund(row: dict, tx_by_index: dict) -> None:
    """MEV-Share-style refunds: part of a backrun's proceeds paid back to the trigger transaction's sender/callee."""
    row["refund_to_trigger_wei"] = None
    if not (
        row.get("trigger_type") == "backrun"
        and row.get("trigger_tx_hash")
        and row["trace"].get("status") == "ok"
    ):
        return
    trigger = (
        tx_by_index.get(row.get("trigger_tx_index")) if row.get("trigger_distance_blocks") == 0 else None
    )
    if not trigger:
        return
    targets = {str(trigger.get("from") or "").lower(), str(trigger.get("to") or "").lower()} - {""}
    refund = sum(
        int(v)
        for by in (row["trace"].get("payouts_by_token") or {}).values()
        for to, v in by.items()
        if to.lower() in targets
    )
    row["refund_to_trigger_wei"] = str(refund) if refund else None
    if refund:
        row["reasons"] = [*row["reasons"], "refund_paid_to_trigger_sender"]


PAYING_TYPES = {
    "atomic_backrun",
    "atomic_top_of_block",
    "atomic_mid_block",
    "atomic_top_of_flashblock",
    "embedded_cycle",
    "loss_cycle",
    "sandwich_front",
    "sandwich_back",
    "liquidation",
    "jit_liquidity",
    "launch_snipe",
}


async def attach_separate_payments(
    spec: ChainSpec, rows, block, by_hash, miner, trace_rpc, trace_dir, refresh
) -> None:
    """A bundle may pay the block builder in a transaction of its own: no logs, value that ends at the block fee
    recipient or a registered builder address, sent by the same wallet or to the same bot contract as an MEV row
    within three positions (Ethereum 26315278 #7 paid Titan for the sandwich back-run #6 through its bot contract
    from another wallet). The payment is that row's cost: added to its builder tip, or to the sandwich bundle's tip
    (held on the back-run), and recorded on both rows."""
    targets = spec.recipients_of_kind("builder") | ({miner} if miner else set())
    paying = [r for r in rows if r.get("mev_type") in PAYING_TYPES]
    if not paying or not targets:
        return
    rows_by_hash = {r["tx_hash"].lower(): r for r in rows}
    for tx in block["transactions"]:
        tx_hash = tx["hash"].lower()
        value = to_int(tx.get("value", "0x0"))
        sender, to = str(tx.get("from") or "").lower(), str(tx.get("to") or "").lower()
        receipt = by_hash.get(tx_hash) or {}
        if not value or receipt.get("logs") or sender == miner or to_int(receipt.get("status", "0x1")) != 1:
            continue
        index = to_int(tx["transactionIndex"])
        near = [
            r
            for r in paying
            if r["tx_index"] != index
            and abs(r["tx_index"] - index) <= 3
            and (
                str(r.get("tx_from") or "").lower() == sender
                or (to and str(r.get("tx_to") or "").lower() == to)
            )
        ]
        if not near:
            continue
        if to in targets:
            amount = value
        elif trace_rpc is not None:
            path = trace_dir / f"{block['hash']}_{tx['hash']}.json.gz" if trace_dir else None
            try:
                trace = await fetch_trace(trace_rpc, tx["hash"], path, refresh)
            except (RpcError, ValueError):
                continue
            amount = sum(
                v for _, receiver, v in native_flows(trace) if receiver and receiver.lower() in targets
            )
        else:
            continue
        if not amount:
            continue
        payee = min(
            near, key=lambda r: (r["tx_index"] > index, abs(r["tx_index"] - index))
        )  # the row just before it
        entry = {"tx_hash": tx_hash, "tx_index": index, "wei": str(amount)}
        sandwich = payee.get("sandwich") or {}
        if sandwich.get("role") in ("front", "back") and sandwich.get("back") in rows_by_hash:
            back = rows_by_hash[sandwich["back"]]
            back["sandwich"]["bundle_builder_tip_wei"] = str(
                int(back["sandwich"].get("bundle_builder_tip_wei") or 0) + amount
            )
            back["sandwich"]["separate_payments"] = [*back["sandwich"].get("separate_payments", []), entry]
            payee = back
        else:
            payee["builder_tip_wei"] = str(int(payee.get("builder_tip_wei") or 0) + amount)
        payee["separate_payments"] = [*payee.get("separate_payments", []), entry]
        payee["reasons"] = [*payee.get("reasons", []), "builder_paid_in_separate_transaction"]
        payer = rows_by_hash.get(tx_hash)
        if payer is not None:
            payer["pays_builder_for"] = payee["tx_hash"]
            payer["reasons"] = [*payer.get("reasons", []), "separate_builder_payment"]
