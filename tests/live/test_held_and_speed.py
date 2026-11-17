"""Held families (econ.HELD_TYPES) stay stored and labelled but out of every total; the migration that recounts an
existing store matches a store built under the rule; list pages are the same rows in the same order however the
query is planned; a rolling window's cached value is served while the next hour's is computed."""

from __future__ import annotations

import json
import threading
import time

from arbkit.live import store as livedb
from arbkit.site.live import LiveChainData
from arbkit.value import econ
from tests.live.test_store import row
from tests.live.test_windows import NOW, SPEC, report


def blocks():
    """Two hours: a counted backrun and an embedded cycle (held) in block 1, another backrun and a sandwich in 2."""
    sandwich = {"role": "back", "profit_usd": 3.0, "net_usd": 2.0, "bundle_id": "b"}
    return {
        1: (
            NOW - 7200 + 60,
            [
                row(1, 0),
                row(1, 1, mev="atomic_backrun", assessment="trace_supported", gross=10.0, net=4.0),
                row(
                    1,
                    2,
                    mev="embedded_cycle",
                    assessment="trace_supported",
                    gross=500.0,
                    net=0.0,
                    to="0xhost",
                ),
            ],
        ),
        2: (
            NOW - 3600 + 60,
            [
                row(
                    2,
                    0,
                    mev="atomic_mid_block",
                    assessment="trace_supported",
                    gross=20.0,
                    net=6.0,
                    to="0xbot2",
                ),
                row(2, 1, mev="sandwich_back", sandwich=sandwich),
            ],
        ),
    }


def build(path, held=None, monkeypatch=None):
    if held is not None:
        monkeypatch.setattr(econ, "HELD_TYPES", held)
    conn = livedb.connect(path)
    livedb.store_report(conn, SPEC, report(blocks()), source="live")
    livedb.set_meta(conn, "floor", 1)
    if held is not None:
        monkeypatch.undo()
    return conn


def accs(conn):
    out = {}
    for table, key in (("days", "day"), ("hours", "hour")):
        for r in conn.execute(f"SELECT {key} k, acc, econ FROM {table}"):
            e = json.loads(r["econ"])
            out[(table, r["k"])] = (json.loads(r["acc"]), {**e, "bots": sorted(e["bots"])})
    return out


def test_held_rows_are_listed_privately_but_never_counted(tmp_path):
    build(tmp_path / "base.sqlite")
    data = LiveChainData("base", tmp_path / "base.sqlite")
    s = data.summary(hours=168)
    assert (s["cycles"], s["gross_usd"], s["net_usd"]) == (2, 30.0, 10.0)
    assert data.economics(hours=168)["totals"]["gross"] == 30.0
    assert [o["address"] for o in data.operators_ranked(168, "gross")] == ["0xbot2", "0xbot"]
    assert {b["number"]: (b["cycles"], b["gross_usd"]) for b in data.blocks_list(10)} == {
        2: (1, 20.0),
        1: (1, 10.0),
    }
    assert data.bot("0xhost")["cycles"] == 0 and data.bot("0xhost")["gross_usd"] == 0
    day = data.daily_for(None, hours=168)["days"]
    assert sum(d["observed"]["held_gross_usd"] for d in day) == 500.0 and sum(d["cycles"] for d in day) == 2
    assert "embedded_cycle" not in {r["mev_type"] for r in data.transactions(50, public=True)}
    assert "embedded_cycle" in {r["mev_type"] for r in data.transactions(50, public=False)}
    assert data.bot("0xhost", public=True)["transactions"] == 0


def test_recount_turns_an_old_store_into_the_new_one(tmp_path, monkeypatch):
    fresh = build(tmp_path / "fresh.sqlite")
    old = build(tmp_path / "old.sqlite", held=frozenset(), monkeypatch=monkeypatch)  # stored before the rule
    assert old.execute("SELECT cycles, gross_usd FROM blocks WHERE number=1").fetchone()[:] == (2, 510.0)
    out = livedb.recount_held(old, SPEC)
    assert out["blocks"] == 1 and out["days"] >= 1

    def block(c):
        return [tuple(r) for r in c.execute("SELECT number, cycles, gross_usd FROM blocks ORDER BY number")]

    assert block(old) == block(fresh) == [(1, 1, 10.0), (2, 1, 20.0)]
    assert accs(old) == accs(fresh)
    livedb.recount_held(old, SPEC)  # idempotent
    assert accs(old) == accs(fresh) and block(old) == block(fresh)


def test_latest_lists_match_a_plain_sort(tmp_path):
    conn = livedb.connect(tmp_path / "base.sqlite")
    kinds = ["atomic_backrun", "atomic_mid_block", "sandwich_back", "loss_cycle", "jit_liquidity"]
    big = {}
    for n in range(1, 41):
        big[n] = (
            NOW - 3600 * (n % 30) - n,
            [
                row(
                    n,
                    i,
                    mev=kinds[(n + i) % 5],
                    assessment="trace_supported" if (n + i) % 5 < 2 else "not_arb",
                    gross=float(n + i),
                )
                for i in range(3)
            ],
        )
    livedb.store_report(conn, SPEC, report(big), source="live")
    data = LiveChainData("base", tmp_path / "base.sqlite")
    plain = lambda q, a: [(r[0], r[1]) for r in conn.execute(q, a)]  # noqa: E731
    for public in (True, False):
        for types in (None, {"atomic_backrun", "loss_cycle"}, {"sandwich_back"}):
            for sort, d in (("latest", "DESC"), ("oldest", "ASC")):
                for offset in (0, 7, 50):
                    got = [
                        (r["block_number"], r["tx_index"])
                        for r in data.transactions(
                            10, types, public=public, sort=sort, offset=offset, hours=168
                        )
                    ]
                    where, args = data._tx_where(types, None, public, data.window_start_block(168))
                    want = plain(
                        f"SELECT block_number, tx_index FROM rows WHERE {where} ORDER BY block_number {d}, tx_index {d} "
                        "LIMIT 10 OFFSET ?",
                        [*args, offset],
                    )
                    assert got == want, (public, types, sort, offset)


def test_a_rolled_window_serves_the_previous_hour_while_computing(tmp_path):
    d = LiveChainData.__new__(LiveChainData)
    d.lock, d._cache, d._slots, d._refreshing = threading.Lock(), {}, {}, set()
    gate = threading.Event()
    assert d.cached("summary:hour1", 5, lambda: "hour 1", slot="summary:168") == "hour 1"

    def slow():
        gate.wait(2)
        return "hour 2"

    assert d.cached("summary:hour2", 5, slow, slot="summary:168") == "hour 1"  # served at once
    gate.set()
    for _ in range(200):
        if not d._refreshing:
            break
        time.sleep(0.01)
    assert d.cached("summary:hour2", 5, slow, slot="summary:168") == "hour 2"
    assert "summary:hour1" not in d._cache  # the old hour is dropped once the new one is in
