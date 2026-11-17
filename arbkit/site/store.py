"""Every chain's data for the server (live SQLite stores when present, else saved reports) and the per-visitor
rate limit of the public site."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from dotenv import load_dotenv

from arbkit.chain.settings import DEFAULT_CHAIN
from arbkit.chain.spec import ROOT, list_chains
from arbkit.site.jobs import JobManager
from arbkit.site.reports import ChainData

PUBLIC_RATE = (240, 60.0)  # API requests per window (seconds) per client address


class RateLimiter:
    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self.hits: dict[str, list[float]] = {}
        self.lock = threading.Lock()

    MAX_CLIENTS = 50_000  # past this many tracked addresses, idle ones are dropped (memory stays bounded)

    def allow(self, client: str) -> bool:
        now = time.monotonic()
        with self.lock:
            if len(self.hits) > self.MAX_CLIENTS:
                self.hits = {k: v for k, v in self.hits.items() if v and now - v[-1] < self.window}
            recent = [t for t in self.hits.get(client, []) if now - t < self.window]
            if len(recent) >= self.limit:
                self.hits[client] = recent
                return False
            recent.append(now)
            self.hits[client] = recent
            return True


class Store:
    def __init__(self, reports_root: Path, public: bool = False, live_db: Path | None = None):
        self.reports_root = reports_root
        self.public = public
        self.live_db = live_db
        self.limiter = RateLimiter(*PUBLIC_RATE)
        self.chains = {name: self._chain(name) for name in list_chains()}
        self.jobs = JobManager(reports_root if reports_root != ROOT / "reports" else None)
        # The chain a fresh browser opens on: the same one `./arb` uses when --chain is omitted.
        load_dotenv(ROOT / ".env", override=False)
        wanted = (os.environ.get("ARB_CHAIN") or DEFAULT_CHAIN, DEFAULT_CHAIN, *self.chains)
        self.default_chain = next((name for name in wanted if name in self.chains), None)

    def _chain(self, name: str) -> ChainData:
        """A chain with a live store (`--live-db DIR`, DIR/<chain>.sqlite) is served from it; others from reports."""
        if self.live_db and (self.live_db / f"{name}.sqlite").exists():
            from arbkit.site.live import LiveChainData

            return LiveChainData(name, self.live_db / f"{name}.sqlite")
        return ChainData(name, self.reports_root / name)

    def get(self, name: str) -> ChainData:
        if name not in self.chains:
            raise KeyError(name)
        chain = self.chains[name]
        if self.live_db and not hasattr(chain, "db_file") and (self.live_db / f"{name}.sqlite").exists():
            chain = self.chains[name] = self._chain(
                name
            )  # the follower created the store after the server started
        return chain
