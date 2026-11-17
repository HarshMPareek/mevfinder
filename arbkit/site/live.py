"""Dashboard data for a chain served from its live store (`livedb`) instead of report files in memory.

Every view the dashboard asks for is answered with an indexed query or from the per-day accumulators the follower
keeps, so memory stays flat however many blocks the store holds. Aggregates are cached for a few seconds; a
request never waits on more than one slow query of its own.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from arbkit.chain.spec import ROOT, load_chain
from arbkit.live import status as livestatus
from arbkit.live import store as livedb
from arbkit.site.reports import ChainData
from arbkit.site.views import PUBLIC_MEV_TYPES, compact, public_kind, public_view
from arbkit.value.builders import measure_builders
from arbkit.value.daily import day_from_json
from arbkit.value.econ import COUNTED_SQL

TTL = 15.0


MAX_STALE = 3600.0  # seconds a cached live aggregate may be served while it is recomputed in the background
MAX_KEYS = 400  # cached values kept per chain; the oldest are dropped beyond it. Only first pages of lists are cached:
# caching every page, sort and bot address (crawlers ask for thousands) grew the public site to its 12 GB limit
# within 3-4 h (OOM-killed 08:04, 11:57 and 16:22 UTC on 2026-11-04).


class LiveChainData(ChainData):
    def __init__(self, name: str, db_file: Path):  # noqa: D107 - no report files: the base __init__ is not used
        self.name = name
        self.spec = load_chain(name)
        self.db_file = db_file
        self.reports_dir = None
        self.reports: list[dict] = []
        self.rows_by_block: dict = {}
        self.rows_by_hash: dict = {}
        self.block_ts: dict = {}
        self.block_info: dict = {}
        self.lock = threading.Lock()
        self._local = threading.local()
        self._cache: dict[str, tuple[float, object]] = {}
        self._slots: dict[str, str] = {}
        self._refreshing: set[str] = set()

    # ---- plumbing -----------------------------------------------------------------------------------------
    def conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._local.conn = livedb.connect(self.db_file, readonly=True)
        return conn

    def cached(self, key: str, ttl: float, fn, slot: str | None = None):
        """A value at most `ttl` old, computed on demand. A stale value up to MAX_STALE old is served at once and
        recomputed in a background thread (one at a time per key), so a page never waits on the store's aggregates
        (seconds on BSC/Robinhood). `slot` names the view across keys that move with time (a rolling window starts at
        a new hour every hour): until the new key is computed, the slot's previous value is served the same way.
        Only a view never computed (or older than MAX_STALE) is computed while the request waits; the site's keeper
        thread (ui._keep_warm) computes the default views before any visitor asks."""
        now = time.time()
        hit = self._cache.get(key)
        if hit is not None and now - hit[0] < ttl:
            return hit[1]
        stand_in = hit
        if stand_in is None and slot is not None and self._slots.get(slot):
            stand_in = self._cache.get(self._slots[slot])
        if stand_in is not None and now - stand_in[0] < MAX_STALE:
            with self.lock:
                start = key not in self._refreshing
                self._refreshing.add(key)
            if start:
                threading.Thread(target=self._refresh_key, args=(key, fn, slot), daemon=True).start()
            return stand_in[1]
        value = fn()
        self._put(key, value, slot)
        return value

    def _put(self, key: str, value, slot: str | None) -> None:
        with self.lock:
            self._cache[key] = (time.time(), value)
            if slot is not None:
                old = self._slots.get(slot)
                self._slots[slot] = key
                if old and old != key:
                    self._cache.pop(old, None)  # the previous window's value: replaced by this one
            if len(self._cache) > MAX_KEYS:
                for k, _ in sorted(self._cache.items(), key=lambda kv: kv[1][0])[: MAX_KEYS // 4]:
                    self._cache.pop(k, None)

    def _refresh_key(self, key: str, fn, slot: str | None = None) -> None:
        try:
            self._put(key, fn(), slot)
        except Exception:  # noqa: BLE001 - keep serving the old value; the next request retries
            pass
        finally:
            with self.lock:
                self._refreshing.discard(key)

    def refresh(self) -> None:
        return None

    def _days(self, period: int | None = None) -> dict:
        since = livedb.since_day(period)

        def load():
            return {
                r["day"]: day_from_json(json.loads(r["acc"]))
                for r in self.conn().execute("SELECT day, acc FROM days WHERE day >= ?", (since,))
            }

        return self.cached(f"days:{since}", TTL, load, slot=f"days:{period}")

    # ---- aggregates ---------------------------------------------------------------------------------------
    @property
    def daily(self) -> dict:
        return self.daily_for(None)

    def daily_for(self, period: int | None, hours: int | None = None) -> dict:
        window = self._window(period, hours)
        if window and self._has_hours():

            def load_window():
                out = livedb.window_days(self.spec, self._hours(window), self._days_cache())
                first = out["days"][0] if out["days"] else None
                if first and first.get(
                    "window_from"
                ):  # the exact time of the first block shown, not just its hour
                    ts = self.get_block_ts(first["first_block"])
                    if ts:
                        first["window_from"] = datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M")
                return out

            return self.cached(
                f"window_days:{livedb.window_start(window)}", TTL, load_window, slot=f"window_days:{window}"
            )
        since = livedb.since_day(window // 24 if window else None)

        def load():
            days_cache = {}
            path = ROOT / "data" / self.name / "days.json"
            if path.exists():
                try:
                    days_cache = json.loads(path.read_text())
                except ValueError:
                    days_cache = {}
            return livedb.daily(self.conn(), self.spec, days_cache, since)

        return self.cached(f"daily:{since}", TTL, load, slot=f"daily:{window}")

    def _window(self, period: int | None, hours: int | None) -> int | None:
        """A window in hours: `hours`, or `period` days as a rolling window of period x 24 hours; None = everything."""
        return hours if hours else (period * 24 if period else None)

    def _hours(self, hours: int) -> dict:
        start = livedb.window_start(hours)
        return self.cached(
            f"hours:{start}", TTL, lambda: livedb.hourly(self.conn(), start), slot=f"hours:{hours}"
        )

    def _has_hours(self) -> bool:
        return self.cached(
            "has_hours", 300.0, lambda: bool(self.conn().execute("SELECT 1 FROM hours LIMIT 1").fetchone())
        )

    def window_start_block(self, hours: int | None) -> int:
        if not hours:
            return 0
        start = livedb.window_start(hours)
        return self.cached(
            f"start_block:{start}",
            300.0,
            lambda: livedb.start_block(self.conn(), start),
            slot=f"start_block:{hours}",
        )

    def summary(self, period: int | None = None, hours: int | None = None) -> dict:
        window = self._window(period, hours)
        rolling = bool(window) and self._has_hours()
        start = livedb.window_start(window) if window else None
        since = livedb.day_of(start) if rolling else livedb.since_day(window // 24 if window else None)

        def block_facts():
            conn = self.conn()
            b = conn.execute(
                "SELECT COUNT(*) n, MIN(number) lo, MAX(number) hi, MAX(classified_at) last FROM blocks "
                "WHERE day >= ? AND timestamp >= ?",
                (since, start if rolling else 0),
            ).fetchone()
            return {"b": dict(b), "coverage": livedb.coverage(conn, since, start if rolling else None)}

        def load():
            conn = self.conn()
            # the block-table scans (seconds on BSC/Robinhood) move by a few blocks a minute: refreshed every 2 minutes
            # in the background; the money figures and the live status are as fresh as the hourly accumulators
            # (no slot: a new window's figures never mix with the previous window's block counts)
            facts = self.cached(f"block_facts:{since}:{start if rolling else ''}", 120.0, block_facts)
            b = facts["b"]
            accs = (
                [a for a, _ in self._hours(window).values()]
                if rolling
                else list(self._days(window // 24 if window else None).values())
            )
            cycles = sum(sum(d["positions"].values()) for d in accs)
            gross = sum(sum(d["gross_usd"].values()) for d in accs)
            net = sum(sum(d["net_usd"].values()) for d in accs)
            st = livestatus.status(conn, self.spec, full=False)
            beat = st["heartbeat"]
            return {
                "name": self.name,
                "chain_id": self.spec.chain_id,
                "native_symbol": self.spec.native_symbol,
                "explorer_tx": self.spec.explorer_tx,
                "explorer_block": self.spec.explorer_block,
                "explorer_address": self.spec.explorer_address,
                "explorer_name": self.spec.explorer_name,
                "family": self.spec.family,
                "ordering_policy": self.spec.ordering_policy,
                "block_time_ms": self.spec.block_time_ms,
                "fee_flow": self.spec.fee_flow or {},
                "reports": 0,
                "blocks_covered": b["n"],
                "first_block": b["lo"],
                "last_block": b["hi"],
                "cycles": cycles,
                "gross_usd": round(gross, 2),
                "net_usd": round(net, 2),
                "sandwiches": sum(d["sandwiches"] for d in accs),
                "days": len({d["day"][:10] for d in accs}),
                "last_scanned": b["last"],
                "live": {
                    "running": st["running"],
                    "lag_blocks": beat.get("lag_blocks"),
                    "lag_seconds": beat.get("lag_seconds"),
                    "head": beat.get("head"),
                    "cursor": st["cursor"],
                    "since_block": st["floor"],
                    "backlog_blocks": st["backlog_blocks"],
                    "gap_blocks": st["gap_blocks"],
                    "heartbeat_at": beat.get("at"),
                },
                "period_days": round(window / 24, 2) if window else None,
                "window_hours": window,
                "window_start": datetime.fromtimestamp(start, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                if rolling
                else None,
                "period_since": since or None,
                "coverage": facts["coverage"],
            }

        return self.cached(
            f"summary:{since}:{start if rolling else ''}", 15.0, load, slot=f"summary:{window}"
        )

    def _days_cache(self) -> dict:
        path = ROOT / "data" / self.name / "days.json"
        try:
            return json.loads(path.read_text()) if path.exists() else {}
        except ValueError:
            return {}

    def economics(self, period: int | None = None, hours: int | None = None) -> dict:
        window = self._window(period, hours)
        if window and self._has_hours():
            start = livedb.window_start(window)
            return self.cached(
                f"economics_h:{start}",
                TTL,
                lambda: livedb.economics(
                    self.conn(),
                    self.spec,
                    livedb.day_of(start),
                    {h: e for h, (_, e) in self._hours(window).items()},
                ),
                slot=f"economics_h:{window}",
            )
        since = livedb.since_day(window // 24 if window else None)
        return self.cached(
            f"economics:{since}",
            TTL,
            lambda: livedb.economics(self.conn(), self.spec, since),
            slot=f"economics:{window}",
        )

    def live(self, public: bool = False) -> dict:
        if public:  # the public site shows status only (the cheap part); audits, hand labels and review flags stay internal
            return {
                "status": self.cached(
                    "live_status", 10.0, lambda: livestatus.status(self.conn(), self.spec, full=False)
                )
            }

        def load():
            conn = self.conn()
            return {
                "status": livestatus.status(conn, self.spec),
                "accuracy": livestatus.accuracy(conn, self.spec),
            }

        return self.cached("live", 60.0, load)

    OPERATOR_SORTS = {
        "gross": "gross_usd DESC",
        "gross_asc": "gross_usd ASC",
        "net": "net_usd DESC",
        "net_asc": "net_usd ASC",
        "cycles": "cycles DESC, gross_usd DESC",
        "cycles_asc": "cycles ASC, gross_usd ASC",
    }

    def operators_ranked(self, hours: int | None = None, sort: str = "gross") -> list[dict]:
        """Every bot with counted cycles in the window, ranked (cached; pages are slices of it)."""
        first = self.window_start_block(hours)
        order = self.OPERATOR_SORTS.get(sort, self.OPERATOR_SORTS["gross"])

        def load():
            rows = self.conn().execute(
                "SELECT operator address, COUNT(*) cycles, COALESCE(SUM(gross_usd),0) gross_usd, COALESCE(SUM(net_usd),0) net_usd, "
                "SUM(trigger_type='backrun') backruns, SUM(trigger_type='top_of_block') top_of_block, COUNT(DISTINCT tx_from) wallets "
                f"FROM rows WHERE {COUNTED_SQL} AND block_number >= ? GROUP BY operator ORDER BY {order}, address",
                (first,),
            )
            return [
                {**dict(r), "gross_usd": round(r["gross_usd"], 2), "net_usd": round(r["net_usd"], 2)}
                for r in rows
            ]

        return self.cached(f"operators:{first}:{order}", 120.0, load, slot=f"operators:{hours}:{order}")

    def operators(
        self,
        limit=25,
        period: int | None = None,
        hours: int | None = None,
        sort: str = "gross",
        offset: int = 0,
    ) -> list[dict]:
        return self.operators_ranked(self._window(period, hours), sort)[offset : offset + limit]

    def families(self, public: bool = False) -> dict:
        def load():
            totals: dict[str, float] = {}
            for d in self.daily.get("days", []):
                for k, v in (d.get("families") or {}).items():
                    if public and k.startswith("cex_dex"):
                        continue
                    totals[k] = totals.get(k, 0) + v
            days = self._days().values()
            q = (
                self.conn()
                .execute(
                    f"SELECT COALESCE(SUM(gross_usd),0) g FROM rows WHERE {COUNTED_SQL} "
                    "AND COALESCE(json_array_length(slim, '$.quote_venues'), 0) > 0"
                )
                .fetchone()
            )
            return {
                "totals": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in totals.items()},
                "quote_leg_cycles": sum(d["quote_leg_cycles"] for d in days),
                "next_block_backruns": sum(d["positions"].get("backrun_next_block", 0) for d in days),
                "quote_leg_gross_usd": round(q["g"], 2),
                "recent": {
                    kind: self.transactions(25, {kind}, public=public)
                    for kind in (
                        "liquidation",
                        "launch_snipe",
                        "jit_liquidity",
                        "pool_exploit",
                        "inventory_arbitrage",
                        "cex_dex_candidate",
                    )
                    if not (public and kind == "cex_dex_candidate")
                },
            }

        return self.cached(f"families:{public}", 120.0, load)

    def builders(self) -> dict:
        def load():
            conn = self.conn()
            numbers = [
                r["number"]
                for r in conn.execute(
                    "SELECT number FROM blocks WHERE txmap IS NOT NULL OR mev_rows = transactions "
                    "ORDER BY number DESC LIMIT 300"
                )
            ]
            rows, blocks = [], []
            for n in numbers:
                rows.extend(self.get_block_rows(n) or [])
                blocks.append(self.get_block_info(n))
            report = {
                "rows": rows,
                "blocks": blocks,
                "created_at": blocks[0].get("classified_at") if blocks else None,
            }
            out = measure_builders(self.spec, report, ROOT / "data" / self.name)
            out["per_block"] = sorted(out["per_block"], key=lambda b: -b["number"])[:300]
            return out

        return self.cached("builders", 300.0, load)

    # ---- lookups ------------------------------------------------------------------------------------------
    @staticmethod
    def _info(b) -> dict:
        if not b:
            return {}
        info = json.loads(b["info"]) if b.get("info") else {}
        return {
            **info,
            "number": b["number"],
            "hash": b["hash"],
            "timestamp": b["timestamp"],
            "transactions": b["transactions"],
            "classified_at": b.get("classified_at"),
            "source": b.get("source"),
        }

    def get_row(self, tx_hash: str) -> dict | None:
        return livedb.full_row(self.conn(), tx_hash)

    def get_block_rows(self, number: int) -> list[dict] | None:
        info, rows = livedb.block_rows(self.conn(), number)
        if info is None:
            return None
        facts = json.loads(info["info"]) if info.get("info") else {}
        for row in rows:  # the slim map of plain transactions keeps no per-row copy of the block's facts
            row.setdefault("explorer_url", self.spec.explorer_url(row["tx_hash"]))
            row.setdefault("block_producer", (facts.get("miner") or "").lower() or None)
            row.setdefault("block_builder_tag", facts.get("builder_tag"))
        return rows

    def get_block_info(self, number: int) -> dict:
        r = self.conn().execute("SELECT * FROM blocks WHERE number=?", (number,)).fetchone()
        return self._info(dict(r)) if r else {}

    def get_block_ts(self, number: int) -> int | None:
        r = self.conn().execute("SELECT timestamp FROM blocks WHERE number=?", (number,)).fetchone()
        return r["timestamp"] if r else None

    def has_block(self, number: int) -> bool:
        return self.conn().execute("SELECT 1 FROM blocks WHERE number=?", (number,)).fetchone() is not None

    # the ascending money/cycle sorts list the blocks with arbitrage first (a block without any is not "the least")
    BLOCK_SORTS = {
        "latest": "number DESC",
        "oldest": "number ASC",
        "gross": "gross_usd DESC, number DESC",
        "gross_asc": "cycles = 0, gross_usd ASC, number DESC",
        "cycles": "cycles DESC, number DESC",
        "cycles_asc": "cycles = 0, cycles ASC, number DESC",
    }

    def blocks_list(
        self, limit: int = 500, offset: int = 0, sort: str = "latest", hours: int | None = None
    ) -> list[dict]:
        order = self.BLOCK_SORTS.get(sort, self.BLOCK_SORTS["latest"])
        first = self.window_start_block(hours)

        def load():
            return [
                {**dict(r), "gross_usd": round(r["gross_usd"], 2) or 0}
                for r in self.conn().execute(
                    "SELECT number, timestamp, transactions, cycles, sandwiches, gross_usd, source FROM blocks WHERE number >= ? "
                    f"ORDER BY {order} LIMIT ? OFFSET ?",
                    (first, limit, offset),
                )
            ]

        if offset:
            return load()
        params = f"{order}:{limit}"
        return self.cached(
            f"blocks:{first}:{params}",
            3.0 if sort == "latest" else 30.0,
            load,
            slot=f"blocks:{hours}:{params}",
        )

    def blocks_total(self, hours: int | None = None) -> int:
        first = self.window_start_block(hours)
        return self.cached(
            f"blocks_total:{first}",
            30.0,
            lambda: (
                self.conn().execute("SELECT COUNT(*) FROM blocks WHERE number >= ?", (first,)).fetchone()[0]
            ),
            slot=f"blocks_total:{hours}",
        )

    def _view(self, row: dict, public: bool) -> dict:
        ts = row.get("timestamp")
        return (
            public_view(row, ts, self.spec.family)
            if public
            else dict(compact(row, ts), kind=public_kind(row))
        )

    # the sort keys are the figures the table shows (ui/app.js resultUsd / kept): a sandwich's result and kept are its
    # bundle's, a liquidation's result its bonus, inventory arbitrage its gain; only counted cycles have a kept figure.
    # Nulls last, so every sort lists the same transactions.
    RESULT = (
        "(CASE mev_type WHEN 'sandwich_back' THEN json_extract(slim, '$.sandwich.profit_usd') "
        "WHEN 'liquidation' THEN json_extract(slim, '$.liquidations[0].bonus_usd') "
        "WHEN 'inventory_arbitrage' THEN json_extract(slim, '$.inventory_arb.gain_usd') ELSE gross_usd END)"
    )
    KEPT = (
        "(CASE WHEN mev_type = 'sandwich_back' THEN json_extract(slim, '$.sandwich.net_usd') "
        f"WHEN {COUNTED_SQL} THEN net_usd END)"
    )
    TX_SORTS = {
        "latest": "block_number DESC, tx_index DESC",
        "oldest": "block_number ASC, tx_index ASC",
        "usd": f"{RESULT} IS NULL, {RESULT} DESC, block_number DESC, tx_index DESC",
        "usd_asc": f"{RESULT} IS NULL, {RESULT} ASC, block_number DESC, tx_index DESC",
        "net": f"{KEPT} IS NULL, {KEPT} DESC, block_number DESC, tx_index DESC",
        "net_asc": f"{KEPT} IS NULL, {KEPT} ASC, block_number DESC, tx_index DESC",
    }

    def _tx_where(self, kinds, min_usd, public, first_block):
        where, args = ["mev_type != 'none'"], []
        types = set(kinds or ())
        if public:
            types = (types & PUBLIC_MEV_TYPES) if types else set(PUBLIC_MEV_TYPES)
            if not types:
                return None, None
        if types:
            where.append(f"mev_type IN ({','.join('?' * len(types))})")
            args.extend(sorted(types))
        if min_usd is not None:
            where.append("usd >= ?")
            args.append(min_usd)
        if first_block:
            where.append("block_number >= ?")
            args.append(first_block)
        return " AND ".join(where), args

    def transactions_total(
        self,
        kinds: set[str] | None = None,
        min_usd: float | None = None,
        public: bool = False,
        hours: int | None = None,
    ) -> int:
        where, args = self._tx_where(kinds, min_usd, public, self.window_start_block(hours))
        if where is None:
            return 0
        slot = f"tx_total:{sorted(kinds or ())}:{min_usd}:{public}:{hours}"
        return self.cached(
            f"tx_total:{where}:{args}",
            30.0,
            lambda: self.conn().execute(f"SELECT COUNT(*) FROM rows WHERE {where}", args).fetchone()[0],
            slot=slot,
        )

    def _types(self, kinds, public) -> set[str] | None:
        """The MEV types a list may show (None = every type but 'none'); an empty set = nothing."""
        types = set(kinds or ()) - {"none"}
        if public:
            return (types & PUBLIC_MEV_TYPES) if kinds else set(PUBLIC_MEV_TYPES)
        return types if kinds else None

    def _ordered_ids(self, types, min_usd, first_block, sort, limit, offset) -> tuple[str, list]:
        """Newest (or oldest) first without sorting the window: one range of the (mev_type, block_number) index per
        type, merged; each range reads at most offset + limit entries. Sorting every matching row of 7 days took
        2.5 s on Base. Returns (query, args) selecting slim, timestamp."""
        d = "ASC" if sort == "oldest" else "DESC"
        parts, args = [], []
        for t in sorted(types):
            cond, a = ["mev_type = ?"], [t]
            if min_usd is not None:
                cond.append("usd >= ?")
                a.append(min_usd)
            if first_block:
                cond.append("block_number >= ?")
                a.append(first_block)
            parts.append(
                f"SELECT * FROM (SELECT id, block_number b, tx_index i FROM rows WHERE {' AND '.join(cond)} "
                f"ORDER BY block_number {d}, tx_index {d} LIMIT ?)"
            )
            args += [*a, offset + limit]
        query = (
            f"SELECT r.slim, r.timestamp FROM ({' UNION ALL '.join(parts)}) k JOIN rows r ON r.id = k.id "
            f"ORDER BY k.b {d}, k.i {d} LIMIT ? OFFSET ?"
        )
        return query, [*args, limit, offset]

    def transactions(
        self,
        limit=200,
        kinds: set[str] | None = None,
        min_usd: float | None = None,
        block: int | None = None,
        public: bool = False,
        sort: str | None = None,
        offset: int = 0,
        hours: int | None = None,
    ) -> list[dict]:
        if block is not None:
            rows = self.get_block_rows(block) or []
            ts = self.get_block_ts(block)
            out = []
            for row in reversed(rows):
                kind = row.get("mev_type", "none")
                if (
                    (public and kind not in PUBLIC_MEV_TYPES)
                    or (kinds and kind not in kinds)
                    or (not kinds and kind == "none")
                ):
                    continue
                out.append(self._view({**row, "timestamp": ts}, public))
            return out[:limit]
        sort = sort if sort in self.TX_SORTS else "latest"
        first = self.window_start_block(hours)
        types = self._types(kinds, public)
        if types is not None and not types:
            return []

        def load():
            if sort in ("latest", "oldest") and types:
                query, args = self._ordered_ids(types, min_usd, first, sort, limit, offset)
            else:
                where, wargs = self._tx_where(kinds, min_usd, public, first)
                if sort in (
                    "latest",
                    "oldest",
                ):  # every type: walk the block order (rows_block), not the type index
                    where = where.replace("mev_type != 'none'", "+mev_type != 'none'", 1)
                query, args = (
                    f"SELECT slim, timestamp FROM rows WHERE {where} ORDER BY {self.TX_SORTS[sort]} LIMIT ? OFFSET ?",
                    [*wargs, limit, offset],
                )
            return [
                self._view({**json.loads(r["slim"]), "timestamp": r["timestamp"]}, public)
                for r in self.conn().execute(query, args)
            ]

        if offset:
            return load()
        params = f"{sorted(types) if types else None}:{min_usd}:{public}:{sort}:{limit}"
        return self.cached(
            f"txs:{first}:{params}", 5.0 if sort == "latest" else 30.0, load, slot=f"txs:{hours}:{params}"
        )

    def bot(
        self, address: str, public: bool = False, limit: int = 300, offset: int = 0, sort: str | None = None
    ) -> dict:
        address = address.lower()
        sort = sort if sort in self.TX_SORTS else "latest"
        where = "(operator = ? OR tx_from = ?)"
        if public:
            where += f" AND mev_type IN ({','.join('?' * len(PUBLIC_MEV_TYPES))})"
        args = (address, address, *(sorted(PUBLIC_MEV_TYPES) if public else ()))

        def load_rows():
            return [
                self._view({**json.loads(r["slim"]), "timestamp": r["timestamp"]}, public)
                for r in self.conn().execute(
                    f"SELECT slim, timestamp FROM rows WHERE {where} AND mev_type != 'none' ORDER BY {self.TX_SORTS[sort]} "
                    "LIMIT ? OFFSET ?",
                    (*args, limit, offset),
                )
            ]

        def load_totals():
            c = COUNTED_SQL
            return dict(
                self.conn()
                .execute(
                    f"SELECT COUNT(*) n, SUM({c}) cycles, COALESCE(SUM(CASE WHEN {c} THEN gross_usd END),0) gross, "
                    f"COALESCE(SUM(CASE WHEN {c} THEN net_usd END),0) net, SUM({c} AND trigger_type='backrun') backruns, "
                    f"SUM({c} AND trigger_type IN ('top_of_block','top_of_flashblock')) tob, "
                    f"COUNT(DISTINCT CASE WHEN {c} AND operator=? THEN tx_from END) wallets, "
                    f"MIN(block_number) lo, MAX(block_number) hi FROM rows WHERE {where} AND mev_type != 'none'",
                    (address, *args),
                )
                .fetchone()
            )

        rows = (
            load_rows()
            if offset
            else self.cached(f"bot_rows:{address}:{public}:{sort}:{limit}", 10.0, load_rows)
        )
        t = self.cached(f"bot_totals:{address}:{public}", 30.0, load_totals)
        return {
            "address": address,
            "transactions": t["n"],
            "cycles": t["cycles"] or 0,
            "gross_usd": round(t["gross"], 2),
            "net_usd": round(t["net"], 2),
            "backruns": t["backruns"] or 0,
            "top_of_block": t["tob"] or 0,
            "wallets": t["wallets"] or 0,
            "first_block": t["lo"],
            "last_block": t["hi"],
            "rows": rows,
        }
