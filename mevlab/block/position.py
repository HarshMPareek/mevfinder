"""Block-position subclassification (index §6): backrun, top-of-block, top-of-flashblock, mid-block."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TxTouch:
    tx_index: int
    tx_hash: str
    priority_fee_per_gas: int
    # venue key -> price move caused by this tx (+1 up, -1 down, 0 unknown/none)
    pools: dict[str, int] = field(default_factory=dict)
    flashblock_index: int | None = None
    is_system: bool = False  # deposits / L1 attributes (index 0 on Base)


@dataclass
class PositionLabel:
    trigger_type: str  # backrun | top_of_block | top_of_flashblock | mid_block | unknown
    trigger_tx_index: int | None = None
    trigger_pool: str | None = None
    trigger_same_flashblock: bool | None = None
    trigger_price_reversal: bool | None = None
    fee_rank_in_block: int | None = None
    notes: list[str] = field(default_factory=list)


def classify_positions(
    txs: list[TxTouch],
    arb_indices: set[int],
    tob_fee_ratio: float = 0.9,
    tob_rule: str = "fee",
) -> dict[int, PositionLabel]:
    """Assign a position label to every tx index in `arb_indices`.

    Backrun: arb at index i includes pool P; some tx j<i touched P and moved its price the way the arb's
    leg reverses (opposite sign); no tx between j and i touched P. Trigger = most recent such j.
    With unknown directions (0) the adjacency alone decides and `trigger_price_reversal` is None.
    Top-of-block: first non-system tx with priority fee at/near the block max (tob_rule="fee", fee-sorted
    sequencers), or simply the first non-system tx (tob_rule="first": builder-ordered and FCFS chains, where
    the fee is not the ordering signal).
    Top-of-flashblock: first tx of flashblock k>0 with the max fee in that flashblock."""
    by_index = {t.tx_index: t for t in txs}
    order = sorted(by_index)
    user_txs = [i for i in order if not by_index[i].is_system]
    max_fee = max((by_index[i].priority_fee_per_gas for i in user_txs), default=0)
    fee_rank = {
        i: r + 1 for r, i in enumerate(sorted(user_txs, key=lambda i: -by_index[i].priority_fee_per_gas))
    }
    fb_first: dict[int, int] = {}
    fb_max: dict[int, int] = {}
    for i in user_txs:
        fb = by_index[i].flashblock_index
        if fb is None:
            continue
        fb_first.setdefault(fb, i)
        fb_max[fb] = max(fb_max.get(fb, 0), by_index[i].priority_fee_per_gas)

    out: dict[int, PositionLabel] = {}
    for i in sorted(arb_indices):
        me = by_index.get(i)
        if me is None:
            out[i] = PositionLabel("unknown", notes=["tx_index_not_in_block"])
            continue
        lab = PositionLabel("mid_block", fee_rank_in_block=fee_rank.get(i))

        # backrun search: most recent earlier tx that touched one of my pools
        best: tuple[int, str, bool | None] | None = None
        for p, d in me.pools.items():
            for j in reversed([k for k in user_txs if k < i]):
                other = by_index[j]
                if p in other.pools:
                    od = other.pools[p]
                    reversal = None if (d == 0 or od == 0) else (od == -d)
                    creating = reversal is None or reversal
                    if creating and (best is None or j > best[0]):
                        best = (j, p, reversal)
                    break  # nearest toucher only; anything earlier is shadowed
        if best is not None:
            j, p, rev = best
            lab.trigger_type = "backrun"
            lab.trigger_tx_index, lab.trigger_pool = j, p
            lab.trigger_price_reversal = rev
            fj, fi = by_index[j].flashblock_index, me.flashblock_index
            lab.trigger_same_flashblock = (fj == fi) if (fj is not None and fi is not None) else None
            out[i] = lab
            continue

        if (
            user_txs
            and i == user_txs[0]
            and (tob_rule == "first" or (max_fee > 0 and me.priority_fee_per_gas >= tob_fee_ratio * max_fee))
        ):
            lab.trigger_type = "top_of_block"
            out[i] = lab
            continue

        fb = me.flashblock_index
        if (
            fb is not None
            and fb > 0
            and fb_first.get(fb) == i
            and (me.priority_fee_per_gas >= fb_max.get(fb, 0))
        ):
            lab.trigger_type = "top_of_flashblock"
        out[i] = lab
    return out
