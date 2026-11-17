"""Base / OP: estimate flashblock boundaries inside a sealed block from receipts alone.

Base orders transactions by priority fee *at the time of selection* within each ~200 ms flashblock (docs),
so inside a flashblock fees are roughly non-increasing and jump upward at the next flashblock's start. With
10 user flashblocks per block, the 9 largest upward fee jumps are candidate boundaries. This is a HYPOTHESIS
(claim C-CHAIN-5) to be validated against the recorder's (payload_id, index) reconciliation; until then the
output is `slot_index_est`, never `slot_index`."""

from __future__ import annotations

import math


def estimate_boundaries(fees: list[int], n_slots: int = 10, min_jump_ratio: float = 1.5) -> list[int]:
    """Indexes (positions in `fees`) where a new estimated flashblock starts. Position 0 is always a start.
    `fees` are priority fees per gas of the user transactions in block order (system txs excluded)."""
    if len(fees) < 2:
        return [0] if fees else []
    jumps: list[tuple[float, int]] = []
    for i in range(1, len(fees)):
        prev, cur = fees[i - 1], fees[i]
        if cur > prev and prev >= 0:
            ratio = (cur + 1) / (prev + 1)
            if ratio >= min_jump_ratio:
                jumps.append((math.log(ratio), i))
    jumps.sort(reverse=True)
    starts = sorted({0, *[i for _, i in jumps[: max(n_slots - 1, 0)]]})
    return starts


def slot_indexes(fees: list[int], n_slots: int = 10) -> list[int]:
    """Estimated flashblock index (1-based, like Base's user flashblocks 1..10) per transaction position."""
    starts = estimate_boundaries(fees, n_slots)
    out = []
    slot = 0
    for i in range(len(fees)):
        if i in starts:
            slot += 1
        out.append(slot)
    return out


def monotone_share(fees: list[int], starts: list[int]) -> float:
    """Fraction of adjacent pairs inside estimated slots that are non-increasing (self-consistency)."""
    if len(fees) < 2:
        return 1.0
    starts_set = set(starts)
    ok = tot = 0
    for i in range(1, len(fees)):
        if i in starts_set:
            continue
        tot += 1
        ok += fees[i] <= fees[i - 1]
    return ok / tot if tot else 1.0
