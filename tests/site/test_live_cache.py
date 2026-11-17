"""Live aggregates: a stale value is served at once and recomputed in the background, never twice at the same time."""

import threading
import time

from arbkit.site import live as liveui
from arbkit.site.live import LiveChainData


def make():
    d = LiveChainData.__new__(LiveChainData)
    d.lock = threading.Lock()
    d._cache = {}
    d._refreshing = set()
    return d


def test_stale_value_is_served_while_it_refreshes(monkeypatch):
    d = make()
    gate = threading.Event()
    calls = []

    def slow():
        calls.append(1)
        gate.wait(2)
        return len(calls)

    gate.set()
    assert d.cached("k", 5, slow) == 1  # first request computes
    gate.clear()
    d._cache["k"] = (time.time() - 10, 1)  # now stale
    assert d.cached("k", 5, slow) == 1 and d.cached("k", 5, slow) == 1  # served at once, one refresh started
    gate.set()
    for _ in range(100):
        if not d._refreshing:
            break
        time.sleep(0.01)
    assert len(calls) == 2 and d.cached("k", 5, slow) == 2


def test_too_old_value_is_recomputed_before_answering(monkeypatch):
    d = make()
    d._cache["k"] = (time.time() - liveui.MAX_STALE - 1, "old")
    assert d.cached("k", 5, lambda: "new") == "new"
