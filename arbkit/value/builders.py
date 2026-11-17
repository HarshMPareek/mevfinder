"""Block-producer economics from a saved report and its cached blocks: what builders / validators took.

Everything is measured on the scanned blocks, nothing is taken from relay data. Per block:
- priority fees: Σ (effective gas price − base fee) × gas used over user transactions (the builder's / sequencer's
  fee income; on BSC the base fee is 0 so this is the whole gas bill, paid to the validator);
- in-transaction builder payments: coinbase transfers and transfers to known builder EOAs found by the trace pass
  (`builder_tip_wei`), summed over traced rows only — untraced transactions may hide more, so the trace coverage
  is reported next to it;
- direct transfers to known builder EOAs visible in the block body (BSC: searchers paying 48 Club / bloXroute by
  plain value transfer), and the builder EOAs' own outgoing transfers (their payment to the validator);
- proposer payment (Ethereum): the block body transactions sent by the fee recipient itself (the builder paying
  the proposer at the end of the block);
- the searchers' side from the classified rows: gross and net USD of the positive cycles and sandwich bundles,
  what they paid the producer (priority + tips) and the share of their gross that represents.

The builder's margin estimate (Ethereum) is priority + observed tips − proposer payment: a lower bound on income
(tips in untraced transactions are invisible) and an exact proposer cost. Relay "builder profit" tables measure
the same quantity from balance changes; the numbers here say where the money came from.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from arbkit.chain.spec import ChainSpec
from arbkit.files import read_json
from mevlab.hexutil import to_int

POSITIVE = {"trace_supported"}


def _usd(wei: int, native_usd: float | None) -> float | None:
    return round(wei / 1e18 * native_usd, 2) if native_usd is not None else None


def measure_builders(spec: ChainSpec, report: dict, data_dir: Path) -> dict:
    rows_by_block: dict[int, list[dict]] = defaultdict(list)
    for row in report["rows"]:
        rows_by_block[row["block_number"]].append(row)
    builder_eoas = spec.recipients_of_kind("builder")
    per_block = []
    for info in sorted(report["blocks"], key=lambda b: b["number"]):
        number = info["number"]
        rows = rows_by_block.get(number, [])
        user = [r for r in rows if not r["fees"]["is_system"]]
        traced = [r for r in user if (r.get("trace") or {}).get("status") == "ok"]
        priority = sum(r["fees"]["priority_fee_wei"] or 0 for r in user)
        base_burned = sum(r["fees"].get("base_fee_wei") or 0 for r in user)
        tips_traced = sum(int(r.get("builder_tip_wei") or 0) for r in traced)
        positives = [r for r in rows if r["assessment"] in POSITIVE]
        backs = [r["sandwich"] for r in rows if (r.get("sandwich") or {}).get("role") == "back"]
        native_usd = next((r.get("native_usd") for r in rows if r.get("native_usd") is not None), None)
        searcher_gross = sum(r.get("gross_usd") or 0 for r in positives) + sum(
            s.get("profit_usd") or 0 for s in backs
        )
        searcher_net = sum(r.get("net_usd") or 0 for r in positives if r.get("net_usd") is not None) + sum(
            s.get("net_usd") or 0 for s in backs if s.get("net_usd") is not None
        )
        searcher_tips = sum(int(r.get("builder_tip_wei") or 0) for r in positives) + sum(
            int(s.get("bundle_builder_tip_wei") or 0) for s in backs
        )
        searcher_priority = sum(r["fees"]["priority_fee_wei"] or 0 for r in positives) + sum(
            rows_by_hash["fees"]["priority_fee_wei"] or 0
            for s in backs
            for rows_by_hash in rows
            if rows_by_hash["tx_hash"] in (s.get("front"), s.get("back"))
        )
        miner = str(info.get("miner") or "").lower()
        proposer_payment = direct_to_builders = builder_outflows = 0
        path = data_dir / "blocks" / f"{number}.json.gz"
        body_seen = False
        if path.exists():
            body_seen = True
            block = read_json(path)["block"]
            miner = str(block.get("miner") or miner).lower()
            for tx in block["transactions"]:
                frm, to, value = (
                    str(tx.get("from", "")).lower(),
                    str(tx.get("to") or "").lower(),
                    to_int(tx.get("value", "0x0")),
                )
                if not value:
                    continue
                if spec.family == "ethereum" and frm == miner and to != miner:
                    proposer_payment += value
                if to in builder_eoas and frm not in builder_eoas:
                    direct_to_builders += value
                if frm in builder_eoas and to not in builder_eoas:
                    builder_outflows += value
        producer_income = priority + tips_traced + direct_to_builders
        per_block.append(
            {
                "number": number,
                "timestamp": info.get("timestamp"),
                "builder": info.get("builder_tag") or None,
                "producer": miner,
                "transactions": len(user),
                "traced": len(traced),
                "native_usd": native_usd,
                "priority_fees_wei": priority,
                "base_fee_burned_wei": base_burned,
                "tips_traced_wei": tips_traced,
                "direct_to_builder_eoas_wei": direct_to_builders,
                "builder_eoa_outflows_wei": builder_outflows,
                "proposer_payment_wei": proposer_payment,
                "body_available": body_seen,
                "producer_income_wei": producer_income,
                "margin_estimate_wei": producer_income - proposer_payment,
                "cycles": len(positives),
                "sandwiches": len(backs),
                "searcher_gross_usd": round(searcher_gross, 2),
                "searcher_net_usd": round(searcher_net, 2),
                "searcher_tips_wei": searcher_tips,
                "searcher_priority_wei": searcher_priority,
                "searcher_paid_usd": _usd(searcher_tips + searcher_priority, native_usd),
            }
        )
    per_builder: dict[str, dict] = {}
    for b in per_block:
        key = b["builder"] or b["producer"]
        agg = per_builder.setdefault(
            key,
            {
                "builder": key,
                "blocks": 0,
                "transactions": 0,
                "traced": 0,
                "priority_fees_wei": 0,
                "tips_traced_wei": 0,
                "direct_to_builder_eoas_wei": 0,
                "proposer_payment_wei": 0,
                "margin_estimate_wei": 0,
                "cycles": 0,
                "sandwiches": 0,
                "searcher_gross_usd": 0.0,
                "searcher_net_usd": 0.0,
                "searcher_paid_wei": 0,
                "income_usd": 0.0,
                "margin_usd": 0.0,
                "searcher_paid_usd": 0.0,
            },
        )
        for k in (
            "transactions",
            "traced",
            "priority_fees_wei",
            "tips_traced_wei",
            "direct_to_builder_eoas_wei",
            "proposer_payment_wei",
            "margin_estimate_wei",
            "cycles",
            "sandwiches",
            "searcher_gross_usd",
            "searcher_net_usd",
        ):
            agg[k] += b[k]
        agg["blocks"] += 1
        agg["searcher_paid_wei"] += b["searcher_tips_wei"] + b["searcher_priority_wei"]
        if b["native_usd"] is not None:
            agg["income_usd"] += b["producer_income_wei"] / 1e18 * b["native_usd"]
            agg["margin_usd"] += b["margin_estimate_wei"] / 1e18 * b["native_usd"]
            agg["searcher_paid_usd"] += (
                (b["searcher_tips_wei"] + b["searcher_priority_wei"]) / 1e18 * b["native_usd"]
            )
    builders = sorted(per_builder.values(), key=lambda a: -a["blocks"])
    for a in builders:
        a["trace_coverage_pct"] = (
            round(100 * a["traced"] / a["transactions"], 1) if a["transactions"] else None
        )
        a["searcher_paid_share_pct"] = (
            round(100 * a["searcher_paid_usd"] / a["searcher_gross_usd"], 1)
            if a["searcher_gross_usd"]
            else None
        )
        for k in ("searcher_gross_usd", "searcher_net_usd", "income_usd", "margin_usd", "searcher_paid_usd"):
            a[k] = round(a[k], 2)
    total_user = sum(b["transactions"] for b in per_block)
    return {
        "chain": spec.name,
        "family": spec.family,
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "report": report.get("created_at"),
        "blocks": len(per_block),
        "trace_coverage_pct": round(100 * sum(b["traced"] for b in per_block) / total_user, 1)
        if total_user
        else None,
        "builders": builders,
        "per_block": per_block,
        "method": (
            "priority fees from receipts; in-tx builder payments from traced rows only (coverage shown); direct transfers to "
            "known builder EOAs and the fee recipient's own transactions from the block body; searcher gross/net from the "
            "classified cycles and sandwich bundles. Margin = priority + tips − proposer payment (Ethereum)."
        ),
    }


def render(console, facts: dict) -> None:
    from rich.table import Table

    console.print(
        f"{facts['chain']}: {facts['blocks']} blocks, trace coverage {facts['trace_coverage_pct']}% of user transactions",
        style="bold",
    )
    table = Table(title="Block producers", expand=True)
    for name in (
        "Builder / producer",
        "Blocks",
        "Priority fees",
        "Tips seen",
        "Direct to builder EOAs",
        "Proposer paid",
        "Margin est.",
        "Income USD",
        "Cycles",
        "Searcher gross USD",
        "Searcher paid USD",
        "Paid / gross",
    ):
        table.add_column(name, justify="right" if name not in ("Builder / producer",) else "left")
    for a in facts["builders"]:
        table.add_row(
            a["builder"][:34],
            str(a["blocks"]),
            f"{a['priority_fees_wei'] / 1e18:.5f}",
            f"{a['tips_traced_wei'] / 1e18:.5f}",
            f"{a['direct_to_builder_eoas_wei'] / 1e18:.5f}",
            f"{a['proposer_payment_wei'] / 1e18:.5f}",
            f"{a['margin_estimate_wei'] / 1e18:.5f}",
            f"{a['income_usd']:,.2f}",
            str(a["cycles"]),
            f"{a['searcher_gross_usd']:,.2f}",
            f"{a['searcher_paid_usd']:,.2f}",
            f"{a['searcher_paid_share_pct']}%" if a["searcher_paid_share_pct"] is not None else "—",
        )
    console.print(table)
    console.print(facts["method"], style="dim")
