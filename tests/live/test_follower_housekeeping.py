"""The cache prune runs off the follower's event loop (2026-11-08): awaited inline it stopped BSC and Robinhood followers
for minutes every 30 min, and their nodes trace only ~128 blocks back, so the blocks behind each pause went to untraced
catch-up (never counted): BSC 26,083 blocks on 2026-11-07, Robinhood 61,014."""

import asyncio
import os
import threading
import time

from arbkit.live import store as livedb
from arbkit.live.follower import Follower


def test_housekeeping_returns_at_once_and_prunes_in_the_background(tmp_path, monkeypatch):
    monkeypatch.setenv("BSC_HTTP_URL", "http://127.0.0.1:9")
    logs: list[str] = []
    f = Follower("bsc", directory=tmp_path / "db", log=logs.append)
    data = tmp_path / "data"
    (data / "traces").mkdir(parents=True)
    (data / "blocks").mkdir()
    f.settings.data = data
    old = time.time() - 10 * 86_400
    for i in range(50):
        p = data / "traces" / f"0xblock_0x{i:064x}.json.gz"
        p.write_bytes(b"x")
        os.utime(p, (old, old))
    fresh = data / "traces" / "0xblock_0xfresh.json.gz"
    fresh.write_bytes(b"x")
    livedb.set_meta(f.conn, "started_at", old - 100)
    livedb.set_meta(f.conn, "floor", 1)
    livedb.set_meta(f.conn, "cursor", 1000)

    class Rpc:
        async def block_number(self):
            return 1002

    f.rpc = Rpc()
    f.resolver = type("Pools", (), {"table": {}})()  # set by run() in a live follower
    gate = threading.Event()
    prune = f._prune_files

    def slow_prune(*args):
        gate.wait(5)  # a scan of millions of files: the follower must not wait for it
        return prune(*args)

    f._prune_files = slow_prune

    async def run():
        t0 = time.time()
        await f.housekeeping()
        assert time.time() - t0 < 1 and f.prune_task is not None and not f.prune_task.done()
        await f.housekeeping()  # a second call while the pass runs starts nothing new
        gate.set()
        await f.prune_task
        await asyncio.sleep(0)  # the done callback runs on the loop

    asyncio.run(run())
    assert sorted(p.name for p in (data / "traces").iterdir()) == [fresh.name]
    assert any("housekeeping removed 50 cached files" in line for line in logs)
    assert livedb.get_meta(f.conn, "failed_attempts") is not None
