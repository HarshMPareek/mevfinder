"""Live store: totals kept by the store equal the in-memory aggregation; a re-stored block replaces, never adds;
the follower's backlog merges and hands out newest-first; singleton pools are learned from Initialize logs; the
dashboard reads lists, blocks and transactions back from the store."""

from __future__ import annotations

import json

from arbkit.chain.spec import load_chain
from arbkit.live import store as livedb
from arbkit.live.follower import Follower
from arbkit.site.live import LiveChainData
from arbkit.value.daily import aggregate_daily

SPEC = load_chain("base")
TS = 1_793_404_800  # 2026-11-01 00:00:00 UTC


def row(block, index, *, mev="none", assessment="not_arb", gross=None, net=None, to="0xbot", sandwich=None):
    return {
        "block_number": block,
        "tx_index": index,
        "tx_hash": f"0x{block:030x}{index:034x}",
        "tx_from": "0xeoa",
        "tx_to": to,
        "assessment": assessment,
        "arb_class": "cycle" if assessment != "not_arb" else "single_swap",
        "mev_type": mev,
        "trigger_type": "backrun" if assessment == "trace_supported" else None,
        "gross_usd": gross,
        "net_usd": net,
        "fee_usd": 0.01 if gross is not None else None,
        "builder_tip_usd": 0.0,
        "native_usd": 3000.0,
        "profit_symbol": "WETH",
        "gross_profit_raw": "1000",
        "gross_source": "trace",
        "reasons": [],
        "fees": {
            "total_fee_wei": 1000,
            "priority_fee_wei": 10,
            "total_fee_eth": "0.000000000000001",
            "is_system": False,
        },
        "economics": {"total_fee_wei": 1000, "priority_fee_wei": 10, "l1_fee_wei": 5},
        "trace": {"status": "ok", "venues": ["0xp"]},
        **({"sandwich": sandwich} if sandwich else {}),
    }


def report(blocks, created="2026-11-01T00:00:00"):
    rows, infos = [], []
    for n, block_rows in blocks.items():
        rows.extend(block_rows)
        infos.append(
            {
                "number": n,
                "hash": f"0xh{n}",
                "parent_hash": f"0xh{n - 1}",
                "timestamp": TS + 2 * n,
                "transactions": len(block_rows),
            }
        )
    return {"created_at": created, "classifier_version": "test", "rows": rows, "blocks": infos}


def sample():
    return report(
        {
            1: [row(1, 0), row(1, 1, mev="atomic_backrun", assessment="trace_supported", gross=5.0, net=4.0)],
            2: [
                row(2, 0, mev="loss_cycle", assessment="trace_supported_loss", gross=-1.0),
                row(2, 1),
                row(2, 2),
            ],
            3: [
                row(
                    3,
                    0,
                    mev="atomic_mid_block",
                    assessment="trace_supported",
                    gross=0.5,
                    net=0.2,
                    to="0xother",
                ),
                row(
                    3,
                    1,
                    mev="sandwich_back",
                    sandwich={"role": "back", "profit_usd": 2.0, "net_usd": 1.5, "bundle_id": "b"},
                ),
            ],
        }
    )


def test_store_totals_equal_the_in_memory_aggregation(tmp_path):
    conn = livedb.connect(tmp_path / "base.sqlite")
    rep = sample()
    stats = livedb.store_report(conn, SPEC, rep, source="import:test")
    assert stats == {"blocks": 3, "rows": 4, "replaced": 0}
    want, got = aggregate_daily(SPEC, [rep])["days"], livedb.daily(conn, SPEC)["days"]
    keys = (
        "blocks_covered",
        "transactions",
        "cycles",
        "loss_cycles",
        "sandwiches",
        "mev_types",
        "observed",
        "positions",
        "top_operators",
    )
    assert [{k: d[k] for k in keys} for d in got] == [{k: d[k] for k in keys} for d in want]
    econ = livedb.economics(conn, SPEC)
    assert econ["cycles"] == 2 and econ["totals"]["gross"] == 5.5 and econ["bots"] == 2


def test_restoring_a_block_replaces_it_and_totals_do_not_double(tmp_path):
    conn = livedb.connect(tmp_path / "base.sqlite")
    livedb.store_report(conn, SPEC, sample(), source="live")
    again = report(
        {1: [row(1, 0), row(1, 1, mev="atomic_backrun", assessment="trace_supported", gross=7.0, net=6.0)]}
    )
    stats = livedb.store_report(conn, SPEC, again, source="live")
    assert stats["replaced"] == 1
    day = livedb.daily(conn, SPEC)["days"][0]
    assert day["blocks_covered"] == 3 and day["cycles"] == 2
    assert day["observed"]["atomic_gross_usd"] == 7.5
    livedb.delete_blocks(conn, SPEC, [3])
    day = livedb.daily(conn, SPEC)["days"][0]
    assert day["blocks_covered"] == 2 and day["cycles"] == 1 and day["sandwiches"] == 0


def test_plain_transactions_are_kept_as_a_slim_map_with_their_fees(tmp_path):
    conn = livedb.connect(tmp_path / "base.sqlite")
    livedb.store_report(conn, SPEC, sample(), source="live")
    info, rows = livedb.block_rows(conn, 2)
    assert info["transactions"] == 3 and [r["tx_index"] for r in rows] == [0, 1, 2]
    plain = rows[1]
    assert (
        plain["mev_type"] == "none"
        and plain["fees"]["priority_fee_wei"] == 10
        and plain["arb_class"] == "single_swap"
    )
    assert conn.execute("SELECT COUNT(*) FROM rows").fetchone()[0] == 4  # only typed rows are stored in full


def test_backlog_merges_ranges_and_hands_out_the_newest_first(tmp_path):
    follower = Follower.__new__(Follower)
    follower.conn = livedb.connect(tmp_path / "x.sqlite")
    follower.set_backlog([[10, 20], [21, 25], [40, 41], [30, 29]])
    assert follower.backlog() == [[10, 25], [40, 41]]
    assert follower.take_from_backlog(5) == [40, 41]
    assert follower.take_from_backlog(4) == [22, 23, 24, 25]
    assert follower.backlog() == [[10, 21]]


def test_singleton_pools_are_learned_from_initialize_logs():
    from arbkit.scan.pools import UNISWAP_V4_INIT, HistoricalPools
    from mevlab.decode.signatures import event_topic

    manager = next(iter(SPEC.pool_managers))
    resolver = HistoricalPools(None, None, {}, SPEC.pool_managers, max_log_range=1000)
    pool_id = "0x" + "ab" * 32
    word = lambda v: f"{v:064x}"  # noqa: E731
    log = {
        "address": manager,
        "topics": [
            event_topic(UNISWAP_V4_INIT),
            pool_id,
            "0x" + "0" * 24 + "11" * 20,
            "0x" + "0" * 24 + "22" * 20,
        ],
        "data": "0x" + word(3000) + word(60) + "0" * 24 + "33" * 20 + word(2**96) + word(0),
    }
    assert resolver.learn([{"logs": [log]}]) == 1
    meta = resolver.table[f"{manager}:{pool_id}"]
    assert meta.token0 == "0x" + "11" * 20 and meta.token1 == "0x" + "22" * 20 and meta.fee == 3000


def test_dashboard_reads_lists_blocks_and_transactions_from_the_store(tmp_path):
    conn = livedb.connect(tmp_path / "base.sqlite")
    livedb.store_report(conn, SPEC, sample(), source="live")
    data = LiveChainData("base", tmp_path / "base.sqlite")
    recent = data.transactions(10)
    assert [r["block_number"] for r in recent] == [3, 3, 2, 1]
    assert [r["tx_index"] for r in data.transactions(10, sort="usd")][:1] == [
        1
    ]  # block 1's $5 cycle is the largest
    assert {r["mev_type"] for r in data.transactions(10, public=True)} <= {
        "atomic_backrun",
        "atomic_mid_block",
        "sandwich_back",
        "loss_cycle",
    }
    block = data.block(2)
    assert [r["kind"] for r in block["rows"]] == ["losing", "other", "other"]
    tx = data.transaction(recent[-1]["tx_hash"])
    assert tx["gross_usd"] == 5.0 and tx["kind"] == "arbitrage"
    summary = data.summary()
    assert summary["cycles"] == 2 and summary["blocks_covered"] == 3 and summary["live"]["running"] is False
    bot = data.bot("0xbot")
    assert bot["cycles"] == 1 and bot["gross_usd"] == 5.0
    assert json.dumps(data.live(public=True))  # status + accuracy serialise


def test_a_builder_payment_in_a_separate_transaction_is_charged_to_the_bundle():
    import asyncio

    from arbkit.scan.block import attach_separate_payments

    spec = load_chain("ethereum")
    miner, bot = "0x4838b106fce9647bdf1e7877bf73ce8b0bad5f97", "0x6338afee3ff32599bff2ce3d8b7604bd6d6a41a7"
    back = {
        "tx_hash": "0xback",
        "tx_index": 6,
        "tx_from": "0xe3d5",
        "tx_to": bot,
        "mev_type": "sandwich_back",
        "reasons": [],
        "sandwich": {"role": "back", "back": "0xback", "bundle_builder_tip_wei": "0"},
    }
    cycle = {
        "tx_hash": "0xcyc",
        "tx_index": 20,
        "tx_from": "0xeoa",
        "tx_to": "0xbot2",
        "mev_type": "atomic_backrun",
        "reasons": [],
        "builder_tip_wei": "5",
    }
    plain = {
        "tx_hash": "0xpay2",
        "tx_index": 21,
        "tx_from": "0xeoa",
        "tx_to": miner,
        "mev_type": "none",
        "reasons": [],
    }
    block = {
        "hash": "0xb",
        "transactions": [
            {"hash": "0xback", "transactionIndex": "0x6", "from": "0xe3d5", "to": bot, "value": "0x0"},
            {"hash": "0xpay", "transactionIndex": "0x7", "from": "0x595c", "to": bot, "value": hex(4993)},
            {"hash": "0xcyc", "transactionIndex": "0x14", "from": "0xeoa", "to": "0xbot2", "value": "0x0"},
            {"hash": "0xpay2", "transactionIndex": "0x15", "from": "0xeoa", "to": miner, "value": hex(700)},
            {"hash": "0xuser", "transactionIndex": "0x16", "from": "0xuser", "to": miner, "value": hex(9)},
        ],
    }
    receipts = {h: {"logs": [], "status": "0x1"} for h in ("0xback", "0xpay", "0xcyc", "0xpay2", "0xuser")}

    class Tracer:
        async def call(self, method, params):
            return {
                "type": "CALL",
                "from": "0x595c",
                "to": bot,
                "value": hex(4993),
                "calls": [{"type": "CALL", "from": bot, "to": miner, "value": hex(4993)}],
            }

    rows = [back, cycle, plain]
    asyncio.run(attach_separate_payments(spec, rows, block, receipts, miner, Tracer(), None, False))
    assert back["sandwich"]["bundle_builder_tip_wei"] == "4993"
    assert cycle["builder_tip_wei"] == "705" and cycle["separate_payments"][0]["tx_hash"] == "0xpay2"
    assert plain["pays_builder_for"] == "0xcyc"


def test_a_reaudited_block_replaces_its_old_audit(tmp_path):
    # re-classification audits blocks again: the old audit (of rows that no longer exist) must leave the totals
    conn = livedb.connect(tmp_path / "a.sqlite")
    result = {
        "transactions_fee_checked": 5,
        "fee_failures": [],
        "cycles_checked": 2,
        "gross_mismatches": [],
        "positive_cycles_with_open_losses": [],
        "missing_cached_data": [],
    }
    for lo, hi in [(1, 1), (2, 3), (4, 4), (5, 7)]:
        livedb.store_audit(conn, lo, hi, result)
    livedb.store_audit(conn, 1, 4, {**result, "cycles_checked": 3})
    rows = [
        tuple(r)
        for r in conn.execute(
            "SELECT first_block, last_block, cycles_checked FROM audits ORDER BY first_block"
        )
    ]
    assert rows == [(1, 4, 3), (5, 7, 2)]
    # stores written before the fix: the newest audit wins wherever ranges overlap
    conn.execute(
        "INSERT INTO audits(first_block, last_block, at, fees_checked, fee_failures, cycles_checked, gross_mismatches, "
        "open_losses, missing, detail) VALUES(6, 6, 'later', 1, 0, 1, 0, 0, 0, '{}')"
    )
    assert livedb.dedupe_audits(conn) == 1
    assert [
        tuple(r) for r in conn.execute("SELECT first_block, last_block FROM audits ORDER BY first_block")
    ] == [(1, 4), (6, 6)]


def test_reclassifying_an_imported_block_keeps_it_an_import(tmp_path):
    # live-reclassify --all-sources re-ran the October samples and stored them as followed "live" blocks (2026-11-01):
    # the site's range then started 2026-10-07 and coverage counted samples as continuous following
    conn = livedb.connect(tmp_path / "s.sqlite")
    livedb.store_report(conn, SPEC, sample(), source="import:2026-10-09")
    livedb.store_report(conn, SPEC, sample(), source="live")
    assert {r["source"] for r in conn.execute("SELECT source FROM blocks")} == {"import:2026-10-09"}
