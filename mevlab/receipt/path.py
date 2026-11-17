"""Layer 0/1 structural analysis of a transaction's swap legs.

Exclusion is by STRUCTURE, never by address (index §3.2). A closed token loop is necessary but not
sufficient for "arb": settlements and zaps also loop. The dislocation test (Layer 2) is the
discriminator; it lives in dislocation.py and is not implemented yet."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from mevlab.decode.model import SwapLeg


@dataclass
class PathCheck:
    is_cycle: bool  # closed by AMOUNT: every token nets to ~0 except at most one positive (profit) token
    sequential_chain: bool  # legs chain in log order and close: out_i == in_{i+1}, out_last == in_0
    tokens: list[str]
    unconsumed_outputs: list[str]  # bought but never sold (split route / aggregator)
    unsourced_inputs: list[str]  # sold but never bought (funded from inventory)
    reasons: list[str] = field(default_factory=list)
    profit_token: str | None = None  # the single token that nets positive beyond tolerance
    gain: int = 0  # its net (bought − sold), raw units
    unbalanced: list[str] = field(default_factory=list)  # tokens outside tolerance (profit or loss)
    worst_gap: float = 0.0  # largest relative imbalance among the tokens treated as balanced
    lost: bool = False  # cycle structure whose only unbalanced token nets negative (attempted)


TOL = 0.01  # relative imbalance tolerated on pass-through tokens (fee-on-transfer, rounding)


def converted_cycle(
    legs: list[SwapLeg], profit: str, net: dict[str, int], bought: dict[str, int], sold: dict[str, int]
) -> bool:
    """`profit` was only bought, every other token passes through: is the route a closed loop in one token C whose
    surplus was converted into `profit`? Without the conversion legs (C -> profit) the remaining legs must close
    in C with a positive surplus through at least two distinct pools, and the conversions must spend that surplus
    (within TOL). A user's split route never qualifies: its input token is sold, never bought back."""
    if net.get(profit, 0) <= 0 or sold.get(profit, 0):
        return False
    conv = [x for x in legs if x.token_out == profit]
    rest = [x for x in legs if x.token_out != profit]
    inputs = {x.token_in for x in conv}
    if len(inputs) != 1 or len(rest) < 2 or len({x.key for x in rest}) < 2:
        return False
    c = inputs.pop()
    spent = sum(x.amount_in for x in conv)
    r_bought: dict[str, int] = defaultdict(int)
    r_sold: dict[str, int] = defaultdict(int)
    for x in rest:
        r_sold[x.token_in] += x.amount_in
        r_bought[x.token_out] += x.amount_out
    surplus = r_bought[c] - r_sold[c]
    if not r_sold[c] or surplus <= 0 or abs(surplus - spent) > TOL * surplus:
        return False
    for t in set(r_bought) | set(r_sold):
        if t != c and abs(r_bought[t] - r_sold[t]) > TOL * max(r_bought[t], r_sold[t], 1):
            return False
    return True


def analyze_path(legs: list[SwapLeg]) -> PathCheck:
    legs = sorted(legs, key=lambda x: x.log_index)
    resolved = [x for x in legs if x.token_in and x.token_out]
    reasons: list[str] = []
    if len(resolved) < len(legs):
        reasons.append(f"unresolved_legs={len(legs) - len(resolved)}")
    if not resolved:
        return PathCheck(
            is_cycle=False,
            sequential_chain=False,
            tokens=[],
            unconsumed_outputs=[],
            unsourced_inputs=[],
            reasons=reasons + ["no_resolved_legs"],
        )

    bought: dict[str, int] = defaultdict(int)
    sold: dict[str, int] = defaultdict(int)
    net: dict[str, int] = defaultdict(int)
    for x in resolved:
        sold[x.token_in] += x.amount_in
        bought[x.token_out] += x.amount_out
        net[x.token_in] -= x.amount_in
        net[x.token_out] += x.amount_out

    tokens = list(dict.fromkeys([t for x in resolved for t in (x.token_in, x.token_out)]))
    unconsumed = [t for t in bought if t not in sold]
    unsourced = [t for t in sold if t not in bought]

    seq = (
        len(resolved) >= 2
        and all(resolved[i].token_out == resolved[i + 1].token_in for i in range(len(resolved) - 1))
        and resolved[-1].token_out == resolved[0].token_in
    )

    # closure by amount
    unbalanced: list[str] = []
    worst = 0.0
    for t in tokens:
        scale = max(bought[t], sold[t], 1)
        rel = abs(net[t]) / scale
        if rel > TOL:
            unbalanced.append(t)
        else:
            worst = max(worst, rel)
    profit_token: str | None = None
    gain = 0
    lost = False
    is_cycle = False
    if len(resolved) >= 2:
        if not unbalanced:
            is_cycle = True  # everything passes through; gain (if any) is inside tolerance
            pos = {t: v for t, v in net.items() if v > 0 and sold[t] > 0}
            if pos:
                profit_token = max(pos, key=lambda t: pos[t] / max(bought[t], sold[t], 1))
                gain = pos[profit_token]
        elif len(unbalanced) == 1 and sold[unbalanced[0]] > 0 and bought[unbalanced[0]] > 0:
            # the one token that does not pass through must be the entry/exit token (bought AND sold)
            t = unbalanced[0]
            if net[t] > 0:
                is_cycle, profit_token, gain = True, t, net[t]
            else:
                is_cycle, lost = True, True  # cycle geometry, but the entry token lost
                profit_token, gain = t, net[t]
        elif len(unbalanced) == 1 and converted_cycle(resolved, unbalanced[0], net, bought, sold):
            # a closed loop in another token whose surplus was swapped into this one at the end: the profit taken in
            # a different asset (BSC 131019461 #21: USDT -> X -> USDT through two Uniswap V4 pools, +0.012649 USDT
            # converted to WBNB on V3 and paid to Blink and 48 Club had read as a "leftover token" split route)
            is_cycle, profit_token, gain = True, unbalanced[0], net[unbalanced[0]]
            reasons.append("profit_converted")
        elif len(unbalanced) == 1:
            reasons.append("leftover_token")  # bought but never sold (or the reverse): a split, not a loop
        else:
            reasons.append("multi_token_imbalance")  # a swap / split route, not a closed loop
    if len(resolved) == 1:
        reasons.append("single_swap")
    if unconsumed:
        reasons.append("unconsumed_outputs")
    if unsourced:
        reasons.append("unsourced_inputs")
    if is_cycle and not seq:
        reasons.append("cycle_unordered")  # reverse-ordered / interleaved cyclic arbs (§3.2)
    if is_cycle and lost:
        reasons.append("cycle_lost")

    return PathCheck(
        is_cycle=is_cycle,
        sequential_chain=seq,
        tokens=tokens,
        unconsumed_outputs=unconsumed,
        unsourced_inputs=unsourced,
        reasons=reasons,
        profit_token=profit_token,
        gain=gain,
        unbalanced=unbalanced,
        worst_gap=worst,
        lost=lost,
    )
