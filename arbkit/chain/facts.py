"""Measured chain mechanics from a saved report and its cached blocks: block cadence, fee behaviour, ordering.

Every number here is measured on the scanned blocks, not taken from documentation. The ordering checks are
heuristics: `fee_monotone_share` is the share of adjacent user-transaction pairs whose priority fee does not
increase (1.0 on a strictly fee-sorted block, lower where flashblocks or arrival order interleave);
`priority_paying_share` is the share of user transactions paying any priority fee at all (≈ 0 on FCFS chains
where tips buy nothing).
"""

from __future__ import annotations

import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from arbkit.chain.spec import ChainSpec
from arbkit.files import read_json
from mevlab.hexutil import to_int


def measure(spec: ChainSpec, report: dict, data_dir: Path) -> dict:
    blocks = sorted(report["blocks"], key=lambda b: b["number"])
    intervals = [
        b["timestamp"] - a["timestamp"] for a, b in zip(blocks, blocks[1:]) if b["number"] == a["number"] + 1
    ]
    rows = report["rows"]
    user = [r for r in rows if not r["fees"]["is_system"]]
    by_block: dict[int, list[dict]] = {}
    for r in user:
        by_block.setdefault(r["block_number"], []).append(r)
    monotone_pairs = total_pairs = 0
    for number, rs in by_block.items():
        rs.sort(key=lambda r: r["tx_index"])
        fees = [r["fees"]["priority_fee_per_gas_wei"] or 0 for r in rs]
        for a, b in zip(fees, fees[1:]):
            total_pairs += 1
            monotone_pairs += b <= a
    tips = [r for r in rows if int(r.get("builder_tip_wei") or 0) > 0]
    positives = [r for r in rows if r["assessment"] == "trace_supported"]
    base_fees, gas_used, timestamps_same_second = [], [], Counter()
    for info in blocks:
        path = data_dir / "blocks" / f"{info['number']}.json.gz"
        if path.exists():
            block = read_json(path)["block"]
            if block.get("baseFeePerGas") is not None:
                base_fees.append(to_int(block["baseFeePerGas"]))
            gas_used.append(to_int(block.get("gasUsed", "0x0")))
            timestamps_same_second[to_int(block["timestamp"])] += 1
    positions = Counter(r.get("trigger_type") for r in positives)
    reversal = Counter({"confirmed": 0, "unknown_direction": 0, "same_direction": 0})
    for r in positives:
        if r.get("trigger_type") == "backrun":
            value = r.get("trigger_price_reversal")
            reversal[
                "confirmed" if value is True else "same_direction" if value is False else "unknown_direction"
            ] += 1
    slots = Counter(r.get("slot_index_est") for r in positives if r.get("slot_index_est") is not None)
    consistency = [
        r["slot_estimate_consistency"] for r in rows if r.get("slot_estimate_consistency") is not None
    ]
    distances = Counter(
        r.get("trigger_distance_blocks") for r in positives if r.get("trigger_type") == "backrun"
    )
    kinds = Counter(
        k for r in positives for k in (r.get("event_label", {}).get("kinds") or r.get("kinds") or [])
    )
    pct = lambda a, b: round(100 * a / b, 1) if b else None  # noqa: E731
    return {
        "chain": spec.name,
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "report": report.get("created_at"),
        "blocks": len(blocks),
        "transactions": len(rows),
        "block_interval_s": {
            "median": statistics.median(intervals) if intervals else None,
            "mean": round(statistics.mean(intervals), 3) if intervals else None,
            "configured_ms": spec.block_time_ms,
            "blocks_per_timestamp_second_max": max(timestamps_same_second.values())
            if timestamps_same_second
            else None,
        },
        "transactions_per_block": {
            "median": statistics.median(len(v) for v in by_block.values()) if by_block else None,
            "max": max((len(v) for v in by_block.values()), default=None),
        },
        "base_fee_gwei": {
            "min": min(base_fees) / 1e9 if base_fees else None,
            "median": statistics.median(base_fees) / 1e9 if base_fees else None,
            "max": max(base_fees) / 1e9 if base_fees else None,
        },
        "priority_paying_share_pct": pct(
            sum(1 for r in user if (r["fees"]["priority_fee_per_gas_wei"] or 0) > 0), len(user)
        ),
        "fee_monotone_share_pct": pct(monotone_pairs, total_pairs),
        "builder_tips": {
            "transactions": len(tips),
            "total_native": sum(int(r["builder_tip_wei"]) for r in tips) / 1e18,
            "cycles_paying": sum(1 for r in positives if int(r.get("builder_tip_wei") or 0) > 0),
        },
        "kickbacks_native": sum(int(r.get("kickback_wei") or 0) for r in rows) / 1e18,
        "timeboosted_transactions": sum(1 for r in rows if r["fees"].get("timeboosted")),
        "system_transactions": sum(1 for r in rows if r["fees"]["is_system"]),
        "reverted_share_pct": pct(sum(1 for r in user if r["arb_class"] == "reverted"), len(user)),
        "positive_cycles": len(positives),
        "cycles_per_block": round(len(positives) / len(blocks), 3) if blocks else None,
        "blocks_with_cycles_pct": pct(len({r["block_number"] for r in positives}), len(blocks)),
        "positions": dict(positions),
        "backrun_trigger_distance_blocks": {
            str(k): v for k, v in sorted(distances.items(), key=lambda kv: (kv[0] is None, kv[0]))
        },
        "backrun_price_reversal": dict(reversal),
        "cycles_by_estimated_flashblock": {str(k): v for k, v in sorted(slots.items())},
        "flashblock_estimate_consistency": round(statistics.mean(consistency), 4) if consistency else None,
        "cycle_index_median": statistics.median(r["tx_index"] for r in positives) if positives else None,
        "venue_kinds_in_cycles": dict(kinds.most_common()),
        "sandwiches": sum(1 for r in rows if r.get("sandwich", {}).get("role") == "back"),
        "embedded_cycles": sum(1 for r in positives if r.get("operator_scope") == "embedded"),
        "ordering_policy_configured": spec.ordering_policy,
    }


def render(console, facts: dict) -> None:
    bi = facts["block_interval_s"]
    console.print(
        f"{facts['chain']}: {facts['blocks']} blocks, {facts['transactions']:,} transactions", style="bold"
    )
    console.print(
        f"Block interval: median {bi['median']} s, mean {bi['mean']} s (configured {bi['configured_ms']} ms); "
        f"up to {bi['blocks_per_timestamp_second_max']} blocks share one timestamp second.",
        markup=False,
    )
    console.print(
        f"Txs/block median {facts['transactions_per_block']['median']}, max {facts['transactions_per_block']['max']}; "
        f"base fee gwei min/median/max {facts['base_fee_gwei']['min']}/{facts['base_fee_gwei']['median']}/{facts['base_fee_gwei']['max']}",
        markup=False,
    )
    console.print(
        f"Priority-paying user txs: {facts['priority_paying_share_pct']}% | fee-monotone adjacent pairs: {facts['fee_monotone_share_pct']}% "
        f"| reverted: {facts['reverted_share_pct']}% | system txs: {facts['system_transactions']} | timeboosted: {facts['timeboosted_transactions']}",
        markup=False,
    )
    t = facts["builder_tips"]
    console.print(
        f"Builder/validator payments inside txs: {t['transactions']} txs, {t['total_native']:.6f} native ({t['cycles_paying']} cycles paying); "
        f"kickbacks to known recipients: {facts['kickbacks_native']:.6f} native",
        markup=False,
    )
    console.print(
        f"Cycles: {facts['positive_cycles']} ({facts['cycles_per_block']} per block, {facts['blocks_with_cycles_pct']}% of blocks); "
        f"positions {facts['positions']}; backrun trigger distance (blocks) {facts['backrun_trigger_distance_blocks']}; "
        f"median cycle index {facts['cycle_index_median']}; sandwiches {facts['sandwiches']}; embedded {facts['embedded_cycles']}",
        markup=False,
    )
    console.print(
        f"Backrun price reversal evidence: {facts['backrun_price_reversal']}"
        + (
            f" | cycles by estimated flashblock {facts['cycles_by_estimated_flashblock']} (fee-monotone inside estimated slots {facts['flashblock_estimate_consistency']})"
            if facts["cycles_by_estimated_flashblock"]
            else ""
        ),
        markup=False,
    )
    console.print(f"Venue kinds in cycles: {facts['venue_kinds_in_cycles']}", markup=False)
