"""On-disk index of a chain's pool metadata cache for the site.

`pool_meta.jsonl` is append-only and large (Base: 9.6M lines, 3.1 GB on 2026-11-04). The site used to re-read the whole
file whenever it changed (the followers append every few seconds) to name the DEX of a route's pools: a Base
transaction page took 23-29 s, and the read (whole text + split) pushed the public site past its 12 GB memory limit
three times that day. Here the lines are indexed into SQLite once and then only the lines appended since the last
offset; a page looks up the few pools it shows. One process updates at a time (a lock file); others only read.
"""

from __future__ import annotations

import fcntl
import json
import sqlite3
import threading
from pathlib import Path

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS pools(key TEXT PRIMARY KEY, token0 TEXT, token1 TEXT, dex TEXT, hooks TEXT);"
    "CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);"
)
BATCH = 50_000


class LazyMap:
    """A read-only mapping answered per key (route code only calls .get)."""

    def __init__(self, fn):
        self.fn = fn

    def get(self, key, default=None):
        value = self.fn(str(key).lower()) if key else None
        return default if value is None else value

    def __contains__(self, key):
        return self.get(key) is not None


class PoolIndex:
    def __init__(self, jsonl: Path, index: Path, managers: dict | None = None):
        self.jsonl, self.index = jsonl, index
        self.managers = {
            str(a).lower(): (e.get("name") if isinstance(e, dict) else e) for a, e in (managers or {}).items()
        }
        self._local = threading.local()

    def _conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            if not self.index.exists():
                return None
            conn = self._local.conn = sqlite3.connect(
                f"file:{self.index}?mode=ro", uri=True, check_same_thread=False, timeout=5
            )
        return conn

    def get(self, key: str) -> tuple | None:
        """key ('pool address' or 'manager:pool_id') -> (token0, token1, dex, hooks), or None."""
        conn = self._conn()
        if conn is None:
            return None
        try:
            row = conn.execute(
                "SELECT token0, token1, dex, hooks FROM pools WHERE key=?", (key.lower(),)
            ).fetchone()
        except sqlite3.Error:
            return None
        return tuple(row) if row else None

    def dex(self, address: str) -> str | None:
        """DEX name of a venue: a registered singleton manager, else the pool's cached metadata."""
        address = address.lower()
        if address in self.managers:
            return self.managers[address]
        meta = self.get(address)
        return meta[2] if meta and meta[2] else None

    def pending_bytes(self) -> int:
        """Bytes of the jsonl not indexed yet (the whole file when there is no index)."""
        size = self.jsonl.stat().st_size if self.jsonl.exists() else 0
        conn = self._conn()
        if conn is None:
            return size
        try:
            row = conn.execute("SELECT v FROM meta WHERE k='offset'").fetchone()
        except sqlite3.Error:
            return size
        return max(0, size - int(row[0])) if row else size

    def update(self, max_lines: int | None = None) -> int:
        """Index the lines appended since the last update. Returns the lines read (0 when another process holds the lock)."""
        if not self.jsonl.exists():
            return 0
        self.index.parent.mkdir(parents=True, exist_ok=True)
        lock = open(self.index.with_suffix(".lock"), "w")  # noqa: SIM115 - held for the whole update
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock.close()
            return 0
        try:
            db = sqlite3.connect(self.index, timeout=30)
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            row = db.execute("SELECT v FROM meta WHERE k='offset'").fetchone()
            offset = int(row[0]) if row else 0
            if self.jsonl.stat().st_size < offset:  # the cache was rewritten: start over
                db.execute("DELETE FROM pools")
                offset = 0
            read, batch = 0, []

            def flush():
                db.executemany("INSERT OR REPLACE INTO pools VALUES(?,?,?,?,?)", batch)
                db.execute("INSERT OR REPLACE INTO meta VALUES('offset', ?)", (str(offset),))
                db.commit()
                batch.clear()

            with self.jsonl.open("rb") as f:
                f.seek(offset)
                for raw in f:
                    if not raw.endswith(b"\n"):
                        try:  # the last line: complete when it parses (a line still being written does not)
                            json.loads(raw)
                        except ValueError:
                            break
                    offset += len(raw)
                    read += 1
                    try:
                        key, meta = json.loads(raw)
                        batch.append(
                            (
                                str(key).lower(),
                                str(meta[0]).lower(),
                                str(meta[1]).lower(),
                                meta[2],
                                str(meta[5] or "").lower() if len(meta) > 5 else "",
                            )
                        )
                    except (ValueError, TypeError, IndexError):
                        pass
                    if len(batch) >= BATCH:
                        flush()
                    if max_lines and read >= max_lines:
                        break
            flush()
            db.close()
            return read
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
