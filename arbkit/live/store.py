"""Live store: one SQLite file per chain (WAL mode) that the follower writes and the dashboard reads.

What is kept, per block: its header facts, a count of every kind of row, and a slim map of the transactions that
carry no MEV type (index, hash, from, to; dropped after `TXMAP_DAYS`). Per transaction with an MEV type or a
non-`not_arb` assessment: indexed columns, a slim JSON with every field the lists and aggregates read, and the
whole row (zlib JSON) for the transaction page. Per UTC day: the daily-figures accumulator (`daily.add_block`)
and the economics accumulator (`econ.econ_add`), updated in the same transaction as the rows, so the dashboard
never re-reads rows to draw totals. Also kept: independent money audits of every stored batch, gaps the follower
could not fill, and the follower's cursor and heartbeat (`meta`).

A block written twice (a reorg, a re-run) replaces its rows; the days it touched are rebuilt from the stored
slim rows so totals never count a block twice.
"""

from __future__ import annotations

import json
import sqlite3
import time
import zlib
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from arbkit.chain.spec import ChainSpec
from arbkit.value.daily import DAILY_METHOD, add_block, day_from_json, day_to_json, finalize_day, merge_day, new_day
from arbkit.value.econ import COUNTED_SQL, HELD_TYPES, counted_sql, econ_add, econ_from_json, econ_to_json, economics_result, new_econ
from arbkit.value.econ import counted as is_counted

SCHEMA = """
CREATE TABLE IF NOT EXISTS blocks(
  number INTEGER PRIMARY KEY, hash TEXT NOT NULL, parent_hash TEXT, timestamp INTEGER NOT NULL, day TEXT NOT NULL,
  transactions INTEGER NOT NULL, mev_rows INTEGER NOT NULL, cycles INTEGER NOT NULL, losses INTEGER NOT NULL,
  candidates INTEGER NOT NULL, sandwiches INTEGER NOT NULL, gross_usd REAL NOT NULL, traced INTEGER NOT NULL,
  trace_unavailable INTEGER NOT NULL, info TEXT NOT NULL, txmap BLOB, source TEXT NOT NULL,
  classifier_version TEXT, classified_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS blocks_day ON blocks(day);
CREATE TABLE IF NOT EXISTS rows(
  id INTEGER PRIMARY KEY AUTOINCREMENT, tx_hash TEXT NOT NULL UNIQUE, block_number INTEGER NOT NULL,
  tx_index INTEGER NOT NULL, timestamp INTEGER, assessment TEXT NOT NULL, mev_type TEXT NOT NULL,
  operator TEXT, tx_from TEXT, trigger_type TEXT, gross_usd REAL, net_usd REAL,
  usd REAL,  -- the row's dollar figure for sorting: a sandwich back-run's profit, else the cycle's gross
  flagged INTEGER NOT NULL DEFAULT 0, slim TEXT NOT NULL, full BLOB NOT NULL);
CREATE INDEX IF NOT EXISTS rows_block ON rows(block_number);
CREATE INDEX IF NOT EXISTS rows_type ON rows(mev_type, block_number);
CREATE INDEX IF NOT EXISTS rows_operator ON rows(operator);
CREATE INDEX IF NOT EXISTS rows_from ON rows(tx_from);
CREATE INDEX IF NOT EXISTS rows_assessment ON rows(assessment, block_number);
CREATE INDEX IF NOT EXISTS rows_usd ON rows(usd);
CREATE TABLE IF NOT EXISTS days(day TEXT PRIMARY KEY, acc TEXT NOT NULL, econ TEXT NOT NULL, updated_at TEXT NOT NULL);
-- the same accumulators per UTC hour ('YYYY-MM-DDTHH'): rolling windows (24 h, 3 days, 7 days) are sums of hours
CREATE TABLE IF NOT EXISTS hours(hour TEXT PRIMARY KEY, acc TEXT NOT NULL, econ TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS blocks_gross ON blocks(gross_usd);
CREATE INDEX IF NOT EXISTS blocks_cycles ON blocks(cycles);
CREATE INDEX IF NOT EXISTS rows_net ON rows(net_usd);
CREATE TABLE IF NOT EXISTS audits(
  id INTEGER PRIMARY KEY AUTOINCREMENT, first_block INTEGER, last_block INTEGER, at TEXT NOT NULL,
  fees_checked INTEGER NOT NULL, fee_failures INTEGER NOT NULL, cycles_checked INTEGER NOT NULL,
  gross_mismatches INTEGER NOT NULL, open_losses INTEGER NOT NULL, missing INTEGER NOT NULL, detail TEXT);
CREATE TABLE IF NOT EXISTS gaps(first_block INTEGER NOT NULL, last_block INTEGER NOT NULL, reason TEXT, noted_at TEXT,
  PRIMARY KEY(first_block, last_block));
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""

TXMAP_DAYS = 30
SLIM_KEYS = (
    "block_number",
    "tx_index",
    "tx_hash",
    "tx_from",
    "tx_to",
    "assessment",
    "arb_class",
    "mev_type",
    "trigger_type",
    "trigger_tx_hash",
    "trigger_tx_index",
    "trigger_distance_blocks",
    "trigger_block_number",
    "n_legs",
    "profit_token",
    "profit_symbol",
    "gross_profit",
    "gross_profit_raw",
    "gross_usd",
    "gross_native",
    "price_source",
    "fee_usd",
    "builder_tip_wei",
    "builder_tip_usd",
    "kickback_wei",
    "kickback_usd",
    "net_usd",
    "native_usd",
    "explorer_url",
    "operator_scope",
    "gross_source",
    "fee_coverage",
    "forwarded_profit_raw",
    "block_producer",
    "block_builder_tag",
    "refund_to_trigger_wei",
    "slot_index_est",
    "quote_venues",
    "snipe",
    "user_index_in_block",
    "dexes",
    "jit",
)
FEE_KEYS = (
    "total_fee_wei",
    "total_fee_eth",
    "priority_fee_wei",
    "priority_fee_per_gas_gwei",
    "base_fee_wei",
    "is_system",
    "complete",
)
TXMAP_FEES = ("total_fee_wei", "priority_fee_wei", "base_fee_wei", "is_system")
ECON_KEYS = ("total_fee_wei", "priority_fee_wei", "l1_fee_wei")
SANDWICH_KEYS = (
    "bundle_id",
    "role",
    "pool",
    "operator",
    "operator_kind",
    "front",
    "back",
    "victims",
    "profit",
    "profit_symbol",
    "profit_usd",
    "net_usd",
    "price_source",
)
LIQ_KEYS = (
    "protocol",
    "liquidator",
    "borrower",
    "debt_asset",
    "debt_usd",
    "collateral_asset",
    "collateral_usd",
    "bonus_usd",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def day_of(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def hour_of(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H")


def keeps(row: dict) -> bool:
    """A row is stored in full when it has an MEV type or is anything but a plain non-arbitrage transaction."""
    return (
        (row.get("mev_type") or "none") != "none"
        or row.get("assessment") != "not_arb"
        or bool(row.get("sandwich"))
    )


def flagged(row: dict) -> bool:
    """Counted rows the review queue holds: a leg through a venue only the trace established (registry backlog)."""
    return any(str(r).startswith("trace_only_venue") for r in row.get("reasons") or [])


def slim(row: dict) -> dict:
    out = {k: row.get(k) for k in SLIM_KEYS if row.get(k) is not None}
    out["fees"] = {k: (row.get("fees") or {}).get(k) for k in FEE_KEYS}
    trace = row.get("trace") or {}
    out["trace"] = {
        "status": trace.get("status"),
        "verdict": trace.get("verdict"),
        "venues_n": len(trace.get("venues") or row.get("venues") or []),
    }
    if row.get("economics") is not None:
        out["economics"] = {k: (row["economics"] or {}).get(k) for k in ECON_KEYS}
    if row.get("sandwich"):
        out["sandwich"] = {k: row["sandwich"].get(k) for k in SANDWICH_KEYS}
    if row.get("liquidations"):
        out["liquidations"] = [{k: liq.get(k) for k in LIQ_KEYS} for liq in row["liquidations"]]
    if row.get("cex_dex"):
        out["cex_dex"] = {k: row["cex_dex"].get(k) for k in ("edge_usd", "notional_usd")}
    if row.get("inventory_arb"):
        out["inventory_arb"] = {
            k: row["inventory_arb"].get(k) for k in ("gain_usd", "paid_usd", "received_usd", "stable_pair")
        }
    if row.get("exploited_pools"):
        out["exploited_pools"] = row["exploited_pools"]
    for key in ("beaten_by", "beat"):
        if row.get(key):
            out[key] = row[key]
    if flagged(row):
        out["flagged"] = True
    return out


def pack(value) -> bytes:
    return zlib.compress(json.dumps(value, separators=(",", ":"), default=str).encode(), 6)


def unpack(blob):
    return json.loads(zlib.decompress(blob)) if blob else None


def connect(path: Path, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False, timeout=30)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, isolation_level=None, timeout=60)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(SCHEMA)
    conn.row_factory = sqlite3.Row
    return conn


def get_meta(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_meta(conn, key: str, value) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value, default=str)),
    )


def _load_days(conn, days) -> dict[str, tuple[dict, dict]]:
    out = {}
    for day in days:
        r = conn.execute("SELECT acc, econ FROM days WHERE day=?", (day,)).fetchone()
        out[day] = (day_from_json(json.loads(r["acc"])), econ_from_json(json.loads(r["econ"]))) if r else None
    return out


def _save_day(conn, day: str, acc: dict, econ: dict) -> None:
    conn.execute(
        "INSERT INTO days(day, acc, econ, updated_at) VALUES(?, ?, ?, ?) ON CONFLICT(day) DO UPDATE SET "
        "acc=excluded.acc, econ=excluded.econ, updated_at=excluded.updated_at",
        (day, json.dumps(day_to_json(acc)), json.dumps(econ_to_json(econ)), utc_now()),
    )


def _load_hours(conn, hours) -> dict[str, tuple[dict, dict]]:
    out = {}
    for hour in hours:
        r = conn.execute("SELECT acc, econ FROM hours WHERE hour=?", (hour,)).fetchone()
        out[hour] = (
            (day_from_json(json.loads(r["acc"])), econ_from_json(json.loads(r["econ"]))) if r else None
        )
    return out


def _save_hour(conn, hour: str, acc: dict, econ: dict) -> None:
    conn.execute(
        "INSERT INTO hours(hour, acc, econ, updated_at) VALUES(?, ?, ?, ?) ON CONFLICT(hour) DO UPDATE SET "
        "acc=excluded.acc, econ=excluded.econ, updated_at=excluded.updated_at",
        (hour, json.dumps(day_to_json(acc)), json.dumps(econ_to_json(econ)), utc_now()),
    )


def store_report(conn, spec: ChainSpec, report: dict, source: str = "live") -> dict:
    """Write every block of a classified batch (rows, block facts, day totals) in one transaction.
    Returns {"blocks", "rows", "replaced"}."""
    by_block: dict[int, list[dict]] = defaultdict(list)
    for row in report["rows"]:
        by_block[row["block_number"]].append(row)
    # Live coverage is contiguous, not a uniform sample of the day: no extrapolation (observed figures only).
    uniform = str(report.get("day_mode", "")).startswith(("sample", "full"))
    version = report.get("classifier_version")
    stats = {"blocks": 0, "rows": 0, "replaced": 0}
    conn.execute("BEGIN IMMEDIATE")
    try:
        touched: dict[str, tuple[dict, dict] | None] = {}
        touched_hours: dict[str, tuple[dict, dict] | None] = {}
        dirty: set[str] = set()
        for info in sorted(report["blocks"], key=lambda b: b["number"]):
            number, ts = info["number"], info["timestamp"]
            day = day_of(ts)
            old = conn.execute("SELECT day, source FROM blocks WHERE number=?", (number,)).fetchone()
            block_source = source
            if old and str(old["source"]).startswith("import") and source == "live":
                # re-classifying an imported block does not make it followed live: it stays a sample of its day, so
                # coverage, the live window and failed-attempt figures never treat October samples as followed blocks
                block_source = old["source"]
            if old:
                dirty.add(old["day"])
                dirty.add(day)
                conn.execute("DELETE FROM rows WHERE block_number=?", (number,))
                conn.execute("DELETE FROM blocks WHERE number=?", (number,))
                stats["replaced"] += 1
            rows = sorted(by_block.get(number, []), key=lambda r: r["tx_index"])
            kept = [r for r in rows if keeps(r)]
            others = [
                [
                    r["tx_index"],
                    r["tx_hash"],
                    r.get("tx_from"),
                    r.get("tx_to"),
                    [(r.get("fees") or {}).get(k) for k in TXMAP_FEES],
                    (r.get("trace") or {}).get("status"),
                    r.get("arb_class"),
                ]
                for r in rows
                if not keeps(r)
            ]
            for r in kept:
                sandwich = r.get("sandwich") or {}
                usd = sandwich.get("profit_usd") if sandwich.get("role") == "back" else r.get("gross_usd")
                jit = r.get("jit") or {}
                if jit.get("role") == "remove" and jit.get("profit_usd") is not None:
                    usd = jit[
                        "profit_usd"
                    ]  # the JIT bundle's result, kept on its remove leg like a sandwich's on its back
                conn.execute(
                    "INSERT OR REPLACE INTO rows(tx_hash, block_number, tx_index, timestamp, assessment, mev_type, operator, "
                    "tx_from, trigger_type, gross_usd, net_usd, usd, flagged, slim, full) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        r["tx_hash"].lower(),
                        number,
                        r["tx_index"],
                        ts,
                        r["assessment"],
                        r.get("mev_type") or "none",
                        (r.get("tx_to") or r.get("tx_from") or "").lower(),
                        (r.get("tx_from") or "").lower(),
                        r.get("trigger_type"),
                        r.get("gross_usd"),
                        r.get("net_usd"),
                        usd,
                        int(flagged(r)),
                        json.dumps(slim(r), default=str),
                        pack(r),
                    ),
                )
            counted = [r for r in rows if is_counted(r)]
            conn.execute(
                "INSERT INTO blocks(number, hash, parent_hash, timestamp, day, transactions, mev_rows, cycles, losses, "
                "candidates, sandwiches, gross_usd, traced, trace_unavailable, info, txmap, source, classifier_version, "
                "classified_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    number,
                    info["hash"],
                    info.get("parent_hash"),
                    ts,
                    day,
                    len(rows),
                    len(kept),
                    len(counted),
                    sum(r["assessment"] == "trace_supported_loss" for r in rows),
                    sum(r["assessment"] in ("event_candidate", "needs_review") for r in rows),
                    sum(1 for r in rows if (r.get("sandwich") or {}).get("role") == "back"),
                    round(sum(r.get("gross_usd") or 0 for r in counted), 6),
                    sum((r.get("trace") or {}).get("status") == "ok" for r in rows),
                    sum((r.get("trace") or {}).get("status") == "unavailable" for r in rows),
                    json.dumps(
                        {k: info.get(k) for k in ("miner", "builder_tag", "base_fee_per_gas", "gas_used")}
                    ),
                    pack(others) if others else None,
                    block_source,
                    version,
                    report.get("created_at") or utc_now(),
                ),
            )
            stats["blocks"] += 1
            stats["rows"] += len(kept)
            if day in dirty:
                continue
            if day not in touched:
                touched.update(_load_days(conn, [day]))
            if touched[day] is None:
                touched[day] = (new_day(day, number), new_econ())
            hour = hour_of(ts)
            if hour not in touched_hours:
                touched_hours.update(_load_hours(conn, [hour]))
            if touched_hours[hour] is None:
                touched_hours[hour] = (new_day(hour, number), new_econ())
            slim_rows = [slim(r) for r in kept]
            for acc, econ in (touched[day], touched_hours[hour]):
                add_block(acc, number, slim_rows, uniform, transactions=len(rows))
                for r in counted:
                    econ_add(econ, spec.family, r)
        for day, value in touched.items():
            if value is not None and day not in dirty:
                _save_day(conn, day, *value)
        for hour, value in touched_hours.items():
            if value is not None and hour[:10] not in dirty:
                _save_hour(conn, hour, *value)
        for day in dirty:
            rebuild_day(conn, spec, day)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return stats


def _accumulate(conn, spec: ChainSpec, blocks: list, key) -> dict[str, tuple[dict, dict]]:
    """Day/hour accumulators for stored blocks (rows: number, transactions, timestamp), grouped by key(block)."""
    out: dict[str, tuple[dict, dict]] = {}
    if not blocks:
        return out
    numbers = {b["number"] for b in blocks}
    rows_by_block: dict[int, list[dict]] = defaultdict(list)
    for r in conn.execute(
        "SELECT block_number, slim FROM rows WHERE block_number BETWEEN ? AND ?", (min(numbers), max(numbers))
    ):
        if r["block_number"] in numbers:
            rows_by_block[r["block_number"]].append(json.loads(r["slim"]))
    for b in sorted(blocks, key=lambda b: b["number"]):
        k = key(b)
        if k not in out:
            out[k] = (new_day(k, b["number"]), new_econ())
        acc, econ = out[k]
        rows = rows_by_block.get(b["number"], [])
        add_block(acc, b["number"], rows, False, transactions=b["transactions"])
        for r in rows:
            econ_add(econ, spec.family, r)
    return out


def rebuild_day(conn, spec: ChainSpec, day: str) -> None:
    """Recompute a day's accumulators, and its hours', from the stored blocks and slim rows (after a block was replaced)."""
    blocks = conn.execute(
        "SELECT number, transactions, timestamp FROM blocks WHERE day=? ORDER BY number", (day,)
    ).fetchall()
    built = _accumulate(conn, spec, blocks, lambda b: day)
    if day in built:
        _save_day(conn, day, *built[day])
    else:
        conn.execute("DELETE FROM days WHERE day=?", (day,))
    conn.execute("DELETE FROM hours WHERE hour LIKE ?", (day + "T%",))
    for hour, value in _accumulate(conn, spec, blocks, lambda b: hour_of(b["timestamp"])).items():
        _save_hour(conn, hour, *value)


def recount_held(conn, spec: ChainSpec, log=None) -> dict:
    """Bring stored figures in line with the held families (econ.HELD_TYPES) without re-classifying: every block with
    a held row gets its cycles and gross recomputed from its counted rows, and every day holding such a block is
    rebuilt (day and hours) from the stored rows. One short write transaction per day, so a follower running at the
    same time only waits a moment. Idempotent. Returns {"blocks", "days", "seconds"}."""
    held = ",".join(repr(t) for t in sorted(HELD_TYPES))
    t0 = time.time()
    numbers = [
        r[0]
        for r in conn.execute(f"SELECT DISTINCT block_number FROM rows WHERE mev_type IN ({held}) ORDER BY 1")
    ]
    days = sorted(
        {
            r[0]
            for r in conn.execute(
                f"SELECT DISTINCT b.day FROM blocks b JOIN rows r ON r.block_number = b.number WHERE r.mev_type IN ({held})"
            )
        }
    )
    for i in range(0, len(numbers), 500):
        part = numbers[i : i + 500]
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                f"UPDATE blocks SET "
                f"cycles = (SELECT COUNT(*) FROM rows r WHERE r.block_number = blocks.number AND {counted_sql('r')}), "
                f"gross_usd = ROUND((SELECT COALESCE(SUM(r.gross_usd), 0) FROM rows r WHERE r.block_number = blocks.number "
                f"AND {counted_sql('r')}), 6) WHERE number IN ({','.join('?' * len(part))})",
                part,
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    for day in days:
        t = time.time()
        conn.execute("BEGIN IMMEDIATE")
        try:
            rebuild_day(conn, spec, day)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        if log:
            log(f"{spec.name} {day} rebuilt in {time.time() - t:.1f}s")
    return {"blocks": len(numbers), "days": len(days), "seconds": round(time.time() - t0, 1)}


def delete_blocks(conn, spec: ChainSpec, numbers: list[int]) -> None:
    """Remove blocks (reorged out) and their rows; their days are rebuilt."""
    if not numbers:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        days = set()
        for n in numbers:
            r = conn.execute("SELECT day FROM blocks WHERE number=?", (n,)).fetchone()
            if r:
                days.add(r["day"])
            conn.execute("DELETE FROM rows WHERE block_number=?", (n,))
            conn.execute("DELETE FROM blocks WHERE number=?", (n,))
            conn.execute("DELETE FROM audits WHERE first_block <= ? AND last_block >= ?", (n, n))
        for day in days:
            rebuild_day(conn, spec, day)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def audit_dollars(report: dict, result: dict) -> tuple[float, float]:
    """(gross USD of the cycles the audit recomputed, USD size of the differences it found): the money view of the
    audit, so one sub-cent disagreement does not read like a broken figure."""
    rows = {r["tx_hash"].lower(): r for r in report["rows"]}
    audited = sum(
        abs(r.get("gross_usd") or 0)
        for r in rows.values()
        if r["assessment"] in ("trace_supported", "trace_supported_loss") and r.get("gross_source") == "trace"
    )
    off = 0.0
    for m in result.get("gross_mismatches") or []:
        r = rows.get(str(m.get("tx_hash", "")).lower()) or {}
        try:
            raw, usd = int(r.get("gross_profit_raw") or 0), r.get("gross_usd")
            if raw and usd is not None:
                off += abs(int(m["classifier_gross"]) - int(m["gross_from_receipts"])) * abs(usd) / abs(raw)
        except (TypeError, ValueError, KeyError):
            continue
    return audited, off


def store_audit(conn, first: int, last: int, result: dict, report: dict | None = None) -> None:
    """Record the money audit of a classified batch. An older audit whose block range overlaps this one is removed
    first: a re-classified or re-orged block is audited again, and its old audit (of rows that no longer exist) must
    not stay in the totals. Until 2026-11-01 re-classification appended, so the overview counted Base's 14,767
    cycles as 67,670 audited and still summed the gross of rows that had been withdrawn."""
    detail = {
        k: result.get(k)[:20]
        for k in ("fee_failures", "gross_mismatches", "positive_cycles_with_open_losses")
        if result.get(k)
    }
    if report is not None:
        detail["audited_usd"], detail["mismatch_usd"] = audit_dollars(report, result)
    conn.execute("DELETE FROM audits WHERE first_block <= ? AND last_block >= ?", (last, first))
    conn.execute(
        "INSERT INTO audits(first_block, last_block, at, fees_checked, fee_failures, cycles_checked, gross_mismatches, "
        "open_losses, missing, detail) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            first,
            last,
            utc_now(),
            result["transactions_fee_checked"],
            len(result["fee_failures"]),
            result["cycles_checked"],
            len(result["gross_mismatches"]),
            len(result["positive_cycles_with_open_losses"]),
            len(result["missing_cached_data"]),
            json.dumps(detail, default=str),
        ),
    )


def dedupe_audits(conn) -> int:
    """One-off repair of stores written before `store_audit` replaced overlapping audits: walking from the newest
    audit down, an audit whose range overlaps a newer one is deleted. Returns how many were removed."""
    import bisect

    starts: list[
        int
    ] = []  # kept ranges, disjoint, sorted by first block: one binary search finds any overlap
    ends: list[int] = []
    doomed = []
    for r in conn.execute("SELECT id, first_block, last_block FROM audits ORDER BY id DESC").fetchall():
        lo, hi = r["first_block"], r["last_block"]
        i = bisect.bisect_right(starts, hi)  # kept ranges starting at or before `hi` are starts[:i]
        if (
            i and ends[i - 1] >= lo
        ):  # the last of them reaches `lo`: overlap (earlier ones end before it starts)
            doomed.append(r["id"])
        else:
            starts.insert(i, lo)
            ends.insert(i, hi)
    conn.execute("BEGIN IMMEDIATE")
    conn.executemany("DELETE FROM audits WHERE id=?", [(i,) for i in doomed])
    conn.execute("COMMIT")
    return len(doomed)


def note_gap(conn, first: int, last: int, reason: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO gaps(first_block, last_block, reason, noted_at) VALUES(?,?,?,?)",
        (first, last, reason, utc_now()),
    )


def drop_old_txmaps(conn, keep_days: int = TXMAP_DAYS) -> int:
    """Clear the block maps of whole UTC days older than `keep_days`, only the days not cleared yet (day index).
    The follower calls this on its event loop: a timestamp filter scanned the whole blocks table (~50 s on
    Robinhood, 2026-11-24), and every pass pushed ~370 blocks past the node's 128-block trace window."""
    now = datetime.now(timezone.utc)
    upto = (now - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    since = get_meta(conn, "txmaps_dropped_before") or (now - timedelta(days=keep_days + 2)).strftime("%Y-%m-%d")
    if since >= upto:
        return 0
    n = conn.execute(
        "UPDATE blocks SET txmap=NULL WHERE day >= ? AND day < ? AND txmap IS NOT NULL", (since, upto)
    ).rowcount
    set_meta(conn, "txmaps_dropped_before", upto)
    return n


# ---- reads (dashboard) ------------------------------------------------------------------------------------------


def coverage(conn, since: str = "", start_ts: int | None = None) -> dict:
    """What a window really covers. Continuous live data starts at the `floor` block (where the follower began
    following without a break); blocks before it (test runs, imported samples) are not part of any window. Within the
    window: first and last block times, blocks stored vs blocks in that span (missing = never classified), and blocks
    classified from receipts only (catch-up beyond the node's trace window). `complete` means the live data reaches back
    to the window's start with nothing missing, so "last N days" is literally true."""
    floor = int(get_meta(conn, "floor") or 0)
    if start_ts is not None:
        since = day_of(start_ts)
    r = conn.execute(
        "SELECT COUNT(*) n, MIN(number) lo, MAX(number) hi, MIN(timestamp) t0, MAX(timestamp) t1, "
        "COALESCE(SUM(source='catchup_untraced'), 0) untraced FROM blocks "
        "WHERE day >= ? AND timestamp >= ? AND number >= ? AND source NOT LIKE 'import%'",
        (since, start_ts or 0, floor),
    ).fetchone()
    if not r["n"]:
        return {"blocks": 0, "complete": False}
    first = conn.execute(
        "SELECT MIN(timestamp) FROM blocks WHERE number >= ? AND source NOT LIKE 'import%'", (floor,)
    ).fetchone()[0]
    start = (
        start_ts
        if start_ts is not None
        else (
            datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() if since else None
        )
    )
    missing = (r["hi"] - r["lo"] + 1) - r["n"]
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    return {
        "from": iso(r["t0"]),
        "to": iso(r["t1"]),
        "hours": round((r["t1"] - r["t0"]) / 3600, 1),
        "blocks": r["n"],
        "first_block": r["lo"],
        "last_block": r["hi"],
        "missing_blocks": missing,
        "untraced_blocks": r["untraced"],
        "live_since": iso(first),
        "window_start": iso(start) if start else None,
        "complete": start is not None and first <= start + 60 and missing == 0,
    }


def since_day(days: int | None) -> str:
    """First UTC day of a period of `days` days ending today ('' = everything)."""
    if not days:
        return ""
    return (datetime.now(timezone.utc) - timedelta(days=days - 1)).strftime("%Y-%m-%d")


def window_start(hours: int, now: float | None = None) -> int:
    """Start of a rolling window of `hours` hours, at the top of an hour: the window is every hour from there to now
    (between `hours` and `hours` + 1 hours long; the exact start is shown with the figures)."""
    now = time.time() if now is None else now
    return int(now - hours * 3600) // 3600 * 3600


def hourly(conn, start_ts: int) -> dict[str, tuple[dict, dict]]:
    return {
        r["hour"]: (day_from_json(json.loads(r["acc"])), econ_from_json(json.loads(r["econ"])))
        for r in conn.execute(
            "SELECT hour, acc, econ FROM hours WHERE hour >= ? ORDER BY hour", (hour_of(start_ts),)
        )
    }


def window_days(spec: ChainSpec, hours: dict[str, tuple[dict, dict]], days_cache: dict | None = None) -> dict:
    """The By-day table of a rolling window: each day's hours inside the window, merged; a day the window starts in
    part-way through says from which hour."""
    by_day: dict[str, dict] = {}
    first_hour: dict[str, str] = {}
    for hour in sorted(hours):
        day = hour[:10]
        acc = hours[hour][0]
        if day not in by_day:
            by_day[day] = new_day(day, acc["first_block"])
            first_hour[day] = hour[11:13]
        merge_day(by_day[day], acc)
    out = []
    for day in sorted(by_day):
        d = finalize_day(spec, day, by_day[day], days_cache)
        if first_hour[day] != "00" and day == min(by_day):
            d["window_from"] = f"{first_hour[day]}:00"
            d["coverage"] = None  # a part of the day by choice of window, not missing data
        out.append(d)
    return {
        "chain": spec.name,
        "chain_id": spec.chain_id,
        "native_symbol": spec.native_symbol,
        "generated_at": utc_now(),
        "reports": 0,
        "source": "live store",
        "days": out,
        "method": DAILY_METHOD,
    }


def start_block(conn, start_ts: int) -> int:
    """The first live block at or after a time (0 when there is none yet)."""
    r = conn.execute(
        "SELECT MIN(number) FROM blocks WHERE day = ? AND timestamp >= ?", (day_of(start_ts), start_ts)
    ).fetchone()[0]
    if r is None:
        r = conn.execute("SELECT MIN(number) FROM blocks WHERE day > ?", (day_of(start_ts),)).fetchone()[0]
    return r or 0


def daily(conn, spec: ChainSpec, days_cache: dict | None = None, since: str = "") -> dict:
    days = [
        finalize_day(spec, r["day"], day_from_json(json.loads(r["acc"])), days_cache)
        for r in conn.execute("SELECT day, acc FROM days WHERE day >= ? ORDER BY day", (since,))
    ]
    return {
        "chain": spec.name,
        "chain_id": spec.chain_id,
        "native_symbol": spec.native_symbol,
        "generated_at": utc_now(),
        "reports": 0,
        "source": "live store",
        "days": days,
        "method": DAILY_METHOD,
    }


def economics(conn, spec: ChainSpec, since: str = "", per_key: dict[str, dict] | None = None) -> dict:
    """The economics payload over days since `since`, or over given accumulators (a rolling window's hours; failed
    attempts are only recorded per day, so they cover the days the window touches)."""
    if per_key is None:
        per_key = {
            r["day"]: econ_from_json(json.loads(r["econ"]))
            for r in conn.execute("SELECT day, econ FROM days WHERE day >= ?", (since,))
        }
    per_day = {k[:10] for k in per_key}
    out = economics_result(spec.name, spec.family, per_key)
    failed = {
        d: v
        for d, v in (get_meta(conn, "failed_attempts", {}) or {}).get("days", {}).items()
        if d >= since and d in per_day
    }
    if failed and all(
        "net_usd" in v for v in failed.values()
    ):  # a figure stored before the same-block net: wait
        # measured only on blocks whose every transaction is stored (the live window): the kept figure it is set
        # against is the counted cycles' net on those same blocks, never the whole period's (imported days have no
        # record of their reverted transactions)
        usd = sum(v.get("usd") or 0.0 for v in failed.values())
        net_same = sum(v.get("net_usd") or 0.0 for v in failed.values())
        out["failed_attempts"] = {
            "transactions": sum(v["count"] for v in failed.values()),
            "usd": round(usd, 2),
            "native": round(sum(int(v["wei"]) for v in failed.values()) / 1e18, 9),
            "since_day": min(failed),
            "blocks": sum(v.get("blocks") or 0 for v in failed.values()),
            "kept_same_blocks_usd": round(net_same, 2),
            "kept_after_usd": round(net_same - usd, 2),
            "what": "gas paid by reverted transactions sent to contracts that have counted cycles, "
            "valued at each day's average native price, on the blocks whose every "
            "transaction is stored; set against the counted cycles' net on those blocks",
        }
    return out


def failed_attempts(conn) -> dict:
    """Gas the searchers burn on attempts that revert: reverted transactions whose callee is a contract with counted
    cycles in the store (the bots), per UTC day, from the blocks' transaction maps (plain transactions' fees and class).
    Valued at the day's average native price of the counted rows. On OP 47,012 such reverts cost $17 against $41 kept
    (2026-11-01): "kept by searchers" had read 70 % high there. Run in the follower's housekeeping, not per request."""
    bots: set[str] = set()
    prices: dict[str, list[float]] = defaultdict(list)
    for slim, ts in conn.execute(f"SELECT slim, timestamp FROM rows WHERE {COUNTED_SQL}"):
        s = json.loads(slim)
        if s.get("tx_to"):
            bots.add(str(s["tx_to"]).lower())
        if s.get("native_usd") and ts:
            prices[day_of(ts)].append(float(s["native_usd"]))
    days: dict[str, dict] = {}
    for day, txmap in conn.execute("SELECT day, txmap FROM blocks WHERE txmap IS NOT NULL"):
        days.setdefault(day, {"count": 0, "wei": 0, "blocks": 0, "net_usd": 0.0})["blocks"] += 1
        for _index, _hash, _sender, to, *rest in unpack(txmap) or []:
            if len(rest) < 3 or rest[2] != "reverted" or str(to or "").lower() not in bots:
                continue
            fee = int(dict(zip(TXMAP_FEES, rest[0] or [])).get("total_fee_wei") or 0)
            days[day]["count"] += 1
            days[day]["wei"] += fee
    for day, net in conn.execute(
        "SELECT b.day, r.net_usd FROM rows r JOIN blocks b ON b.number = r.block_number "
        f"WHERE {counted_sql('r')} "
        "AND b.txmap IS NOT NULL AND r.net_usd IS NOT NULL"
    ):
        if day in days:
            days[day]["net_usd"] += float(net)
    for day, d in days.items():
        px = prices.get(day)
        d["usd"] = d["wei"] / 1e18 * (sum(px) / len(px)) if px else None
        d["wei"] = str(d["wei"])
    return {"computed_at": utc_now(), "bots": len(bots), "days": days}


def block_rows(conn, number: int) -> tuple[dict | None, list[dict]]:
    """(block facts, every transaction of the block as a slim row in execution order); (None, []) if not stored."""
    b = conn.execute("SELECT * FROM blocks WHERE number=?", (number,)).fetchone()
    if b is None:
        return None, []
    rows = [
        json.loads(r["slim"]) for r in conn.execute("SELECT slim FROM rows WHERE block_number=?", (number,))
    ]
    for index, tx_hash, sender, to, *rest in unpack(b["txmap"]) or []:
        fees = dict(zip(TXMAP_FEES, rest[0])) if rest else {}
        rows.append(
            {
                "block_number": number,
                "tx_index": index,
                "tx_hash": tx_hash,
                "tx_from": sender,
                "tx_to": to,
                "assessment": "not_arb",
                "arb_class": rest[2] if len(rest) > 2 else "none",
                "mev_type": "none",
                "fees": fees,
                "trace": {"status": rest[1]} if len(rest) > 1 else {},
            }
        )
    rows.sort(key=lambda r: r["tx_index"])
    return dict(b), rows


def full_row(conn, tx_hash: str) -> dict | None:
    r = conn.execute("SELECT full FROM rows WHERE tx_hash=?", (tx_hash.lower(),)).fetchone()
    return unpack(r["full"]) if r else None
