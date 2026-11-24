"""The route: a chain of legs through distinct venues that starts and ends in one asset."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from mevlab.trace.model import Leg


def returning_route(legs: list[Leg], token: str) -> list[dict[str, Any]]:
    """Find a token route returning to its input through distinct venues.

    Callback settlement order need not equal swap order, so search connectivity.
    This proves route shape only; signed settled balances decide its result.
    """
    edges = [
        leg
        for leg in legs
        if leg.token_in
        and leg.token_out
        and leg.token_in != leg.token_out
        and leg.amount_in > 0
        and leg.amount_out > 0
    ]
    adjacent: dict[str, list[Leg]] = defaultdict(list)
    for leg in edges:
        adjacent[leg.token_in].append(leg)

    def search(current, path, seen):
        for leg in adjacent.get(current, []):
            if any(leg is x for x in path):
                continue
            # The same venue address may host several pools (Uniswap V4 / Infinity singletons): a repeat is
            # allowed only with a different token pair. The same pair again is a same-pool round trip.
            if any(
                x.venue == leg.venue and {x.token_in, x.token_out} == {leg.token_in, leg.token_out}
                for x in path
            ):
                continue
            route = [*path, leg]
            if leg.token_out == token and len(route) >= 2:
                return route
            if leg.token_out not in seen and len(route) < 12:
                found = search(leg.token_out, route, seen | {leg.token_out})
                if found:
                    return found
        return []

    return [
        {
            "venue": leg.venue,
            "token_in": leg.token_in,
            "token_out": leg.token_out,
            "amount_in": str(leg.amount_in),
            "amount_out": str(leg.amount_out),
        }
        for leg in search(token, [], {token})
    ]


def route_surplus(path: list[dict[str, Any]]) -> int:
    """What the route itself produces from its first leg's input: that input carried through every leg at the leg's
    executed rate, minus the input. Equal to last output - first input when each leg is funded by the one before;
    a leg fed from elsewhere contributes only its rate, never the outside amount."""
    value = int(path[0]["amount_in"])
    start = value
    for leg in path:
        value = value * int(leg["amount_out"]) // int(leg["amount_in"])
    return value - start


def continuity_gap(path: list[dict[str, Any]], tol: float = 0.01) -> float | None:
    """Largest relative excess of a leg's input over the previous leg's output along the route (None when every
    leg is funded by the one before it within `tol`). An excess means the operator added inventory mid-route."""
    worst = 0.0
    for prev, nxt in zip(path, path[1:]):
        out_prev, in_next = int(prev["amount_out"]), int(nxt["amount_in"])
        if out_prev > 0 and in_next > out_prev * (1 + tol):
            worst = max(worst, (in_next - out_prev) / out_prev)
    return worst or None
