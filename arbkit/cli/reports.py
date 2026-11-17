"""Shared by the commands: which chain, run a scan, save a report (JSON + CSV)."""

from __future__ import annotations

import asyncio
import csv
import json
import os
from datetime import datetime, timezone

from arbkit.chain.settings import DEFAULT_CHAIN
from arbkit.files import write_json
from arbkit.scan.scanner import Scanner
from arbkit.value.proceeds import enrich_report

CSV_COLUMNS = [
    "chain",
    "block_number",
    "tx_index",
    "tx_hash",
    "explorer_url",
    "assessment",
    "arb_class",
    "mev_type",
    "is_arb",
    "n_legs",
    "trigger_type",
    "trigger_tx_hash",
    "trigger_block_number",
    "trigger_distance_blocks",
    "trigger_price_reversal",
    "slot_index_est",
    "block_producer",
    "block_builder_tag",
    "refund_to_trigger_wei",
    "profit_token",
    "gross_profit_raw",
    "profit_symbol",
    "gross_profit",
    "gross_source",
    "gross_usd",
    "gross_native",
    "price_source",
    "fee_usd",
    "builder_tip_wei",
    "builder_tip_usd",
    "kickback_wei",
    "kickback_usd",
    "net_usd",
    "fee_coverage",
    "gross_minus_network_fee_wei",
    "retained_before_gas_raw",
    "forwarded_profit_raw",
    "forwarded_proceeds_json",
    "forwarded_claims_json",
    "trace_asset_deltas_json",
    "trace_group_deltas_json",
    "cycle_path_json",
    "venue_terminal_receipts_json",
    "liquidity_json",
    "operator_scope",
    "embedded_cycles_json",
    "sandwich_json",
    "retained_minus_network_fee_wei",
    "net_after_gas_wei",
    "known_recipient_received_wei",
    "user_operations_json",
    "trace_status",
    "trace_verdict",
    "gas_used",
    "effective_gas_price_wei",
    "priority_fee_per_gas_wei",
    "max_priority_fee_per_gas_wei",
    "priority_fee_wei",
    "base_fee_wei",
    "execution_fee_wei",
    "l1_fee_wei",
    "operator_fee_wei",
    "total_fee_wei",
    "total_fee_eth",
]


def save_report(report, directory, stem=None):
    enrich_report(report)
    numbers = report["requested_blocks"]
    chain = report.get("chain", "base")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = directory / f"{stem or f'{chain}_{numbers[0]}-{numbers[-1]}'}_{stamp}.json"
    write_json(path, report)
    with path.with_suffix(".csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in report["rows"]:
            flat = {
                **row,
                **row["fees"],
                "chain": chain,
                "trace_status": row["trace"]["status"],
                "trace_verdict": row["trace"].get("verdict"),
            }
            flat["forwarded_proceeds_json"] = json.dumps(row.get("forwarded_proceeds", {}), sort_keys=True)
            flat["forwarded_claims_json"] = json.dumps(row.get("forwarded_claims", []), sort_keys=True)
            flat["trace_asset_deltas_json"] = json.dumps(row["trace"].get("asset_deltas", {}), sort_keys=True)
            flat["trace_group_deltas_json"] = json.dumps(
                row["trace"].get("group_asset_deltas", {}), sort_keys=True
            )
            flat["cycle_path_json"] = json.dumps(row["trace"].get("cycle_path", []), sort_keys=True)
            flat["venue_terminal_receipts_json"] = json.dumps(
                row["trace"].get("venue_terminal_receipts", []), sort_keys=True
            )
            flat["liquidity_json"] = json.dumps(row.get("liquidity", {}), sort_keys=True)
            flat["embedded_cycles_json"] = json.dumps(row.get("embedded_cycles", []), sort_keys=True)
            flat["sandwich_json"] = json.dumps(row.get("sandwich", {}), sort_keys=True)
            flat["user_operations_json"] = json.dumps(row.get("user_operations", []), sort_keys=True)
            safe = {}
            for key in CSV_COLUMNS:
                value = flat.get(key)
                # Untrusted token symbols must not become spreadsheet formulas.
                if (
                    key == "profit_symbol"
                    and isinstance(value, str)
                    and value.startswith(("=", "+", "-", "@"))
                ):
                    value = "'" + value
                safe[key] = value
            writer.writerow(safe)
    write_json(directory / "latest.json", report)
    return path


def chain_name(args):
    return args.chain or os.environ.get("ARB_CHAIN") or DEFAULT_CHAIN


def run_scan(settings, console, blocks, args, focus=None, extra=None):
    scanner = Scanner(
        settings,
        lambda text: console.print(text, markup=False, style="dim"),
        offline_prices=getattr(args, "offline_prices", False),
    )
    return asyncio.run(scanner.run(blocks, args.traces, args.refresh, focus, extra))


def days_cache(settings):
    path = settings.data / "days.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except ValueError:
            return {}
    return {}
