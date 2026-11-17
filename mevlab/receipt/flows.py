"""Layer 1 (deltas): net per-address, per-token balance changes from transfers. Used ONLY for the
beneficiary's net delta; legs come from protocol events (see legs/structure)."""

from __future__ import annotations

from collections import defaultdict

from mevlab.decode.model import Transfer

Deltas = dict[str, dict[str, int]]


def net_deltas(transfers: list[Transfer]) -> Deltas:
    d: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for t in transfers:
        d[t.sender][t.token] -= t.value
        d[t.receiver][t.token] += t.value
    return {a: {k: v for k, v in m.items() if v != 0} for a, m in d.items() if any(m.values())}


def beneficiary(
    deltas: Deltas, candidates: list[str | None], exclude: set[str] | None = None
) -> tuple[str | None, dict[str, int]]:
    """Pick the beneficiary: tx.to first, then tx.from; if neither gained, fall back to any address
    with a pure gain (>=1 positive delta, no negative) that is not a venue or token contract
    (bots often sweep profit to a separate collector). Returns (address, delta-map)."""
    excl = {a.lower() for a in (exclude or set())}

    def score(addr: str) -> tuple[int, int] | None:
        m = deltas.get(addr, {})
        pos = sum(1 for v in m.values() if v > 0)
        neg = sum(1 for v in m.values() if v < 0)
        return None if pos == 0 else (1 if neg == 0 else 0, pos)

    best: tuple[tuple[int, int], str] | None = None
    for c in candidates:
        if not c:
            continue
        sc = score(c.lower())
        if sc and (best is None or sc > best[0]):
            best = (sc, c.lower())
    if best is None:
        cand_set = {c.lower() for c in candidates if c}
        for a in sorted(deltas):
            if a in excl or a in cand_set:
                continue
            sc = score(a)
            if sc and sc[0] == 1 and (best is None or sc > best[0]):
                best = (sc, a)
    if best is None:
        return None, {}
    return best[1], deltas.get(best[1], {})


def net_payouts(transfers: list[Transfer], callee: str, exclude: set[str]) -> dict[str, dict[str, int]]:
    """token -> {counterparty -> outflow − inflow (floored at 0)} for value the callee sent to addresses that
    are not venues / token contracts / infra. Netting per counterparty makes flash-loan repayments and
    round trips vanish (Morpho lends 77k WETH and is repaid 77k WETH → payout 0) while collector and
    kickback payments stay."""
    callee = callee.lower()
    flow: dict[str, dict[str, int]] = {}
    for t in transfers:
        if t.sender == callee and t.receiver != callee and t.receiver not in exclude:
            flow.setdefault(t.token, {})
            flow[t.token][t.receiver] = flow[t.token].get(t.receiver, 0) + t.value
        elif t.receiver == callee and t.sender != callee and t.sender not in exclude:
            flow.setdefault(t.token, {})
            flow[t.token][t.sender] = flow[t.token].get(t.sender, 0) - t.value
    out = {tok: {a: v for a, v in d.items() if v > 0} for tok, d in flow.items()}
    return {tok: d for tok, d in out.items() if d}
