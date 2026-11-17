"""Read side of the live store: per-chain status (follower heartbeat, lag, backlog, gaps), the accuracy panel
(independent money audits, hand-label regression, review flags, trace and price coverage), and the one-off import
of reports classified before the follower existed."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from arbkit.live import store as livedb
from arbkit.chain.spec import ROOT, list_chains, load_chain
from arbkit.value.daily import is_report_file, load_report
from arbkit.value.econ import COUNTED_SQL
from arbkit.live.follower import db_path


def _pct(part, whole):
    return round(100.0 * part / whole, 2) if whole else None


def hand_labels(spec) -> dict | None:
    files = sorted((spec.directory / "validation").glob("handlabels_*.json"))
    if not files:
        return None
    try:
        data = json.loads(files[-1].read_text())
    except ValueError:
        return None
    return {
        "file": files[-1].name,
        "checked_at": data.get("checked_at"),
        "labels": data.get("total"),
        "found": data.get("found"),
        "agree": data.get("ok"),
        "agree_pct": _pct(data.get("ok") or 0, data.get("found") or 0),
        "proceeds_exact": data.get("proceeds_exact"),
    }


def status(conn, spec, full: bool = True) -> dict:
    """The follower's state. `full` adds the block counts by source and the pool seeds: a scan of the whole blocks
    table (seconds on BSC/Robinhood), so the site's summaries and the public status ask for the cheap part only."""
    beat = livedb.get_meta(conn, "heartbeat") or {}
    gaps = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(last_block - first_block + 1), 0) blocks FROM gaps"
    ).fetchone()
    at = beat.get("at")
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(at)).total_seconds() if at else None
    running = age is not None and age < 180 and not beat.get("stopped")
    out = {
        "chain": spec.name,
        "running": running,
        "heartbeat_age_s": round(age) if age is not None else None,
        "heartbeat": beat,
        "cursor": livedb.get_meta(conn, "cursor"),
        "floor": livedb.get_meta(conn, "floor"),
        "backlog_blocks": sum(hi - lo + 1 for lo, hi in livedb.get_meta(conn, "backlog", [])),
        "gaps": gaps["n"],
        "gap_blocks": gaps["blocks"],
    }
    if not full:
        return out
    blocks = conn.execute(
        "SELECT COUNT(*) n, MIN(number) lo, MAX(number) hi, SUM(source='live') live, "
        "SUM(source='catchup_untraced') untraced, SUM(source LIKE 'import%') imported, MAX(classified_at) last "
        "FROM blocks"
    ).fetchone()
    out.update(
        {
            "blocks_stored": blocks["n"],
            "first_block": blocks["lo"],
            "last_block": blocks["hi"],
            "blocks_live": blocks["live"] or 0,
            "blocks_untraced_catchup": blocks["untraced"] or 0,
            "blocks_imported": blocks["imported"] or 0,
            "last_classified_at": blocks["last"],
            "pool_seed": {
                k[len("pool_seed:") :]: json.loads(v)
                for k, v in conn.execute(
                    "SELECT key, value FROM meta WHERE key LIKE 'pool_seed:%'"
                ).fetchall()
            },
        }
    )
    return out


def accuracy(conn, spec) -> dict:
    """What can be said about correctness from the store alone, every figure with its denominator."""
    a = conn.execute(
        "SELECT COUNT(*) batches, COALESCE(SUM(fees_checked),0) fees, COALESCE(SUM(fee_failures),0) fee_bad, "
        "COALESCE(SUM(cycles_checked),0) cycles, COALESCE(SUM(gross_mismatches),0) gross_bad, "
        "COALESCE(SUM(open_losses),0) open_losses, COALESCE(SUM(missing),0) missing, MAX(at) last, "
        "COALESCE(SUM(json_extract(detail, '$.audited_usd')),0) audited_usd, "
        "COALESCE(SUM(json_extract(detail, '$.mismatch_usd')),0) mismatch_usd FROM audits"
    ).fetchone()
    recent = [
        {**dict(r), "detail": json.loads(r["detail"]) if r["detail"] else None}
        for r in conn.execute(
            "SELECT first_block, last_block, at, fee_failures, gross_mismatches, open_losses, detail "
            "FROM audits WHERE fee_failures > 0 OR gross_mismatches > 0 OR open_losses > 0 "
            "ORDER BY id DESC LIMIT 10"
        )
    ]
    c = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(flagged),0) flagged, COALESCE(SUM(gross_usd),0) gross, "
        "COALESCE(SUM(CASE WHEN flagged THEN gross_usd END),0) flagged_gross FROM rows "
        f"WHERE {COUNTED_SQL}"
    ).fetchone()
    prices = {
        r["src"] or "none": r["n"]
        for r in conn.execute(
            f"SELECT json_extract(slim, '$.price_source') src, COUNT(*) n FROM rows WHERE {COUNTED_SQL} GROUP BY src"
        )
    }
    t = conn.execute(
        "SELECT COALESCE(SUM(traced),0) traced, COALESCE(SUM(trace_unavailable),0) unavailable, "
        "COALESCE(SUM(candidates),0) candidates FROM blocks WHERE source='live'"
    ).fetchone()
    unpriced = prices.get("unpriced", 0)
    return {
        "chain": spec.name,
        "money_audit": {
            "what": "every stored batch: network fees and each counted cycle's gross recomputed from raw receipts, "
            "transfer logs, native calls and claims, without the classifier's decoders; compared to the unit",
            "batches": a["batches"],
            "last": a["last"],
            "fees_checked": a["fees"],
            "fee_mismatches": a["fee_bad"],
            "fee_match_pct": _pct(a["fees"] - a["fee_bad"], a["fees"]),
            "cycles_checked": a["cycles"],
            "gross_mismatches": a["gross_bad"],
            "gross_match_pct": _pct(a["cycles"] - a["gross_bad"], a["cycles"]),
            "gross_audited_usd": round(a["audited_usd"], 2),
            "gross_mismatch_usd": round(a["mismatch_usd"], 6),
            "gross_dollar_match_pct": _pct(a["audited_usd"] - a["mismatch_usd"], a["audited_usd"]),
            "positive_cycles_with_open_losses": a["open_losses"],
            "missing_cache": a["missing"],
            "recent_failures": recent,
        },
        "hand_labels": hand_labels(spec),
        "review_flags": {
            "what": "counted cycles with a leg through a venue only the trace established (decoder/registry backlog); "
            "a flag for a human, not a verdict",
            "counted_cycles": c["n"],
            "flagged": c["flagged"],
            "flagged_pct": _pct(c["flagged"], c["n"]),
            "gross_usd": round(c["gross"], 2),
            "flagged_gross_usd": round(c["flagged_gross"], 2),
            "flagged_gross_pct": _pct(c["flagged_gross"], c["gross"]),
        },
        "pricing": {
            "counted_cycles_by_price_source": prices,
            "unpriced": unpriced,
            "priced_pct": _pct(c["n"] - unpriced, c["n"]),
        },
        "tracing": {
            "traces_ok": t["traced"],
            "trace_unavailable": t["unavailable"],
            "trace_ok_pct": _pct(t["traced"], t["traced"] + t["unavailable"]),
            "unverified_candidates": t["candidates"],
        },
    }


def import_reports(
    console, chain: str, directory: Path | None = None, reports_dir: Path | None = None
) -> int:
    spec = load_chain(chain)
    reports_dir = reports_dir or ROOT / "reports" / chain
    conn = livedb.connect(db_path(chain, directory))
    files = sorted(
        (p for p in reports_dir.glob("*.json") if is_report_file(p)), key=lambda p: p.stat().st_mtime
    )
    reports = [r for r in (load_report(p) for p in files) if r is not None]
    reports.sort(key=lambda r: r.get("created_at", ""))
    total = {"blocks": 0, "rows": 0, "replaced": 0}
    live_blocks = {
        r["number"] for r in conn.execute("SELECT number FROM blocks WHERE source NOT LIKE 'import%'")
    }
    for report in reports:
        live = {b["number"] for b in report["blocks"]} & live_blocks
        if live:  # never let an old report overwrite what the follower classified
            report = {
                **report,
                "blocks": [b for b in report["blocks"] if b["number"] not in live],
                "rows": [r for r in report["rows"] if r["block_number"] not in live],
            }
        if not report["blocks"]:
            continue
        stats = livedb.store_report(conn, spec, report, source=f"import:{report.get('created_at', '')[:19]}")
        for k in total:
            total[k] += stats[k]
    console.print(
        f"{chain}: imported {len(reports)} reports: {total['blocks']} blocks ({total['replaced']} re-written by a newer "
        f"report), {total['rows']} rows into {db_path(chain, directory)}",
        markup=False,
    )
    return 0


def render_status(console, directory: Path | None = None) -> int:
    for chain in list_chains():
        path = db_path(chain, directory)
        if not path.exists():
            console.print(f"{chain:10s} no live store", markup=False)
            continue
        conn = livedb.connect(path, readonly=True)
        spec = load_chain(chain)
        s, a = status(conn, spec), accuracy(conn, spec)
        beat, money = s["heartbeat"], a["money_audit"]
        console.print(
            f"{chain:10s} {'RUNNING' if s['running'] else 'stopped'} cursor {s['cursor']} lag {beat.get('lag_blocks')} blocks "
            f"({beat.get('lag_seconds')} s), {beat.get('blocks_per_minute')}/min vs chain {beat.get('chain_blocks_per_minute')}/min, "
            f"{beat.get('ms_per_block')} ms/block | stored {s['blocks_stored']} blocks (live {s['blocks_live']}, untraced catch-up "
            f"{s['blocks_untraced_catchup']}, imported {s['blocks_imported']}), backlog {s['backlog_blocks']}, gaps {s['gap_blocks']} | "
            f"fees {money['fee_match_pct']}% of {money['fees_checked']}, gross {money['gross_match_pct']}% of {money['cycles_checked']} "
            f"cycles (${money['gross_mismatch_usd']} off of ${money['gross_audited_usd']}) | "
            f"flagged {a['review_flags']['flagged_pct']}% | trace ok {a['tracing']['trace_ok_pct']}%",
            markup=False,
        )
    return 0
