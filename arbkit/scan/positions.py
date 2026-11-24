"""Block-position models per ordering policy.

fee_sorted (Base, Optimism): the sequencer sorts by priority fee inside flashblocks; a backrun is the arb that
    follows the last other transaction on one of its pools in the same block; top_of_block is the first user
    transaction when it pays the block's maximum fee.
builder (Ethereum, BSC): the builder orders the block; top_of_block is the first user transaction whatever it
    paid (tips go to the coinbase); backruns are same-block as above.
fcfs (Robinhood, Arbitrum): no fee ordering; reactions land in the same block or a few blocks later, so the
    trigger of a cycle is the last other-party transaction on one of its pools within `trigger_window_blocks`,
    searched across the contiguous blocks already scanned.
Any policy with `trigger_window_blocks > 0` gets the same cross-block search for cycles that found no same-block
    trigger (Base / Optimism "next-block" arbitrage: the reaction to a swap sealed in the previous block);
    `trigger_distance_blocks` >= 1 marks those rows.

Every label is an inference from transaction order and pool price directions; no mempool or feed timing is
measured here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from arbkit.chain.spec import ChainSpec
from mevlab.block.flashblocks import estimate_boundaries, monotone_share, slot_indexes
from mevlab.block.position import TxTouch, classify_positions


@dataclass
class Touch:
    block_number: int
    tx_index: int
    tx_hash: str
    tx_from: str | None
    tx_to: str | None
    pools: dict[str, int] = field(default_factory=dict)


class TriggerContext:
    """Rolling memory of the last transaction that touched each venue, across contiguous blocks."""

    def __init__(self, window_blocks: int):
        self.window = window_blocks
        self.last: dict[str, Touch] = {}
        self.previous_block: int | None = None

    def reset(self) -> None:
        self.last.clear()
        self.previous_block = None

    def contiguous(self, block_number: int) -> bool:
        return self.previous_block is not None and block_number == self.previous_block + 1

    def find(self, me: Touch) -> tuple[Touch, str] | None:
        """Most recent earlier-block touch of one of `me`'s pools by another party within the window."""
        best: tuple[Touch, str] | None = None
        for pool in me.pools:
            t = self.last.get(pool)
            if t is None or t.block_number >= me.block_number:
                continue
            if me.block_number - t.block_number > self.window:
                continue
            if t.tx_to and t.tx_to == me.tx_to:
                continue
            if t.tx_from and t.tx_from == me.tx_from:
                continue
            if best is None or (t.block_number, t.tx_index) > (best[0].block_number, best[0].tx_index):
                best = (t, pool)
        return best

    def record(self, touches: list[Touch]) -> None:
        for t in sorted(touches, key=lambda x: (x.block_number, x.tx_index)):
            for pool in t.pools:
                self.last[pool] = t
            self.previous_block = t.block_number
        if self.window and self.last:
            horizon = max(t.block_number for t in self.last.values()) - self.window
            self.last = {p: t for p, t in self.last.items() if t.block_number >= horizon}


def created_pools(rows: list[dict]) -> dict[int, list[str]]:
    """A transaction that creates a pool sets its first price: the pool counts as touched by it (direction unknown) so the
    arbitrage reacting to it can be linked; the row itself is not changed. Only creation counts (`row['created_pools']`, liquidity.created_pools: a V2 pair's first mint, a
    V3 / V4 / Infinity Initialize); an ordinary add or remove at the pool's price creates no gap and would only hide the
    swap that did. BSC 131952051 #1 created V2 pair 0x9d7afd0d (addLiquidity, MINIMUM_LIQUIDITY burnt) and #2 took
    $1,131.61 through it, which had read as mid-block."""
    out = {}
    for row in rows:
        created = row.get("created_pools") or []
        if created and not row.get("venues"):
            out[row["tx_index"]] = [str(p).lower() for p in created]
    return out


def link_original_triggers(
    rows: list[dict],
    tx_by_index: dict[int, dict],
    block_number: int,
    created: dict[int, list[str]] | None = None,
) -> None:
    """Same-block trigger search that passes over transactions which did not create the opportunity (chain option
    `[ordering] link_original_trigger`): the operator's own earlier transactions (a sibling is never the trigger, and
    `assign_positions` would otherwise stop at it) and other counted cycles that moved the pool the same way as this one
    (a competitor that took the larger share of the same gap first). The search goes on to the transaction that moved
    the pool the other way, as `classify_positions` does for the nearest one. Only rows without a same-block trigger
    change. BSC 2026-11-06/07 (48 h): of the counted mid-block and top-of-block rows whose route an earlier transaction
    of the block had touched, $1,405/day followed another bot's cycle taking the same move first and $441/day the
    operator's own transaction (gaphunt bsc_delayed.txt); both read as mid-block."""
    created = created or {}
    ordered = sorted((r for r in rows if not r["fees"]["is_system"]), key=lambda r: r["tx_index"])

    def pools(r) -> list[str]:
        return list(r.get("venues") or []) or created.get(r["tx_index"], [])

    for i, row in enumerate(ordered):
        if row["assessment"] == "not_arb" or row.get("trigger_type") == "backrun":
            continue
        me_to, me_from = str(row.get("tx_to") or "").lower(), str(row.get("tx_from") or "").lower()
        best = None
        for pool in row.get("venues") or []:
            d = (row.get("price_moves") or {}).get(pool, 0)
            for other in reversed(ordered[:i]):
                if pool not in pools(other):
                    continue
                if (me_to and str(other.get("tx_to") or "").lower() == me_to) or str(
                    other.get("tx_from") or ""
                ).lower() == me_from:
                    continue  # the operator's own transaction
                od = (other.get("price_moves") or {}).get(pool, 0)
                if other["assessment"] != "not_arb" and d and od == d:
                    continue  # a competing cycle that took the same move first
                reversal = None if (d == 0 or od == 0) else od == -d
                if reversal is not False and (best is None or other["tx_index"] > best[0]["tx_index"]):
                    best = (other, pool, reversal)
                break
        if best is None:
            continue
        other, pool, reversal = best
        trigger = tx_by_index.get(other["tx_index"]) or {}
        row["trigger_type"] = "backrun"
        row["trigger_tx_index"], row["trigger_pool"], row["trigger_price_reversal"] = (
            other["tx_index"],
            pool,
            reversal,
        )
        row["trigger_tx_hash"] = trigger.get("hash") or other["tx_hash"]
        row["trigger_block_number"], row["trigger_distance_blocks"] = block_number, 0
        row["reasons"] = [
            *[x for x in row.get("reasons", []) if x != "sibling_trigger_dropped"],
            "original_trigger_linked",
        ]


def assign_positions(
    spec: ChainSpec,
    rows: list[dict],
    tx_by_index: dict[int, dict],
    ctx: TriggerContext | None,
    block_number: int,
) -> None:
    """Set trigger_type / trigger_* fields on every classified row of one block."""
    # Fee-sorted sequencers with flashblocks (Base): estimate the flashblock of every user transaction from the
    # upward priority-fee jumps inside the sealed block. It is an estimate (hypothesis C-CHAIN-5 in the research
    # notes), reported as `slot_index_est`, never as a measured boundary.
    slots: dict[int, int] = {}
    # Base: from block 53,556,127 (2026-11-05) every block is one 200 ms auction, so there are no slots to estimate.
    flashblocks = spec.flashblocks_per_block > 0 and (
        spec.flashblocks_until_block is None or block_number < spec.flashblocks_until_block
    )
    if spec.ordering_policy == "fee_sorted" and flashblocks:
        user = [r for r in sorted(rows, key=lambda r: r["tx_index"]) if not r["fees"]["is_system"]]
        fees = [r["fees"]["priority_fee_per_gas_wei"] or 0 for r in user]
        for r, slot in zip(user, slot_indexes(fees, spec.flashblocks_per_block)):
            slots[r["tx_index"]] = slot
        consistency = monotone_share(fees, estimate_boundaries(fees, spec.flashblocks_per_block))
        for r in rows:
            r["slot_index_est"] = slots.get(r["tx_index"])
            r["slot_estimate_consistency"] = round(consistency, 4)

    def inert_marker(r) -> bool:
        """A builder's zero-value transaction to itself (48 Club puts one at #0 of its BSC blocks): no user, no trade."""
        tx = tx_by_index.get(r["tx_index"]) or {}
        sender, to = str(tx.get("from") or "").lower(), str(tx.get("to") or "").lower()
        return (
            bool(sender)
            and sender == to
            and not int(str(tx.get("value") or "0x0"), 16)
            and not r.get("n_legs")
        )

    created = created_pools(rows) if spec.link_original_trigger else {}
    touches = [
        TxTouch(
            r["tx_index"],
            r["tx_hash"],
            r["fees"]["priority_fee_per_gas_wei"] or 0,
            {v: r["price_moves"].get(v, 0) for v in (r["venues"] or created.get(r["tx_index"], []))},
            flashblock_index=slots.get(r["tx_index"]),
            is_system=r["fees"]["is_system"] or inert_marker(r),
        )
        for r in rows
    ]
    arb_indices = {r["tx_index"] for r in rows if r["assessment"] != "not_arb"}
    tob_rule = "fee" if spec.ordering_policy == "fee_sorted" else "first"
    positions = classify_positions(touches, arb_indices, tob_rule=tob_rule)
    for row in rows:
        row["trigger_block_number"] = None
        row["trigger_distance_blocks"] = None
        row["trigger_tx_hash"] = None
        pos = positions.get(row["tx_index"])
        if not pos:
            continue
        row["trigger_type"] = pos.trigger_type
        row["trigger_tx_index"] = pos.trigger_tx_index
        row["trigger_pool"] = pos.trigger_pool
        row["trigger_price_reversal"] = pos.trigger_price_reversal
        row["fee_rank_in_block"] = pos.fee_rank_in_block
        row["trigger_same_flashblock"] = pos.trigger_same_flashblock
        row["tof_est"] = pos.trigger_type == "top_of_flashblock"
        trigger = tx_by_index.get(pos.trigger_tx_index) if pos.trigger_tx_index is not None else None
        if trigger and pos.trigger_type == "backrun":
            # the operator's own earlier transaction (same bot contract or same wallet) cannot be what it backran
            # (OP 0x9e18cf33's "trigger" was a sibling cycle of the same bot 0xd559a1ad — manual review 2026-10-31)
            same_bot = str(trigger.get("to") or "").lower() == str(
                row.get("tx_to") or ""
            ).lower() and row.get("tx_to")
            same_wallet = str(trigger.get("from") or "").lower() == str(row.get("tx_from") or "").lower()
            if same_bot or same_wallet:
                row["trigger_type"] = "mid_block"
                row["trigger_tx_index"] = row["trigger_pool"] = row["trigger_price_reversal"] = None
                row["reasons"] = [*row.get("reasons", []), "sibling_trigger_dropped"]
                trigger = None
        if trigger:
            row["trigger_tx_hash"] = trigger["hash"]
            row["trigger_block_number"] = block_number
            row["trigger_distance_blocks"] = 0
    if spec.link_original_trigger:
        link_original_triggers(rows, tx_by_index, block_number, created)
    if ctx is None or spec.trigger_window_blocks <= 0:
        return
    block_touches = [
        Touch(
            block_number,
            r["tx_index"],
            r["tx_hash"],
            r.get("tx_from"),
            r.get("tx_to"),
            {v: r["price_moves"].get(v, 0) for v in r["venues"]},
        )
        for r in rows
        if not r["fees"]["is_system"]
    ]
    if ctx.contiguous(block_number) or ctx.previous_block is None:
        for row in rows:
            if row["assessment"] == "not_arb" or row.get("trigger_tx_hash"):
                continue
            me = next(t for t in block_touches if t.tx_index == row["tx_index"])
            found = ctx.find(me)
            if found is None:
                continue
            trigger, pool = found
            row["trigger_type"] = "backrun"
            row["trigger_tx_hash"] = trigger.tx_hash
            row["trigger_tx_index"] = trigger.tx_index
            row["trigger_pool"] = pool
            row["trigger_block_number"] = trigger.block_number
            row["trigger_distance_blocks"] = block_number - trigger.block_number
            row["trigger_price_reversal"] = None
            row["reasons"] = [*row["reasons"], f"cross_block_trigger:{block_number - trigger.block_number}"]
    else:
        ctx.reset()
    ctx.record(block_touches)
