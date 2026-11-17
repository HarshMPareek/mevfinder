"""Cycles run inside another party's transaction (an operator contract called as a helper, a hook executing a loop
during the user's swap): each is verified on its own sub-trace."""

from __future__ import annotations

from typing import Any

from mevlab.decode.signatures import NATIVE_ETH
from mevlab.hexutil import addr
from mevlab.trace.calls import callees, hook_machinery
from mevlab.trace.model import TraceVerdict
from mevlab.trace.movements import movements, nets, terminal_receivers
from mevlab.trace.route import returning_route
from mevlab.trace.verdict import trace_verdict


def embedded_cycles(
    trace: dict[str, Any],
    weth: str,
    logs: list[dict[str, Any]],
    primary: TraceVerdict,
    venue_hints: set[str] | None = None,
    blocked: set[str] | None = None,
) -> list[TraceVerdict]:
    """Inspect closed secondary operators without assigning their gains to the user.

    Hooks can execute a self-funded cycle inside an ordinary user swap. A net-positive
    executor may look like a venue from the user's perspective, so use it as a separate
    seed and require a closed portfolio and an actual returning route.

    A pool's hook or a helper it calls (`hook_machinery`) is a secondary operator only when it paid for every leg of
    its route itself: a fee-and-liquidity helper's "route" closes through the user's own swap (Robinhood/Base helpers
    0x9982538f, 0xbf4195ab, 2026-11-01: the user's 2.01 WETH buy as the first leg, 0.0019 WETH paid by the helper),
    while a hook that flash-borrows and sells its pool against an outside pool pays every leg (Base fixture
    `embedded_cycle`: 400 USDC flash loan, V4 pool -> V3 0xd0b53d92, +0.181 USDC).
    """
    moves, reverted = movements(trace, weth, logs)
    if reverted:
        return []
    balances = nets(moves)
    called = callees(trace)
    roots = {addr(trace.get("from", "0x" + "0" * 40)), addr(trace.get("to", "0x" + "0" * 40))}
    # an undecoded contract the transaction's own parties hand their tokens to is their executor, never a market on a
    # secondary operator's route: the helper of a 25.6 WETH multi-venue sale closed as an "embedded cycle" whose last
    # venue was the executor that fed it (Base 52340882 #214). A decoded pool the user trades on stays a venue.
    hints = {addr(h) for h in (venue_hints or set()) if h}
    own = roots | set(primary.group) | ({m.receiver for m in moves if m.sender in roots} - hints)
    excluded = roots | set(primary.group) | {m.token for m in moves} | terminal_receivers(moves)
    machinery = hook_machinery(trace)

    def same(t: str) -> str:
        return addr(weth) if t == NATIVE_ETH else t

    def self_funded(candidate) -> bool:
        """Every leg of the route paid (at least half its input) by the candidate's own group."""
        group = set(candidate.group)
        for leg in candidate.cycle_path or []:
            token, venue, need = same(addr(leg["token_in"])), addr(leg["venue"]), int(leg["amount_in"])
            paid = sum(
                m.amount
                for m in moves
                if same(m.token) == token and m.receiver == venue and m.sender in group
            )
            if paid * 2 < need:
                return False
        return True

    output, assigned = [], set()
    for actor, net in balances.items():
        if actor in excluded or actor in assigned or actor not in called:
            continue
        if not any(v > 0 for v in net.values()) or any(v < 0 for v in net.values()):
            continue
        touched = {m.token for m in moves if actor in (m.sender, m.receiver)}
        if len(touched) < 2:
            continue
        candidate = trace_verdict(trace, weth, actor, actor, logs=logs)
        if candidate.verdict != "cycle" or roots.intersection(candidate.group):
            continue
        if blocked and (actor in blocked or set(candidate.group) & {addr(b) for b in blocked}):
            # a declared RFQ settlement or order settlement is a venue of someone's fill, never a secondary operator:
            # a user's 0x Settler swap sold to a Bebop maker had closed as the Settler's "embedded cycle" with the
            # maker as its venue and the Settler's 0.0000249 ETH fee as the gain (Base 52340002 #58)
            continue
        candidate.cycle_path = returning_route(candidate.legs, candidate.profit_token)
        if not candidate.cycle_path or any(v < 0 for v in candidate.group_asset_deltas.values()):
            continue
        if machinery & ({actor} | set(candidate.group)) and not self_funded(candidate):
            continue  # a hook's fee/liquidity helper closing its "route" through the user's own swap
        if own.intersection(p["venue"] for p in candidate.cycle_path):
            # the transaction's sender or callee is the primary party, never a market on a secondary operator's
            # route: a user's buy through a fee-taking router chain closed as an "embedded cycle" whose last venue
            # was the buyer's own contract and whose gain was the router's fee (BSC 126640601 #54). A hook that
            # merely takes the user's input through the callee keeps its route through real pools.
            continue
        candidate.notes.append("secondary_operator_in_transaction")
        output.append(candidate)
        assigned.update(candidate.group)
    return output
