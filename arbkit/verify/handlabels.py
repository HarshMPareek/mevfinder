"""Hand-labelled transactions per chain: the classifier must reproduce what a person verified on the explorer.

chains/<chain>/handlabels.json lists transactions with their block, the expected outcome and the proceeds the
person read off the explorer. `./arb -c <chain> handlabels` scans those blocks (whole blocks, so block-order
positions are available), compares every listed hash and writes chains/<chain>/validation/handlabels_<date>.md.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from arbkit.chain.spec import ChainSpec
from arbkit.value.fees import units

EXPECTED_ASSESSMENT = {
    "atomic_cycle": {"trace_supported"},
    "embedded_cycle": {"trace_supported"},
    "loss_cycle": {"trace_supported_loss"},
    "review": {"trace_supported", "trace_supported_loss", "needs_review", "event_candidate", "not_arb"},
    "needs_review": {"needs_review"},
    "not_arb": {"not_arb"},
    "not_counted": {"not_arb", "needs_review", "event_candidate"},  # anything but a counted or losing cycle
    # a hand-confirmed cycle through a venue the registry cannot verify yet: held for review until the venue is
    # identified and registered (2026-10-31 venue verification); counting it again is not a regression either
    "cycle_pending_venue": {"trace_supported", "trace_supported_loss", "needs_review"},
}


EXPECTED_FAMILY = {
    "pool_exploit": "pool_exploit",
    "inventory_arbitrage": "inventory_arbitrage",
    # not counted and no MEV family at all: a user's swap, a CEX-DEX candidate (2026-11-01: an $11,484
    # router swap and a single-venue Ekubo sale had read as inventory arbitrage; `not_counted` passed them)
    "no_family": "none",
    # a leg of a sandwich bundle (cyclic sandwiches: two cycles around a victim, 2026-11-01)
    "sandwich_front": "sandwich_front",
    "sandwich_back": "sandwich_back",
}


def load_handlabels(spec: ChainSpec) -> dict:
    path = spec.directory / "handlabels.json"
    if not path.exists():
        raise ValueError(f"No hand labels for {spec.name}: create {path}.")
    return json.loads(path.read_text())


def compare(spec: ChainSpec, labels: dict, report: dict) -> dict:
    rows = {r["tx_hash"].lower(): r for r in report["rows"]}
    results = []
    for item in labels["rows"]:
        tx_hash = item["tx_hash"].lower()
        row = rows.get(tx_hash)
        expected = item.get("expected", "atomic_cycle")
        if row is None:
            results.append({**item, "found": False, "ok": False, "assessment": None})
            continue
        if (
            expected in EXPECTED_FAMILY
        ):  # a family outside the cycle totals: the family, never a counted cycle
            ok = (row.get("mev_type") or "none") == EXPECTED_FAMILY[expected] and row[
                "assessment"
            ] in EXPECTED_ASSESSMENT["not_counted"]
        else:
            ok = row["assessment"] in EXPECTED_ASSESSMENT.get(expected, {expected})
        gross, symbol = None, row.get("profit_symbol")
        if row["assessment"] in {"trace_supported", "trace_supported_loss"}:
            gross = row.get("gross_profit")
            if (
                gross is None
                and row.get("gross_profit_raw") is not None
                and row.get("profit_decimals") is not None
            ):
                gross = units(row["gross_profit_raw"], row["profit_decimals"])
        sandwich = row.get("sandwich") or {}
        if sandwich.get("role") == "back" and sandwich.get("profit_raw") is not None:
            gross = sandwich.get("profit") or sandwich["profit_raw"] + " raw"
            symbol = (
                sandwich.get("profit_symbol") or sandwich.get("profit_token") or ""
            ) + " (sandwich bundle)"
        proceeds_match = None
        labelled = item.get("labelled_proceeds")
        if labelled and gross is not None:
            try:
                amount = Decimal(str(labelled).split()[0].replace(",", ""))
                proceeds_match = Decimal(gross) == amount
            except (ArithmeticError, ValueError):
                proceeds_match = None
        results.append(
            {
                **item,
                "found": True,
                "ok": ok,
                "assessment": row["assessment"],
                "arb_class": row["arb_class"],
                "mev_type": row.get("mev_type"),
                "trigger_type": row.get("trigger_type"),
                "trigger_distance_blocks": row.get("trigger_distance_blocks"),
                "n_legs": row["n_legs"],
                "profit_symbol": symbol,
                "gross": gross,
                "gross_usd": row.get("gross_usd"),
                "fee": row["fees"]["total_fee_eth"],
                "trace": row["trace"].get("verdict", row["trace"]["status"]),
                "proceeds_match": proceeds_match,
                "reasons": row.get("reasons", []),
                "payouts": row.get("forwarded_proceeds", {}),
                "explorer_url": row["explorer_url"],
            }
        )
    return {
        "chain": spec.name,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "source": labels.get("source"),
        "total": len(results),
        "found": sum(r["found"] for r in results),
        "ok": sum(r["ok"] for r in results),
        "proceeds_exact": sum(1 for r in results if r.get("proceeds_match")),
        "results": results,
    }


def write_markdown(spec: ChainSpec, result: dict, out_dir: Path | None = None) -> Path:
    out_dir = out_dir or spec.directory / "validation"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    path = out_dir / f"handlabels_{stamp}.md"
    lines = [
        f"# {spec.name}: hand-labelled transactions vs classifier",
        "",
        f"Checked {result['checked_at']}. Source: {result.get('source')}.",
        "",
        f"**{result['ok']}/{result['total']} as expected; {result['found']} found in the scanned blocks; "
        f"{result['proceeds_exact']} proceeds match the explorer figure to the unit.**",
        "",
        "| Tx | Block | Expected | Classifier | Position | Legs | Gross | Labelled | Match | Fee |",
        "|---|---:|---|---|---|---:|---|---|---|---:|",
    ]
    for r in result["results"]:
        link = f"[{r['tx_hash'][:10]}…]({spec.explorer_tx}{r['tx_hash']})"
        if not r["found"]:
            lines.append(
                f"| {link} | {r.get('block')} | {r.get('expected')} | not in scanned blocks | | | | {r.get('labelled_proceeds') or ''} | ✗ | |"
            )
            continue
        position = r.get("trigger_type") or ""
        if r.get("trigger_distance_blocks"):
            position += f" (-{r['trigger_distance_blocks']} blk)"
        gross = f"{r['gross']} {r.get('profit_symbol') or ''}".strip() if r.get("gross") is not None else "—"
        match = "✓" if r.get("proceeds_match") else ("✗" if r.get("proceeds_match") is False else "n/a")
        mark = "✓" if r["ok"] else "✗"
        lines.append(
            f"| {link} | {r['block']} | {r.get('expected')} | {mark} {r['assessment']} / {r['arb_class']} ({r.get('mev_type')}) | "
            f"{position} | {r['n_legs']} | {gross} | {r.get('labelled_proceeds') or ''} | {match} | {r['fee']} |"
        )
    lines += [
        "",
        "Expected `review` rows are hand-disputed cases: the classifier's answer is recorded for comparison, not scored.",
        "Positions are inferred from block order; `(-N blk)` marks a cross-block trigger on FCFS chains.",
    ]
    notes = [r for r in result["results"] if r.get("note")]
    if notes:
        lines += ["", "## Notes from the hand labels", ""]
        for r in notes:
            lines.append(f"- `{r['tx_hash'][:10]}…`: {r['note']}")
    path.write_text("\n".join(lines) + "\n")
    return path
