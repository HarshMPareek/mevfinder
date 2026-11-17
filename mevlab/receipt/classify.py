"""Classify one transaction from its receipt alone (the event layer).

Sees: the transaction, its receipt logs, pool metadata and (optionally) the pools' pre-trade prices.
Produces: an ArbLabel - the decoded swap legs, the token path they form, who gained what, and a class
(atomic_cycle, single_swap, lp_action, settlement_like, ...). The trace layer (mevlab.trace) then verifies or
overrules a cycle from the call trace.

Stages, in order (each is a function below):
1. `gate`            transactions that are no attempt at all: reverted, system, probe
2. `read_events`     receipt logs -> swap legs, token transfers, reserve syncs, liquidity / settlement counts
3. `complete_legs`   tokens from pool metadata, V4 hook corrections, native ETH settled without events
4. `track_prices`    pre-trade prices of the legs' pools and the direction this transaction moved them
5. `who_gained`      the callee's net plus what it paid onward (attribution)
6. `decide`          the class, from the path the legs form (mevlab.receipt.path) and the facts above
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mevlab.decode.lifecycle import decode_sync_log
from mevlab.decode.model import SwapLeg, Transfer, canonical_token
from mevlab.decode.signatures import (
    APPROVAL_TOPIC,
    LIQUIDITY_TOPICS,
    SETTLEMENT_TOPICS,
    TRANSFER_TOPIC,
    ZERO_ADDRESS,
)
from mevlab.decode.swaps import decode_swap_log, infer_tokens_from_transfers, merge_bin_legs
from mevlab.decode.transfers import decode_transfer_log, decode_weth_log, weth_mint_burn
from mevlab.hexutil import addr, to_int
from mevlab.pools.meta import PoolMetaResolver
from mevlab.pools.prices import (
    PoolPriceTracker,
    cycle_pre_log_product,
    invariant_violations,
    price_move_sign,
    realized_log_product,
)
from mevlab.receipt.economics import economics_from_receipt
from mevlab.receipt.flows import beneficiary, net_deltas, net_payouts
from mevlab.receipt.hooks import apply_hook_claims, apply_hook_cuts
from mevlab.receipt.label import ArbLabel
from mevlab.receipt.path import PathCheck, analyze_path
from mevlab.version import CLASSIFIER_VERSION

# Leg kinds whose pools answer no token0()/token1(): their tokens come from the pool's own transfers
NO_TOKEN_GETTERS = ("mm_pool_fcfa", "clmm_pair_cd8b", "liquidity_book", "curve", "maverick_v2")


@dataclass
class ReceiptEvents:
    """What the receipt's logs say, decoded."""

    legs: list[SwapLeg] = field(default_factory=list)
    transfers: list[Transfer] = field(default_factory=list)
    syncs: list[tuple[str, int, int]] = field(default_factory=list)
    liquidity_events: int = 0
    settlement_events: int = 0


@dataclass
class Gains:
    """Who got the money (attribution only: a cycle's amount comes from its path)."""

    beneficiary: str | None
    gross: dict[str, int]  # token -> positive net
    losses: dict[str, int]  # token -> negative net
    payouts: dict[str, dict[str, int]]  # token -> recipient -> what the callee paid onward

    @property
    def profit_token(self) -> str | None:
        return max(self.gross, key=self.gross.get) if self.gross else None


@dataclass
class Decision:
    arb_class: str
    is_arb: bool = False
    confidence: float = 0.0
    reasons: list[str] = field(default_factory=list)  # added to the reasons gathered before the decision
    evidence: dict[str, Any] | None = None


async def classify_tx(
    tx: dict[str, Any],
    receipt: dict[str, Any],
    resolver: PoolMetaResolver,
    *,
    base_fee_per_gas: int,
    weth: str,
    family: str = "op-stack",
    tracker: PoolPriceTracker | None = None,
    system_tx_types: frozenset[int] | set[int] = frozenset({126}),
    hook_liquidity: bool = False,
) -> ArbLabel:
    """Classify one tx. If a tracker is given, pre-trade prices are read before the tx's legs are applied
    and the tracker is advanced afterwards (call in block/tx order). `hook_liquidity`: the caller read the trace
    and every liquidity change in the receipt was made by a Uniswap V4 hook inside its own callback."""
    tx_hash = tx.get("hash") or receipt.get("transactionHash")
    tx_from = addr(tx["from"]) if tx.get("from") else None
    tx_to = addr(tx["to"]) if tx.get("to") else None
    logs = receipt.get("logs") or []
    base = dict(
        tx_hash=tx_hash,
        block_number=to_int(tx.get("blockNumber") or receipt.get("blockNumber")),
        tx_index=to_int(tx.get("transactionIndex") or receipt.get("transactionIndex")),
        tx_from=tx_from,
        tx_to=tx_to,
        classifier_version=CLASSIFIER_VERSION,
    )

    # 1. no attempt at all
    early = gate(tx, receipt, tx_to, system_tx_types)
    if early is not None:
        economics = economics_from_receipt(receipt, base_fee_per_gas, {}, family).to_dict()
        return ArbLabel(
            **base,
            is_arb=False,
            arb_class=early.arb_class,
            reasons=early.reasons,
            confidence=early.confidence,
            economics=economics,
        )

    # 2-3. the legs and the transfers around them
    events = read_events(logs, weth)
    legs = merge_bin_legs(events.legs)
    weth_c = weth.lower()
    transfers, hooks_by_key, hooked_zero, reasons = await complete_legs(
        legs, events.transfers, logs, resolver, tx_hash, tx_from, tx_to, weth_c
    )

    # 4. prices: a pool whose swaps broke its own price math is an exploit, never an arbitrage
    pre_prices, moves = track_prices(legs, events.syncs, tracker)
    hooked_keys = {k for k, h in hooks_by_key.items() if h and h != ZERO_ADDRESS}
    for key, excess in invariant_violations(legs, hooked_keys, pre_prices):
        reasons.append(f"pool_invariant_violation:{key}:{excess:.3f}")

    # 5. who gained
    gains = who_gained(transfers, legs, tx_from, tx_to, weth_c)
    profit_token = gains.profit_token
    gross = gains.gross
    economics = economics_from_receipt(receipt, base_fee_per_gas, gross, family)

    # 6. the path and the class
    path = analyze_path(legs)
    reasons = list(path.reasons) + reasons
    gross_delta_raw = gross.get(profit_token) if profit_token else None
    ben = gains.beneficiary
    if path.is_cycle and path.profit_token and path.gain > 0:
        # The cycle's own arithmetic is exact for what it extracted: profit token bought − sold. With a
        # zero-amount hook swap in the path the closure is only approximate (C-CLS-31): keep the path gain,
        # which is at least the right order, and leave gross_unreliable set so the trace pass decides.
        profit_token = path.profit_token
        gross = {profit_token: path.gain}
        if ben is None:
            ben = tx_to
    payouts = gains.payouts
    common = dict(
        n_legs=len(legs),
        tokens=path.tokens,
        venues=[x.key for x in legs],
        kinds=sorted({x.kind for x in legs}),
        dexes=sorted({x.dex for x in legs if x.dex}),
        sequential_chain=path.sequential_chain if legs else None,
        beneficiary=ben,
        profit_token=profit_token,
        gross_profit_raw=gross.get(profit_token) if profit_token else None,
        gross_delta_raw=gross_delta_raw,
        economics=economics.to_dict(),
        price_moves=moves,
        payouts={tok: {k: str(v) for k, v in d.items()} for tok, d in payouts.items()} if payouts else {},
        gross_unreliable=bool(hooked_zero),
    )
    d = decide(legs, path, events, hook_liquidity, profit_token, gross, gains.losses, ben, tx_to, pre_prices)
    return ArbLabel(
        **base,
        **common,
        is_arb=d.is_arb,
        arb_class=d.arb_class,
        reasons=reasons + d.reasons,
        confidence=d.confidence,
        dislocation_evidence=d.evidence,
    )


def gate(tx: dict[str, Any], receipt: dict[str, Any], tx_to: str | None, system_tx_types) -> Decision | None:
    """Reverted, system and probe transactions: classified before any decoding."""
    if to_int(receipt.get("status", "0x1")) == 0:
        return Decision("reverted", reasons=["status_0"])
    # System transactions: OP Stack type 126 (L1 attributes, deposits), Arbitrum/Nitro types 100 and 106
    # (deposits, ArbitrumInternalTx). They can have no external logs but must not inflate bot/probe counts.
    tx_type = to_int(tx.get("type", receipt.get("type")))
    if tx_type in system_tx_types:
        reason = "op_stack_deposit_type_126" if tx_type == 126 else f"system_tx_type_{tx_type}"
        return Decision("system_deposit", reasons=[reason], confidence=1.0)
    # Probe (Wu & Öz 2026): successful, value <= 1 wei, a contract call, no logs from anyone but the bot
    # itself — and the bot's own logs must not be token events (C-CLS-23: a Transfer / Approval emitted by the
    # callee means the callee is a token and the tx a transfer, which made 43 % of Ethereum txs "probes").
    logs = receipt.get("logs") or []
    external_logs = [lg for lg in logs if tx_to is None or addr(lg["address"]) != tx_to]
    own_token_logs = [
        lg for lg in logs if (lg.get("topics") or [""])[0].lower() in (TRANSFER_TOPIC, APPROVAL_TOPIC)
    ]
    gas_used = to_int(receipt.get("gasUsed", "0x0"))
    value = to_int(tx.get("value"))
    if not external_logs and not own_token_logs and value <= 1 and tx_to is not None and gas_used > 21_000:
        return Decision("probe", reasons=["no_external_logs", "value<=1wei"], confidence=0.6)
    return None


def read_events(logs: list[dict[str, Any]], weth: str) -> ReceiptEvents:
    """Decode every log: a swap, a token transfer (a proxy WETH's mint / burn is a wrap, C-CLS-21), a reserve sync,
    or a WETH deposit / withdrawal; liquidity and intent-settlement events are counted."""
    out = ReceiptEvents()
    for log in logs:
        leg = decode_swap_log(log)
        if leg is not None:
            out.legs.append(leg)
            continue
        tr = decode_transfer_log(log)
        if tr is not None:
            out.transfers.extend(weth_mint_burn(tr, weth) or [tr])
            continue
        sy = decode_sync_log(log)
        if sy is not None:
            out.syncs.append(sy)
            continue
        out.transfers.extend(decode_weth_log(log, weth))
        topics = log.get("topics") or []
        if topics and topics[0].lower() in LIQUIDITY_TOPICS:
            out.liquidity_events += 1
        if topics and topics[0].lower() in SETTLEMENT_TOPICS:
            out.settlement_events += 1
    return out


async def complete_legs(
    legs: list[SwapLeg],
    transfers: list[Transfer],
    logs: list[dict[str, Any]],
    resolver: PoolMetaResolver,
    tx_hash: str | None,
    tx_from: str | None,
    tx_to: str | None,
    weth: str,
) -> tuple[list[Transfer], dict[str, str], int, list[str]]:
    """Fill each leg's tokens, correct V4 legs for hook cuts, and add the native ETH a V4 pool settled without an
    event. Returns (transfers with ETH ≡ WETH and the synthetic native transfers, hook per leg key, number of hooked
    zero-amount V4 legs, reasons)."""
    hooks_by_key: dict[str, str] = {}
    unresolved: list[str] = []
    for leg in legs:
        leg.tx_hash = tx_hash
        if leg.token_in and leg.token_out:  # balancer carries its tokens; others need pool metadata
            continue
        meta = None if leg.kind in NO_TOKEN_GETTERS else await resolver.get(leg.pool, leg.pool_id)
        if meta:
            leg.resolve_tokens(meta.token0, meta.token1)
            leg.dex = leg.dex or meta.dex
            if meta.hooks:
                hooks_by_key[leg.key] = addr(meta.hooks)
        elif not infer_tokens_from_transfers(leg, transfers):
            unresolved.append(f"{leg.kind}:{leg.pool[:10]}")

    # hooks that keep a slice of a V4 swap as a claim: correct the leg amounts before anything reads them
    hook_claims = apply_hook_claims(legs, logs, {tx_from, tx_to}, {h for h in hooks_by_key.values() if h})
    reasons = [f"v4_hook_claims:{hook_claims}"] if hook_claims else []
    # hooks paid by the PoolManager directly (fee events, PM -> non-party transfers): C-CLS-30
    pools = {x.pool for x in legs}
    senders = {t.sender for t in transfers}
    party = {a for a in (tx_from, tx_to) if a}
    party |= {x.sender for x in legs if x.sender} | {x.recipient for x in legs if x.recipient}
    party |= {t.sender for t in transfers if t.receiver in pools}
    party |= {t.receiver for t in transfers if t.sender in pools and t.receiver in senders}
    reasons += apply_hook_cuts(legs, logs, transfers, party, pools, hooks_by_key)

    synthetic: list[Transfer] = []
    hooked_zero = 0
    for leg in legs:
        if leg.kind == "clmm_v4_singleton" and leg.amount_in == 0 and leg.amount_out == 0:
            hooked_zero += 1  # hook executed the swap: the event carries no amounts, settle is invisible
        # V4 native currency: settle/take are internal ETH calls with no event. The Swap event still
        # states the amounts, so synthesise the two transfers between the locker and the PoolManager.
        if leg.kind == "clmm_v4_singleton" and leg.sender:
            if leg.token_in == ZERO_ADDRESS and leg.amount_in > 0:
                synthetic.append(Transfer(weth, leg.sender, leg.pool, leg.amount_in, leg.log_index))
            if leg.token_out == ZERO_ADDRESS and leg.amount_out > 0:
                synthetic.append(Transfer(weth, leg.pool, leg.sender, leg.amount_out, leg.log_index))
        # ETH ≡ WETH for path and delta logic (pool prices are unaffected)
        leg.token_in = canonical_token(leg.token_in, weth)
        leg.token_out = canonical_token(leg.token_out, weth)
    transfers = [
        Transfer(canonical_token(t.token, weth), t.sender, t.receiver, t.value, t.log_index)
        for t in transfers
    ] + synthetic

    if unresolved:
        reasons.append("unresolved_venue_tokens:" + ",".join(unresolved[:4]))
    if synthetic:
        reasons.append("v4_native_synthetic_transfers")
    if hooked_zero:
        reasons.append("hooked_v4_zero_amounts")  # gross from events is unreliable; trace pass decides
    return transfers, hooks_by_key, hooked_zero, reasons


def track_prices(
    legs: list[SwapLeg], syncs: list[tuple[str, int, int]], tracker: PoolPriceTracker | None
) -> tuple[dict[str, float], dict[str, int]]:
    """Pre-trade log prices of the legs' pools (before this transaction), then advance the tracker in log order and
    record the direction (+1 / -1) each pool's price moved. ({}, {}) without a tracker."""
    pre_prices: dict[str, float] = {}
    moves: dict[str, int] = {}
    if tracker is None:
        return pre_prices, moves
    for leg in legs:
        lp = tracker.get(leg.key)
        if lp is not None and leg.key not in pre_prices:
            pre_prices[leg.key] = lp
    for leg in sorted(legs, key=lambda x: x.log_index):
        pre, post = tracker.apply_leg(leg)
        sgn = price_move_sign(pre, post)
        if sgn:
            moves[leg.key] = sgn
    for pool, r0, r1 in syncs:
        pre = tracker.get(pool)
        tracker.apply_sync(pool, r0, r1)
        sgn = price_move_sign(pre, tracker.get(pool))
        if sgn:
            moves[pool] = sgn
    return pre_prices, moves


def who_gained(
    transfers: list[Transfer], legs: list[SwapLeg], tx_from: str | None, tx_to: str | None, weth: str
) -> Gains:
    """The callee's net plus what it paid onward (netted per counterparty so loans and round trips vanish); when the
    callee kept nothing, the address among (callee, sender) that ended positive."""
    deltas = net_deltas(transfers)
    exclude = {x.pool for x in legs} | {t.token for t in transfers} | {weth}
    payouts = net_payouts(transfers, tx_to, exclude) if tx_to else {}
    callee_net = dict(deltas.get(tx_to, {})) if tx_to else {}
    before_payouts = {tok: callee_net.get(tok, 0) + sum(v.values()) for tok, v in payouts.items()}
    for tok, v in callee_net.items():
        before_payouts.setdefault(tok, v)
    if tx_to and any(v > 0 for v in before_payouts.values()):
        ben, ben_delta = tx_to, {k: v for k, v in before_payouts.items() if v != 0}
    else:
        ben, ben_delta = beneficiary(deltas, [tx_to, tx_from], exclude)
    return Gains(
        beneficiary=ben,
        gross={k: v for k, v in ben_delta.items() if v > 0},
        losses={k: v for k, v in ben_delta.items() if v < 0},
        payouts=payouts,
    )


def decide(
    legs: list[SwapLeg],
    path: PathCheck,
    events: ReceiptEvents,
    hook_liquidity: bool,
    profit_token: str | None,
    gross: dict[str, int],
    losses: dict[str, int],
    ben: str | None,
    tx_to: str | None,
    pre_prices: dict[str, float],
) -> Decision:
    """The class, by structure (never by address): exclusions first, then the cycle with its evidence."""
    if not legs:
        return Decision("no_swaps")
    if any(not (x.token_in and x.token_out) for x in legs) and len(legs) > 1:
        # a leg we cannot read may close or break the loop: never call this a cycle either way
        return Decision("unknown", reasons=["unresolved_leg_blocks_verdict"], confidence=0.2)
    reasons: list[str] = []
    if events.liquidity_events > 0 and hook_liquidity:
        # the pool's own fee collect, auto-compound or range rebalance while serving the swap (Robinhood 84259655 #4,
        # 70067057 #13; BSC 83600063 #2; Ethereum 26150007 #441): not the operator's position
        reasons.append("liquidity_changes_inside_hook_callbacks")
    elif events.liquidity_events > 0:
        return Decision("lp_action", reasons=["same_tx_liquidity_event"], confidence=0.7)
    if profit_token and profit_token in {x.pool for x in legs}:
        return Decision(
            "lp_action", reasons=reasons + ["profit_token_is_pool"], confidence=0.7
        )  # LP token gain
    if len(legs) == 1:
        return Decision("single_swap", reasons=reasons, confidence=0.9)
    if path.is_cycle and len({x.key for x in legs}) < 2:
        # same pool both ways: wash / volume farming, never an arb
        return Decision("round_trip", reasons=reasons + ["single_venue_loop"], confidence=0.9)
    if path.is_cycle and events.settlement_events > 0:
        return Decision("settlement_like", reasons=reasons + ["settlement_fill_event"], confidence=0.8)
    if path.is_cycle:
        return decide_cycle(legs, path, gross, losses, ben, tx_to, pre_prices, reasons)
    if path.unconsumed_outputs or path.unsourced_inputs:
        return Decision("split_route", reasons=reasons, confidence=0.6)
    return Decision("unknown", reasons=reasons)


def decide_cycle(
    legs: list[SwapLeg],
    path: PathCheck,
    gross: dict[str, int],
    losses: dict[str, int],
    ben: str | None,
    tx_to: str | None,
    pre_prices: dict[str, float],
    reasons: list[str],
) -> Decision:
    """A closed loop through two or more venues: an atomic cycle when someone kept a gain, else an attempt."""
    ordered = sorted(legs, key=lambda x: x.log_index)
    pre_prod = cycle_pre_log_product(ordered, pre_prices)
    evidence = {
        "continuity_gap": path.worst_gap,
        "pre_log_product": pre_prod,
        "realized_log_product": realized_log_product(ordered),
        "pre_prices_known": len(pre_prices),
        "legs": len(legs),
    }
    nonpositive_pre = pre_prod is not None and pre_prod <= 0
    if nonpositive_pre:
        reasons = reasons + ["nonpositive_pre_cycle"]
    if losses:
        reasons = reasons + ["beneficiary_mixed_sign"]  # gained one token, lost another: check by hand
    if path.lost or ben is None or not gross:
        if ben is None or not gross:
            reasons = reasons + ["no_beneficiary"]  # V4 / ERC-6909 blindness, or a losing loop
        return Decision("attempted_cycle", reasons=reasons, confidence=0.6, evidence=evidence)
    confidence = 0.6 + 0.25  # a beneficiary kept a gain
    if path.sequential_chain:
        confidence += 0.1
    if ben == tx_to:
        confidence += 0.05
    if nonpositive_pre:
        confidence -= 0.2
    if losses:
        confidence -= 0.15
    return Decision(
        "atomic_cycle",
        is_arb=True,
        confidence=max(min(confidence, 0.99), 0.1),
        reasons=reasons,
        evidence=evidence,
    )
