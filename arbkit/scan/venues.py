"""Venue verification: a cycle counts only when every venue on its route is a real pool that swapped in this
transaction (decoded Swap event, or the tokens it holds and the flows it made)."""

from __future__ import annotations

from arbkit.chain.spec import ChainSpec
from mevlab.decode.signatures import ZERO_ADDRESS, event_topic, selector
from mevlab.decode.swaps import SWAP_DECODERS
from mevlab.rpc import RpcError


def hook_addresses(resolver, event_venues) -> set[str]:
    """Hook contracts of the singleton pools on the event route (from the resolver's Initialize cache)."""
    table = getattr(resolver, "table", None) or {}
    zero = "0x" + "0" * 40
    out = set()
    for key in event_venues:
        meta = table.get(str(key).lower())
        hooks = getattr(meta, "hooks", None) if meta else None
        if hooks and hooks.lower() != zero:
            out.add(hooks.lower())
    return out


TOKEN_LIST_CALLS = (
    "token0()",
    "token1()",
    "coins(uint256):0",
    "coins(uint256):1",
    "coins(uint256):2",
    "coins(uint256):3",
    "getTokens()",
    "getCurrentTokens()",
    "tokenAddress()",
    "getImmutables()",
    "_BASE_TOKEN_()",
    "_QUOTE_TOKEN_()",
    "tokenA()",
    "tokenB()",
)  # DODO V2, Maverick V2


PAIR_CALLS = {"token0()", "token1()", "tokenA()", "tokenB()", "_BASE_TOKEN_()", "_QUOTE_TOKEN_()"}


async def venue_tokens(resolver, venue: str) -> set[str]:
    """The tokens a venue trades, from its own view functions — accepted only where the interface proves a pool:
    Curve coins(i), Mooniswap getTokens, Balancer V1 getCurrentTokens, Uniswap V1 tokenAddress (with ETH), MetricOmm
    getImmutables (`MetricOmmPool`, six chains). Pair getters (token0/token1, tokenA/tokenB, DODO base/quote) count only
    when the contract's factory() is a known DEX factory of the chain: strategy vaults and ALM vaults expose the same
    getters (Base 0xb6e91d9b, a factory-made vault whose rebalance a Safe executed for a skim, listed cbETH/WETH as
    tokenA/tokenB and had been read as a pool — 2026-10-31). Asked at the scanned block, then at the head; cached."""
    cache = resolver.__dict__.setdefault("venue_token_cache", {})
    if venue in cache:
        return cache[venue]
    found: set[str] = set()
    meta = resolver.table.get(venue)
    if (
        meta is not None
        and meta.token0
        and meta.token1
        and (meta.dex or str(meta.factory or "").lower() in resolver.factory_to_dex)
    ):
        found = {meta.token0.lower(), meta.token1.lower()}
    elif getattr(resolver, "rpc", None) is not None:
        rpc = resolver.rpc
        for tag in (None, "latest"):
            pinned = rpc.block_tag
            if tag:
                rpc.block_tag = tag
            try:
                calls = [entry for entry in (*TOKEN_LIST_CALLS, "factory()")]
                batch = []
                for entry in calls:
                    sig, _, arg = entry.partition(":")
                    batch.append(
                        (
                            "eth_call",
                            [
                                {"to": venue, "data": selector(sig) + (f"{int(arg):064x}" if arg else "")},
                                "latest",
                            ],
                        )
                    )
                results = await rpc.batch(batch, lenient=True)
            except RpcError:
                results = []
            finally:
                rpc.block_tag = pinned
            answers = dict(zip(calls, results or []))
            factory = answers.get("factory()")
            factory = (
                ("0x" + factory[-40:]).lower() if isinstance(factory, str) and len(factory) >= 66 else ""
            )
            known_factory = factory in resolver.factory_to_dex
            for entry, raw in answers.items():
                if entry == "factory()" or not isinstance(raw, str) or len(raw) < 66:
                    continue
                if entry.split(":")[0] in PAIR_CALLS and not known_factory:
                    continue
                for w in (raw[i : i + 64] for i in range(2, len(raw), 64)):
                    if w[:24] == "0" * 24 and int(w, 16) > 0xFFFFFFFF:  # an address-shaped word
                        found.add("0x" + w[24:].lower())
                if entry == "tokenAddress()":
                    found.add(resolver.__dict__.get("weth", ZERO_ADDRESS))
            if found:
                break
    cache[venue] = found
    return found


async def issues_lp_token(resolver, venue: str, token: str) -> bool:
    """`token` is the LP token of pool `venue`, proven both ways the Curve interfaces allow: the pool's `lp_token()`
    names it (FRAXBP 0xdcef968d -> crvFRAX), or the token's `minter()` names the pool (3CRV -> 3pool). Cached."""
    cache = resolver.__dict__.setdefault("lp_token_cache", {})
    key = (venue, token)
    if key in cache:
        return cache[key]
    rpc = getattr(resolver, "rpc", None)
    if rpc is None:
        return False
    answered = False
    # Asked at the scanned block, then at the head: the LP-token relation never changes, and a node that pruned the
    # block's state must not turn it into a cached "no" (the regression scans October blocks first: FRAXBP had been
    # cached as unverified for the whole run).
    for tag in (None, "latest"):
        pinned = rpc.block_tag
        if tag:
            rpc.block_tag = tag
        try:
            for to, sig, expect in ((venue, "lp_token()", token), (token, "minter()", venue)):
                try:
                    raw = await rpc.eth_call(to, selector(sig))
                except RpcError:
                    continue
                answered = True
                if isinstance(raw, str) and len(raw) >= 66 and ("0x" + raw[-40:]).lower() == expect:
                    cache[key] = True
                    return True
        finally:
            rpc.block_tag = pinned
    if answered:
        cache[key] = False
    return False


# Swap events measured on chain whose venues the decoders do not price yet (2026-10-31, held-row audit):
# MantaAMMV2 `Swapped(address indexed sender, address indexed tokenIn, address indexed tokenOut, uint256 amountIn,
# uint256 amountOut, address recipient)` (BaseScan 0x5d2806b9…, verified; the same address on Base and Robinhood; its
# custody contracts held 462 Base rows) and Curve Twocrypto's TokenExchange variant emitted by 0x656341ef… on Ethereum
# (Etherscan: `Twocrypto`; topic measured from its logs, parameter types not yet read off the ABI).
MANTA_SWAPPED = event_topic("Swapped(address,address,address,uint256,uint256,address)").lower()
# Fluid DEX `Swap(bool swap0to1, uint256 amountIn, uint256 amountOut, address to)` (hash checked against pool 0x667701e5's
# logs on Ethereum): the tokens sit in Fluid's Liquidity contract (0x52aa8994…, its `LogOperate` 0x4d93b232 also measured),
# so a leg there is verified when a Fluid Swap in the same receipt carries exactly its two amounts.
FLUID_SWAP = event_topic("Swap(bool,uint256,uint256,address)").lower()
# 0x0e1f3c59 was listed here on 2026-10-31 as "Curve Twocrypto's TokenExchange variant" without reading the ABI. The verified
# Twocrypto ABI (Etherscan, pool 0x656341ef, Vyper 0.4.3) makes it `AddLiquidity(address,address,uint256[2],uint256,uint256,
# uint256)`: a liquidity event, whose presence had let a Yield Basis share redemption pass the decoded-swap rule and show as a
# $4,527 "arbitrage" (Ethereum 26316933 #190). Twocrypto's real swap, TokenExchange 0x143f1f8e, is decoded already.
MEASURED_SWAP_TOPICS = {MANTA_SWAPPED, FLUID_SWAP}


def swap_topics(spec: ChainSpec) -> set[str]:
    """topic0 of every Swap event the decoders or the chain's registry know (V2/V3/V4/Infinity/Curve/Balancer…)."""
    topics = {str(t).lower() for t in SWAP_DECODERS} | MEASURED_SWAP_TOPICS
    topics |= {event_topic(d["swap_event"]).lower() for d in spec.dexes if d.get("swap_event")}
    return topics


def event_backed_legs(receipt, weth: str) -> set[tuple[str, str, int, int]]:
    """(token_in, token_out, amount_in, amount_out) of every swap event that names its tokens (MantaAMMV2 Swapped):
    a leg matching one is a swap at that venue whatever contract held the tokens."""
    out = set()
    for lg in (receipt or {}).get("logs") or []:
        topics = [str(t).lower() for t in lg.get("topics") or []]
        data = str(lg.get("data") or "0x")
        if len(topics) == 4 and topics[0] == MANTA_SWAPPED and len(data) >= 2 + 64 * 2:
            norm = lambda t: weth if t == ZERO_ADDRESS else t  # noqa: E731
            out.add(
                (
                    norm("0x" + topics[2][-40:]),
                    norm("0x" + topics[3][-40:]),
                    int(data[2:66], 16),
                    int(data[66:130], 16),
                )
            )
        elif len(topics) == 1 and topics[0] == FLUID_SWAP and len(data) >= 2 + 64 * 3:
            out.add(
                (None, None, int(data[66:130], 16), int(data[130:194], 16))
            )  # amounts only: both must match
    return out


async def verify_cycle_venues(spec: ChainSpec, row, resolver, receipt=None) -> None:
    """A cycle runs through at least two distinct liquidity venues and every leg is at one (definition rules 3 and 6,
    docs/METHOD.md). A venue is verified when the receipts decoded its Swap, it
    is registered (declared or quote venue, venue alias, singleton manager), or it is a pair pool for exactly the two
    tokens the leg exchanged. A token contract trading its own token (mint, burn, redemption), a vault, a router, an
    EntryPoint or a user is not a venue: the trace's closed result is then kept for review and never counted. The
    trace layer had promoted such rows on its own — vault redemptions on BSC ($13.2k, $8.3k, $3.5k), a Fluid
    withdrawal and a vault exploit on Arbitrum, ERC-4337 bundles and a bridge hand-off on Base, a keeper zap on
    Robinhood, fee-router user swaps on OP (manual review 2026-10-31)."""
    if row.get("assessment") not in ("trace_supported", "trace_supported_loss") or resolver is None:
        return
    weth = spec.weth
    norm = lambda t: weth if not t or str(t).lower() in (ZERO_ADDRESS, "eth") else str(t).lower()  # noqa: E731
    trace = row.get("trace") or {}
    legs = trace.get("cycle_path") or trace.get("legs") or []
    event = row.get("event_label") or {}
    keys = {str(v).lower() for v in (event.get("venues") or [])}
    managers = set(spec.pool_managers)
    registered = (
        {k.split(":")[0] for k in keys}
        | set(spec.declared_venues or {})
        | set(spec.quote_venues or {})
        | set(spec.venue_aliases or {})
        | managers
    )
    problems, verified, converters = [], set(), []
    backed_legs = event_backed_legs(receipt, weth)
    for leg in legs:
        venue = str(leg.get("venue") or "").lower()
        if not venue:
            continue
        token_in, token_out = norm(leg.get("token_in")), norm(leg.get("token_out"))
        if venue in registered:
            verified.add(venue)
        elif (
            venue in (token_in, token_out)
            and leg.get("token_in")
            and leg.get("token_out")
            and token_in != token_out
        ):
            # a two-sided conversion at the token's own contract (rETH burned for ETH, stETH wrapped into wstETH): the
            # issuer is the counterparty of an exchange, a venue like a pool
            verified.add(venue)
            converters.append(venue)
        elif leg.get("token_out") is None or leg.get("token_in") is None:
            converters.append(venue)  # one-sided: a burn or a mint with nothing coming back in this leg
        else:
            resolver.__dict__.setdefault("weth", weth)
            listed = {norm(t) for t in await venue_tokens(resolver, venue)}
            try:
                amounts = (int(leg.get("amount_in") or 0), int(leg.get("amount_out") or 0))
            except (TypeError, ValueError):
                amounts = (0, 0)
            backed = any(
                (t_in == token_in and t_out == token_out and (a_in == amounts[0] or a_out == amounts[1]))
                or (t_in is None and a_in == amounts[0] and a_out == amounts[1] and a_in > 0)
                for t_in, t_out, a_in, a_out in backed_legs
            )
            if {token_in, token_out} <= listed or backed:
                verified.add(venue)
            elif len({token_in, token_out} & listed) == 1 and await issues_lp_token(
                resolver, venue, ({token_in, token_out} - listed).pop()
            ):
                # one of the pool's coins exchanged for the pool's own LP token (Curve add_liquidity / remove_liquidity
                # _one_coin as a conversion leg of a metapool cycle, Ethereum 26314163 #15)
                verified.add(venue)
            else:
                problems.append(f"unverified_venue:{venue}")
    # A conversion at a token's own contract (rETH burned for ETH) is an arbitrage venue only next to a real DEX swap:
    # with no Swap event in the receipt at all it is a share redeemed from a vault (BSC 0x8983fe4d / 0x91c2daa5: vault
    # redemption payouts of $34k and $15k had closed through an issuer and a token-listing contract).
    topics = swap_topics(spec)
    has_swap = any(
        str((lg.get("topics") or [""])[0]).lower() in topics for lg in (receipt or {}).get("logs") or []
    ) or bool(row.get("quote_venues"))
    if not has_swap:
        # Promotion rule (withdrawals promoted as cycles had carried 389 ETH of non-arbitrage): a trace-closed cycle counts only when the receipt holds a swap
        # the decoders know (or a declared quote venue's leg). Venues without a decoder go to the venue backlog.
        problems.append("no_decoded_swap")
        verified -= set(converters)
    distinct = len(verified - managers) + len({k for k in keys if ":" in k and k.split(":")[0] in managers})
    # Definition rule 4 (the loop paid at pre-trade prices) stays a reason, not a block: the pre-trade prices come from
    # the last Swap/Sync seen and are stale on busy pools, and a 2026-10-31 review found real cycles with a
    # realised gain held by it (OP 0xc1f458c1, BSC 0x083173cb).
    if not problems and distinct < 2 and legs:
        # converter legs only ride along a cycle of real venues; with fewer than two there is no cycle (a token
        # burned at its issuer and redeemed from a vault: BSC 0xbfe33140, a $13.2k "cycle" with no pool at all)
        problems.append(
            "fewer_than_two_venues"
            + (":converters=" + ",".join(sorted(set(converters))) if converters else "")
        )
    elif converters:
        row["reasons"] = [*row["reasons"], "converter_legs:" + ",".join(sorted(set(converters)))]
    if problems:
        row["review_reasons"] = sorted(set(problems))
        row["reasons"] = [*row["reasons"], *sorted(set(problems))]
        row["assessment"] = "needs_review"
        row["is_arb"] = False
