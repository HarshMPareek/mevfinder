"""Rolling windows: the store keeps hourly accumulators next to the daily ones; a window's figures are the sum of its
hours and equal what the days say when the window spans them; rebuilds and the backfill reproduce the hours exactly;
lists page and sort over a window with a stable total."""

from __future__ import annotations

import json
import time

from arbkit.chain.spec import load_chain
from arbkit.live import store as livedb
from arbkit.site.live import LiveChainData
from tests.live.test_store import row

SPEC = load_chain("base")
NOW = int(time.time()) // 3600 * 3600


def report(blocks):
    rows, infos = [], []
    for n, (ts, block_rows) in blocks.items():
        rows.extend(block_rows)
        infos.append(
            {
                "number": n,
                "hash": f"0xh{n}",
                "parent_hash": f"0xh{n - 1}",
                "timestamp": ts,
                "transactions": len(block_rows),
            }
        )
    return {"created_at": "2026-11-01T00:00:00", "classifier_version": "test", "rows": rows, "blocks": infos}


def store(tmp_path):
    """Cycles at 80 h, 50 h, 20 h and 1 h ago: 3-day window holds three, 24 h two, 7 days all four."""
    conn = livedb.connect(tmp_path / "base.sqlite")
    ages = {1: 80, 2: 50, 3: 20, 4: 1}
    blocks = {
        n: (
            NOW - h * 3600 + 60,
            [
                row(n, 0),
                row(
                    n,
                    1,
                    mev="atomic_backrun",
                    assessment="trace_supported",
                    gross=float(10 * n),
                    net=float(n),
                    to=f"0xbot{n % 2}",
                ),
            ],
        )
        for n, h in ages.items()
    }
    livedb.store_report(conn, SPEC, report(blocks), source="live")
    livedb.set_meta(conn, "floor", 1)
    return conn, LiveChainData("base", tmp_path / "base.sqlite")


def hours_json(conn):
    return {
        r["hour"]: (json.loads(r["acc"]), json.loads(r["econ"]))
        for r in conn.execute("SELECT hour, acc, econ FROM hours")
    }


def test_hours_add_up_to_the_days(tmp_path):
    conn, _ = store(tmp_path)
    hours = livedb.hourly(conn, 0)
    days = {r["day"]: json.loads(r["acc"]) for r in conn.execute("SELECT day, acc FROM days")}
    assert len(hours) == 4
    assert (
        round(sum(sum(a["gross_usd"].values()) for a, _ in hours.values()), 6)
        == round(sum(sum(d["gross_usd"].values()) for d in days.values()), 6)
        == 100.0
    )


def test_rebuild_reproduces_the_hours(tmp_path):
    conn, _ = store(tmp_path)
    before = hours_json(conn)
    for day in [r["day"] for r in conn.execute("SELECT day FROM days")]:
        livedb.rebuild_day(conn, SPEC, day)

    def strip(h):
        return {k: (a, {**e, "bots": sorted(e["bots"])}) for k, (a, e) in h.items()}

    assert strip(hours_json(conn)) == strip(before)


def test_windows_sum_their_hours_and_say_where_they_start(tmp_path):
    _, data = store(tmp_path)
    s24, s72, s168 = data.summary(hours=24), data.summary(hours=72), data.summary(hours=168)
    assert (s24["cycles"], s24["gross_usd"]) == (2, 70.0)
    assert (s72["cycles"], s72["gross_usd"]) == (3, 90.0)
    assert (s168["cycles"], s168["gross_usd"]) == (4, 100.0)
    assert s24["window_start"] == time.strftime("%Y-%m-%dT%H:00:00Z", time.gmtime(livedb.window_start(24)))
    assert data.economics(hours=24)["totals"]["gross"] == 70.0
    days = data.daily_for(None, hours=72)["days"]
    assert sum(d["cycles"] for d in days) == 3
    first = days[0]  # a day the window cuts into says from when (unless its first data is at midnight anyway)
    assert first.get("window_from") is None or (len(first["window_from"]) == 5 and first["coverage"] is None)


def test_lists_page_and_sort_with_a_stable_total(tmp_path):
    _, data = store(tmp_path)
    assert data.transactions_total(hours=168) == data.transactions_total(hours=168) == 4
    assert data.transactions_total(hours=24) == 2
    by_usd = [r["gross_usd"] for r in data.transactions(10, sort="usd", hours=168)]
    assert by_usd == [40.0, 30.0, 20.0, 10.0]
    assert [r["gross_usd"] for r in data.transactions(10, sort="usd_asc", hours=168)] == by_usd[::-1]
    assert [r["gross_usd"] for r in data.transactions(2, sort="usd", offset=2, hours=168)] == [20.0, 10.0]
    assert [r["block_number"] for r in data.transactions(10, sort="oldest")] == [1, 2, 3, 4]
    assert [b["number"] for b in data.blocks_list(2, 0, "gross", 168)] == [4, 3] and data.blocks_total(
        24
    ) == 2
    ranked = data.operators_ranked(168, "gross")
    assert [o["address"] for o in ranked] == ["0xbot0", "0xbot1"] and ranked[0]["gross_usd"] == 60.0
    assert [o["address"] for o in data.operators_ranked(24, "net")] == ["0xbot0", "0xbot1"]


def test_sorting_uses_the_figures_the_table_shows(tmp_path):
    conn = livedb.connect(tmp_path / "base.sqlite")
    ts = NOW - 3600
    sandwich = {"role": "back", "profit_usd": 50.0, "net_usd": 45.0, "bundle_id": "b"}
    livedb.store_report(
        conn,
        SPEC,
        report(
            {
                1: (
                    ts,
                    [
                        row(1, 0, mev="atomic_backrun", assessment="trace_supported", gross=20.0, net=5.0),
                        row(1, 1, mev="sandwich_back", sandwich=sandwich),
                    ],
                )
            }
        ),
        source="live",
    )
    data = LiveChainData("base", tmp_path / "base.sqlite")
    assert [r["mev_type"] for r in data.transactions(10, sort="usd")] == ["sandwich_back", "atomic_backrun"]
    assert [r["mev_type"] for r in data.transactions(10, sort="net")] == ["sandwich_back", "atomic_backrun"]
    assert [r["mev_type"] for r in data.transactions(10, sort="net_asc")] == [
        "atomic_backrun",
        "sandwich_back",
    ]
