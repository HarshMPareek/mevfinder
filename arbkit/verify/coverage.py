"""Recall and decoder-coverage checks over saved reports and their cached blocks.

Two questions, both answered from local data only:
1. Which swap-like venues did the trace layer see whose events we cannot decode? Every address that appears
   as a trace venue (cycle path, trace venues, embedded cycles) is a venue by construction; the topics it
   emitted in those transactions that no decoder recognises are listed by frequency with sample transactions.
   Those are the registry/decoder gaps that could hide cycles when no trace is requested.
2. Does a broader trace schedule find positives the default schedule missed? `compare_reports` restricts both
   reports to their common blocks and lists every transaction whose assessment differs, with the broad
   report's verdict. A positive only the broad scan found is a recall gap of the default schedule.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

from arbkit.files import read_json
from mevlab.decode.signatures import (
    APPROVAL_TOPIC,
    LIQUIDITY_TOPICS,
    SETTLEMENT_TOPICS,
    SYNC_TOPICS,
    TRANSFER_TOPIC,
    WETH_DEPOSIT_TOPIC,
    WETH_WITHDRAWAL_TOPIC,
)
from mevlab.decode.swaps import SWAP_DECODERS
from mevlab.hexutil import addr

KNOWN_NON_SWAP = {
    TRANSFER_TOPIC,
    APPROVAL_TOPIC,
    WETH_DEPOSIT_TOPIC,
    WETH_WITHDRAWAL_TOPIC,
    *SYNC_TOPICS,
    *LIQUIDITY_TOPICS,
    *SETTLEMENT_TOPICS,
    "0x1b3d7edb2e9c0b0e7c525b20aaaef0f5940d2ed71663c7d39266ecafac728859",  # ERC-6909 Transfer
    "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925",  # Approval(address,address,uint256)
    "0x17307eab39ab6107e8899845ad3d59bd9653f200f220920489ca2b5937696c31",  # ApprovalForAll
}
POSITIVE = {"trace_supported", "trace_supported_loss"}


def _venues_of_row(row) -> set[str]:
    out = set()
    trace = row.get("trace") or {}
    for v in trace.get("venues") or []:
        out.add(addr(v))
    for leg in trace.get("cycle_path") or []:
        out.add(addr(leg["venue"]))
    for leg in trace.get("legs") or []:
        out.add(addr(leg["venue"]))
    for cycle in row.get("embedded_cycles") or []:
        for v in cycle.get("venues") or []:
            out.add(addr(v))
    return out


def decoder_gaps(report: dict, data_dir: Path) -> dict:
    """Undecoded topics emitted by trace venues, from the cached receipts of the report's blocks."""
    rows_by_block: dict[int, list[dict]] = defaultdict(list)
    for row in report["rows"]:
        if row.get("trace", {}).get("status") == "ok":
            rows_by_block[row["block_number"]].append(row)
    topics: Counter = Counter()
    emitters: dict[str, Counter] = defaultdict(Counter)
    samples: dict[str, list[str]] = defaultdict(list)
    venues_total: Counter = Counter()
    decoded_venues: set[str] = set()
    undecoded_venues: set[str] = set()
    missing_blocks = []
    for info in report["blocks"]:
        number = info["number"]
        path = data_dir / "blocks" / f"{number}.json.gz"
        if not path.exists():
            missing_blocks.append(number)
            continue
        bundle = read_json(path)
        receipts = {r["transactionHash"].lower(): r for r in bundle["receipts"]}
        for row in rows_by_block.get(number, []):
            venues = _venues_of_row(row)
            if not venues:
                continue
            receipt = receipts.get(row["tx_hash"].lower())
            if not receipt:
                continue
            for venue in venues:
                venues_total[venue] += 1
            seen_decoded = set()
            pending: list[tuple[str, str]] = []
            for log in receipt.get("logs", []):
                emitter = addr(log["address"])
                if emitter not in venues:
                    continue
                t = (log.get("topics") or [None])[0]
                t = t.lower() if t else "log0"
                if t in SWAP_DECODERS or (
                    t == "log0" and emitter == "0x00000000000014aa86c5d3c41765bb24e11bd701"
                ):
                    seen_decoded.add(emitter)
                    continue
                if t in KNOWN_NON_SWAP:
                    continue
                pending.append((t, emitter))
            # Only venues that emitted no decodable swap event in this transaction are decoder gaps; extra events
            # of a decoded pool (fee accounting, oracle updates) are not.
            for t, emitter in pending:
                if emitter in seen_decoded:
                    continue
                topics[t] += 1
                emitters[t][emitter] += 1
                if len(samples[t]) < 3 and row["tx_hash"] not in samples[t]:
                    samples[t].append(row["tx_hash"])
            decoded_venues |= seen_decoded
            undecoded_venues |= venues - seen_decoded
    gaps = [
        {"topic0": t, "logs": n, "emitters": emitters[t].most_common(5), "sample_txs": samples[t]}
        for t, n in topics.most_common(40)
    ]
    return {
        "chain": report.get("chain"),
        "blocks": len(report["blocks"]),
        "missing_cached_blocks": missing_blocks,
        "traced_rows_with_venues": sum(len(v) for v in rows_by_block.values()),
        "distinct_trace_venues": len(venues_total),
        "venues_without_decoded_swap_event": len(undecoded_venues - decoded_venues),
        "undecoded_topics": gaps,
    }


def compare_reports(default: dict, broad: dict) -> dict:
    """Assessment differences on the blocks both reports cover; the broad report is the reference."""
    blocks = {b["number"] for b in default["blocks"]} & {b["number"] for b in broad["blocks"]}
    prior = {r["tx_hash"]: r for r in default["rows"] if r["block_number"] in blocks}
    diffs = []
    only_broad_positive, only_default_positive = [], []
    for row in broad["rows"]:
        if row["block_number"] not in blocks:
            continue
        before = prior.get(row["tx_hash"])
        if before is None:
            continue
        if (
            before["assessment"] != row["assessment"]
            or before["arb_class"] != row["arb_class"]
            or before.get("gross_profit_raw") != row.get("gross_profit_raw")
        ):
            diffs.append(
                {
                    "tx_hash": row["tx_hash"],
                    "block_number": row["block_number"],
                    "tx_index": row["tx_index"],
                    "default": (
                        before["assessment"],
                        before["arb_class"],
                        before.get("gross_profit_raw"),
                        before["trace"]["status"],
                    ),
                    "broad": (
                        row["assessment"],
                        row["arb_class"],
                        row.get("gross_profit_raw"),
                        row["trace"].get("verdict", row["trace"]["status"]),
                    ),
                }
            )
            if row["assessment"] in POSITIVE and before["assessment"] not in POSITIVE:
                only_broad_positive.append(row["tx_hash"])
            if before["assessment"] in POSITIVE and row["assessment"] not in POSITIVE:
                only_default_positive.append(row["tx_hash"])
    return {
        "common_blocks": len(blocks),
        "transactions_compared": len(prior),
        "changed": len(diffs),
        "positives_only_in_broad": only_broad_positive,
        "positives_only_in_default": only_default_positive,
        "default_trace_mode": default.get("trace_mode"),
        "broad_trace_mode": broad.get("trace_mode"),
        "default_traces": sum(
            r["trace"]["status"] == "ok" for r in default["rows"] if r["block_number"] in blocks
        ),
        "broad_traces": sum(
            r["trace"]["status"] == "ok" for r in broad["rows"] if r["block_number"] in blocks
        ),
        "diffs": diffs,
    }
