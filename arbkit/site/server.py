"""`./arb ui`: serve the site, keeping each chain's summary warm in the background."""

from __future__ import annotations

import os
import signal
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

from arbkit.chain.spec import ROOT
from arbkit.site.http import make_handler
from arbkit.site.process import PID_FILE, read_pidfile, write_pidfile
from arbkit.site.store import Store
from arbkit.site.windows import LIVE_WINDOW_DAYS, chain_summary

KEEP_WARM_S = 20.0  # how often the keeper looks at the default views (a fresh value costs nothing)
CYCLE_TYPES = {
    "atomic_backrun",
    "atomic_top_of_block",
    "atomic_mid_block",
    "atomic_top_of_flashblock",
}  # ui/app.js CYCLE_TYPES


def _keep_warm(store, name: str, once: bool = False) -> None:
    """Compute the views a visitor's first page needs before anyone asks, and keep them fresh, for one live chain (one
    thread per chain, so a restart warms all six at once): the chain list's summary and, in the site's default window,
    the overview (summary, days, bots, economics, recent and largest transactions), status, latest blocks and first
    list pages. A fresh value costs nothing; a stale one is recomputed in the background (liveui.cached), so a visitor
    never waits on the stores' aggregates. Without it the first visitor after 10 quiet minutes, or after the window
    moved to a new hour, waited 5-13 s for an overview (2026-11-03)."""
    hours, public = LIVE_WINDOW_DAYS * 24, store.public
    chain = store.get(name)
    if not hasattr(chain, "db_file"):
        return
    while True:
        try:
            chain_summary(chain)
            chain.daily_for(None, hours=hours)
            chain.operators(25, hours=hours)
            chain.economics(hours=hours)
            chain.live(public)
            chain.transactions(limit=50, public=public)
            chain.transactions(5, CYCLE_TYPES, public=public, sort="usd", hours=hours)
            chain.transactions(50, None, None, public=public, sort="latest", offset=0, hours=hours)
            chain.transactions_total(None, None, public=public, hours=hours)
            chain.blocks_list(60)
            chain.blocks_list(50, 0, "latest", hours)
            chain.blocks_total(hours)
            chain.pool_index().update()  # the lines the follower appended since the last pass
        except Exception:  # noqa: BLE001 - warming is best effort; a request computes it anyway
            pass
        if once:
            return
        time.sleep(KEEP_WARM_S)


def serve(
    port: int = 8787,
    reports_root: Path | None = None,
    console=None,
    public: bool = False,
    host: str = "127.0.0.1",
    live_db: Path | None = None,
) -> None:
    """Serve until Ctrl-C or SIGTERM (`./arb ui --stop`); the pid file lets the stop switch find this process.
    `public` serves the visitor view (labels and money only, no proofs or scans, rate-limited). `live_db`: directory
    of the followers' stores; a chain with a store there is served from it."""
    store = Store(reports_root or ROOT / "reports", public=public, live_db=live_db)
    server = ThreadingHTTPServer((host, port), make_handler(store))
    write_pidfile(PID_FILE, os.getpid(), port)
    if live_db:
        for name in store.chains:
            threading.Thread(
                target=_keep_warm, args=(store, name), daemon=True, name=f"keep-warm-{name}"
            ).start()

    def _terminate(_signum, _frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, _terminate)
    if console:
        mode = "public view: no proofs, scans or validation" if public else "private view"
        console.print(
            f"Dashboard: http://{host}:{port}/  ({mode}; API under /api/; Ctrl-C or `./arb ui --stop` to stop)",
            style="bold cyan",
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        signal.signal(signal.SIGTERM, previous)
        if read_pidfile(PID_FILE) and read_pidfile(PID_FILE)[0] == os.getpid():
            PID_FILE.unlink(missing_ok=True)
