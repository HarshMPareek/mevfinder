"""Hand review at scale: draw fresh rows, write the evidence sheet, lock the verdicts into `handlabels.json`.

The accuracy claim of this classifier rests on rows a person has read, not on the classifier agreeing with itself.
This module makes the reading mechanical so it can be done by the thousand per chain and class:

- `draw` picks `count` rows of a kind (counted cycles, losing cycles, family rows, rows under review) that no hand
  label, explorer copy or earlier sheet has covered, with a stated seed, from the newest classification per block;
- `write_sheet` puts everything a reader needs next to each row (label, route, the operator's flows, payouts, fees,
  reasons, trace notes, the explorer link) with empty `verdict` / `reading` fields in the JSON;
- `lock` appends the read rows to `handlabels.json`: `confirmed` keeps the classifier's class with its exact
  proceeds, `wrong:<expected>` records the corrected label (the regression fails until a rule fixes it), `unsure`
  records `review`;
- `progress` compares the hand-labelled counts per class with the targets.
"""

from __future__ import annotations

import json
import random
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from arbkit.chain.spec import ROOT, ChainSpec
from arbkit.value.daily import load_reports
from arbkit.verify.brackets import merged_report
from arbkit.verify.evidence import transaction_evidence
from arbkit.verify.review import route_lines, venue_dexes

FAMILIES = ("jit_liquidity", "liquidation", "launch_snipe", "cex_dex_candidate")
POSITIVE = {"trace_supported", "trace_supported_loss"}
TARGETS = {
    "cycles": 1000,
    "cycles_from_random_blocks": 100,
    "losing": 200,
    "sandwiches": 200,
    "families": 200,
}
KINDS = {
    "cycles": lambda r: r["assessment"] == "trace_supported",
    "losing": lambda r: r["assessment"] == "trace_supported_loss",
    "sandwiches": lambda r: (r.get("sandwich") or {}).get("role") == "back",
    "families": lambda r: r.get("mev_type") in FAMILIES,
    "review": lambda r: r["assessment"] == "needs_review",
}
KINDS["all"] = lambda r: any(f(r) for k, f in KINDS.items() if k != "all")


def covered_hashes(spec: ChainSpec) -> set[str]:
    """Hashes a hand label, an explorer copy or an earlier review sheet already covers."""
    out: set[str] = set()
    labels = spec.directory / "handlabels.json"
    if labels.exists():
        try:
            out |= {r["tx_hash"].lower() for r in json.loads(labels.read_text()).get("rows", [])}
        except (ValueError, KeyError, TypeError):
            pass
    out |= {p.name.split(".")[0].lower() for p in (spec.directory / "validation" / "explorer").glob("0x*")}
    for sheet in (spec.directory / "validation").glob("handreview_*.json"):
        try:
            data = json.loads(sheet.read_text())
        except ValueError:
            continue
        entries = data if isinstance(data, list) else data.get("entries") or data.get("rows") or []
        out |= {str(e.get("tx_hash", "")).lower() for e in entries if isinstance(e, dict)}
    return out


def block_sources(reports_dir: Path) -> dict[int, str]:
    """How each scanned block was chosen: `blind` (a seeded random draw, whatever rescanned it later), `day_sample`
    (an evenly spaced sample of a day), else `targeted` (a window or a block someone asked for)."""
    rank = {"blind": 2, "day_sample": 1, "targeted": 0}
    out: dict[int, str] = {}
    for report in load_reports(reports_dir):
        kind = (
            "blind"
            if report.get("blind_seed") is not None
            else ("day_sample" if str(report.get("day_mode", "")).startswith("sample") else "targeted")
        )
        for info in report["blocks"]:
            if rank[kind] > rank.get(out.get(info["number"], "targeted"), -1) or info["number"] not in out:
                out[info["number"]] = kind
    return out


def draw(
    spec: ChainSpec,
    reports_dir: Path,
    kind: str,
    count: int,
    seed: int,
    exclude: set[str] | None = None,
    random_only: bool = False,
) -> tuple[list[dict], dict]:
    """`count` fresh rows of `kind`, seeded, from the newest classification per block, with the block info by number.
    `random_only` restricts the draw to blocks that came from a seeded random (blind) sample."""
    report = merged_report(spec, reports_dir)
    sources = block_sources(reports_dir)
    info = {b["number"]: {**b, "source": sources.get(b["number"], "targeted")} for b in report["blocks"]}
    skip = covered_hashes(spec) | (exclude or set())
    pool = [
        r
        for r in report["rows"]
        if KINDS[kind](r)
        and r["tx_hash"].lower() not in skip
        and (not random_only or sources.get(r["block_number"]) == "blind")
    ]
    pool.sort(key=lambda r: (r["block_number"], r["tx_index"]))
    rng = random.Random(seed)
    picked = pool if len(pool) <= count else rng.sample(pool, count)
    return sorted(picked, key=lambda r: (r["block_number"], r["tx_index"])), info


def entry(spec: ChainSpec, row: dict, data_dir: Path, block: dict | None, dex_of: dict[str, str]) -> dict:
    """Everything a reader needs for one row; `verdict` and `reading` are left for the reader."""
    decimals = {t.address: t.decimals for t in spec.tokens}
    symbols = {t.address: t.symbol for t in spec.tokens}
    if row.get("profit_token") and row.get("profit_decimals") is not None:
        decimals[row["profit_token"].lower()] = row["profit_decimals"]
        if row.get("profit_symbol"):
            symbols[row["profit_token"].lower()] = row["profit_symbol"]
    try:
        ev = transaction_evidence(spec, data_dir, row, (block or {}).get("hash"))
    except (OSError, ValueError, KeyError):
        ev = {"token_transfers": [], "native_calls": [], "singleton_swaps": [], "claims": [], "status": None}
    transfers = [
        f"{t.get('symbol') or str(t.get('token', ''))[:10]} {str(t.get('from', ''))[:12]} -> {str(t.get('to', ''))[:12]} {t.get('amount_decimal') or t.get('amount') or t.get('raw')}"
        for t in (ev.get("token_transfers") or [])[:24]
    ]
    natives = [
        f"{str(c.get('from', ''))[:12]} -> {str(c.get('to', ''))[:12]} {c.get('value')}"
        for c in (ev.get("native_calls") or [])
        if isinstance(c, dict) and str(c.get("value") or "0") not in ("0", "0.0")
    ][:12]
    swaps = [
        f"{s.get('manager', '')[:12]} pool {s.get('pool_id', '')[:12]} by {s.get('locker', '')[:12]}: amount0 {s.get('amount0')} amount1 {s.get('amount1')}"
        for s in (ev.get("singleton_swaps") or [])[:16]
    ]
    claim_lines = [
        f"{c.get('kind')} {c.get('symbol') or str(c.get('token', ''))[:10]} {c.get('amount') or c.get('raw')} {'to' if c.get('kind') == 'claim_mint' else 'from'} {str(c.get('claimant', ''))[:12]}"
        for c in (ev.get("claims") or [])[:12]
    ]
    receipt_cached = ev.get("status") is not None
    tv = row.get("trace") or {}
    sandwich = row.get("sandwich") or {}
    return {
        "tx_hash": row["tx_hash"],
        "block": row["block_number"],
        "tx_index": row["tx_index"],
        "timestamp": (block or {}).get("timestamp"),
        "block_source": (block or {}).get("source", "targeted"),
        "label": {
            "assessment": row["assessment"],
            "arb_class": row["arb_class"],
            "mev_type": row.get("mev_type"),
            "trigger_type": row.get("trigger_type"),
            "trigger_distance_blocks": row.get("trigger_distance_blocks"),
        },
        "from": row.get("tx_from"),
        "to": row.get("tx_to"),
        "result": {
            "gross": row.get("gross_profit"),
            "symbol": row.get("profit_symbol"),
            "gross_usd": row.get("gross_usd"),
            "net_usd": row.get("net_usd"),
            "price_source": row.get("price_source"),
            "fee_native": (row.get("fees") or {}).get("total_fee_eth"),
            "builder_tip_wei": row.get("builder_tip_wei"),
            "kickback_wei": row.get("kickback_wei"),
        },
        "route": route_lines(spec, row, decimals, symbols, dex_of),
        "venues": row.get("venues"),
        "kinds": row.get("kinds"),
        "dexes": row.get("dexes"),
        "payouts": row.get("payouts"),
        "forwarded_proceeds": row.get("forwarded_proceeds"),
        "reasons": row.get("reasons"),
        "trace_verdict": tv.get("verdict"),
        "trace_notes": [n for n in (tv.get("notes") or []) if not n.startswith("erc20")][:8],
        "trace_group": tv.get("group"),
        "asset_deltas": tv.get("asset_deltas"),
        "sandwich": {
            k: sandwich.get(k)
            for k in ("bundle_id", "role", "profit", "profit_symbol", "profit_usd", "victims")
        }
        if sandwich
        else None,
        "liquidations": row.get("liquidations"),
        "jit": row.get("jit"),
        "cex_dex": row.get("cex_dex"),
        "token_transfers": transfers,
        "native_value_calls": natives,
        "singleton_swaps": swaps,
        "claims": claim_lines,
        "receipt_cached": receipt_cached,
        "explorer_url": row.get("explorer_url"),
        "verdict": "",
        "reading": "",
    }


def write_sheet(
    spec: ChainSpec, kind: str, seed: int, entries: list[dict], out_dir: Path | None = None
) -> tuple[Path, Path]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    out_dir = out_dir or spec.directory / "validation"
    out_dir.mkdir(parents=True, exist_ok=True)
    base = out_dir / f"handreview_{stamp}_{kind}_seed{seed}"
    base.with_suffix(".json").write_text(
        json.dumps(
            {
                "chain": spec.name,
                "kind": kind,
                "seed": seed,
                "drawn_at": datetime.now(timezone.utc).isoformat(),
                "verdicts": "confirmed | wrong:<expected arb_class or not_arb> | unsure",
                "entries": entries,
            },
            indent=1,
            default=str,
        )
    )
    lines = [
        f"# {spec.name}: hand review sheet ({kind}, seed {seed}, {len(entries)} rows)",
        "",
        "Read each row from the evidence below (and the explorer link), then put `confirmed`, `wrong:<expected>` or `unsure` plus a one-line "
        f"reading into the `verdict` / `reading` fields of `{base.with_suffix('.json').name}` and run `./arb -c {spec.name} handreview --lock {base.with_suffix('.json').name}`.",
        "",
    ]
    for e in entries:
        lab = e["label"]
        lines += [
            f"## block {e['block']} ({e.get('block_source', 'targeted')} block) #{e['tx_index']} — {lab['assessment']} / {lab['arb_class']} / {lab['mev_type']} — [{e['tx_hash'][:14]}…]({e['explorer_url']})",
            f"- from {e['from']} to {e['to']}; position {lab['trigger_type']} (distance {lab['trigger_distance_blocks']})",
            f"- result {e['result']['gross']} {e['result']['symbol'] or ''} ≈ ${e['result']['gross_usd']} [{e['result']['price_source']}], net ${e['result']['net_usd']}, fee {e['result']['fee_native']} native, tip {e['result']['builder_tip_wei']} wei",
            "- route: " + (" | ".join(e["route"]) if e["route"] else "(no route lines)"),
            f"- trace {e['trace_verdict']}; notes {e['trace_notes']}; group {e['trace_group']}; deltas {e['asset_deltas']}",
            f"- payouts {e['payouts']}; forwarded {e['forwarded_proceeds']}; reasons {e['reasons']}",
        ]
        if e["sandwich"]:
            lines.append(f"- sandwich {e['sandwich']}")
        if e["liquidations"] or e["jit"] or e["cex_dex"]:
            lines.append(f"- family: liquidations {e['liquidations']} jit {e['jit']} cex_dex {e['cex_dex']}")
        lines += [
            "- transfers: "
            + (
                "; ".join(e["token_transfers"])
                if e["token_transfers"]
                else (
                    "(no ERC-20 transfers in the receipt)"
                    if e.get("receipt_cached")
                    else "(no cached receipt)"
                )
            ),
            "- native value calls: "
            + ("; ".join(e["native_value_calls"]) if e["native_value_calls"] else "none"),
        ]
        if e.get("singleton_swaps"):
            lines.append(
                "- singleton swaps (operator's deltas: positive = received, negative = paid): "
                + "; ".join(e["singleton_swaps"])
            )
        if e.get("claims"):
            lines.append("- ERC-6909 claims: " + "; ".join(e["claims"]))
        lines += ["- **verdict:** ____  **reading:** ____", ""]
    base.with_suffix(".md").write_text("\n".join(lines))
    return base.with_suffix(".md"), base.with_suffix(".json")


def lock(spec: ChainSpec, sheet: Path, labels_path: Path | None = None) -> dict:
    """Append the read rows of a sheet to handlabels.json; rows without a verdict are skipped and reported."""
    data = json.loads(Path(sheet).read_text())
    entries = data["entries"] if isinstance(data, dict) else data
    labels_path = labels_path or spec.directory / "handlabels.json"
    labels = (
        json.loads(labels_path.read_text())
        if labels_path.exists()
        else {"chain": spec.name, "source": "", "rows": []}
    )
    by_hash = {r["tx_hash"].lower(): r for r in labels["rows"]}
    counts = Counter()
    for e in entries:
        verdict = str(e.get("verdict") or "").strip()
        if not verdict:
            counts["skipped_no_verdict"] += 1
            continue
        lab = e["label"]
        positive = lab["assessment"] in POSITIVE
        if verdict == "confirmed":
            expected = (
                lab["arb_class"] if positive else ("not_arb" if lab["assessment"] == "not_arb" else "review")
            )
            proceeds = (
                f"{e['result']['gross']} {e['result']['symbol']}"
                if positive and e["result"].get("gross") is not None
                else None
            )
        elif verdict.startswith("wrong:"):
            expected, proceeds = verdict.split(":", 1)[1].strip(), None
        elif verdict == "unsure":
            expected, proceeds = "review", None
        else:
            counts["skipped_bad_verdict"] += 1
            continue
        row = {
            "tx_hash": e["tx_hash"],
            "block": e["block"],
            "expected": expected,
            "labelled_proceeds": proceeds,
            "block_source": e.get("block_source", "targeted"),
            "note": f"hand review {data.get('drawn_at', '')[:10] if isinstance(data, dict) else ''} ({verdict}; classifier said {lab['assessment']}/{lab['arb_class']}/{lab['mev_type']}): {e.get('reading') or ''}".strip(),
        }
        if e["tx_hash"].lower() in by_hash:
            by_hash[e["tx_hash"].lower()].update(row)
            counts["updated"] += 1
        else:
            labels["rows"].append(row)
            by_hash[e["tx_hash"].lower()] = row
            counts["added"] += 1
        counts[verdict.split(":")[0]] += 1
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    labels["source"] = (
        (labels.get("source") or "").rstrip("; ")
        + f"; hand review sheet {Path(sheet).name} locked {stamp} ({counts['added']} added, {counts['updated']} updated)"
    )
    labels_path.write_text(json.dumps(labels, indent=1))
    return dict(counts)


def progress(spec: ChainSpec, labels_path: Path | None = None, reports_dir: Path | None = None) -> dict:
    labels_path = labels_path or spec.directory / "handlabels.json"
    rows = json.loads(labels_path.read_text()).get("rows", []) if labels_path.exists() else []
    by = Counter(r.get("expected") for r in rows)
    cycles = by.get("atomic_cycle", 0) + by.get("embedded_cycle", 0)
    sources = block_sources(reports_dir) if reports_dir is not None and reports_dir.exists() else {}
    random_cycles = sum(
        1
        for r in rows
        if r.get("expected") in ("atomic_cycle", "embedded_cycle")
        and (r.get("block_source") == "blind" or sources.get(int(r["block"])) == "blind")
    )
    pending = 0
    for sheet in (spec.directory / "validation").glob("handreview_*_seed*.json"):
        try:
            pending += sum(
                1 for e in json.loads(sheet.read_text())["entries"] if not str(e.get("verdict") or "").strip()
            )
        except (ValueError, KeyError, TypeError):
            continue
    return {
        "chain": spec.name,
        "hand_labelled": len(rows),
        "by_expected": dict(by),
        "cycles_confirmed": cycles,
        "cycles_confirmed_from_random_blocks": random_cycles,
        "losing_confirmed": by.get("loss_cycle", 0),
        "targets": TARGETS,
        "cycles_remaining_to_target": max(0, TARGETS["cycles"] - cycles),
        "random_block_cycles_remaining_to_target": max(
            0, TARGETS["cycles_from_random_blocks"] - random_cycles
        ),
        "pending_in_sheets": pending,
    }


def dex_map(spec: ChainSpec, data_dir: Path) -> dict[str, str]:
    return venue_dexes(spec, data_dir)


def default_dirs(spec: ChainSpec) -> tuple[Path, Path]:
    return ROOT / "reports" / spec.name, ROOT / "data" / spec.name
