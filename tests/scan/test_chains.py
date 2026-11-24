"""Multi-chain layer: chain specs, family fees, position models, sandwiches, pricing, daily accounting."""

import json

import pytest
from eth_abi import encode

from arbkit.chain.spec import list_chains, load_chain
from arbkit.scan.block import BlockContext, classify_block, mev_type_of
from arbkit.scan.positions import Touch, TriggerContext
from arbkit.scan.sandwich import find_sandwiches, operator_result, swap_direction
from arbkit.value.daily import aggregate_daily, sample_blocks
from arbkit.value.fees import receipt_fees
from arbkit.value.pricing import PriceBook, implied_amount, value_rows
from mevlab.block.position import TxTouch, classify_positions
from mevlab.decode.signatures import (
    SIG_BALANCER_V3_SWAP,
    SIG_CURVE_CRYPTO_EXCHANGE,
    SIG_CURVE_STABLE_EXCHANGE,
    SIG_INFINITY_CL_SWAP,
    SIG_V3_SWAP,
    event_topic,
)
from mevlab.decode.swaps import decode_swap_log
from tests.mevlab.test_receipt import (
    BOT,
    EOA,
    POOL_A,
    POOL_B,
    RESOLVER,
    USDC,
    WETH,
    make_tx,
    solidly_swap,
    t,
    transfer,
    v3_swap,
)

BASE = load_chain("base")


def test_every_chain_spec_loads_with_valid_settings():
    names = list_chains()
    assert {"base", "ethereum", "robinhood", "arbitrum", "optimism", "bsc"} <= set(names)
    for name in names:
        spec = load_chain(name)
        assert spec.chain_id > 0 and spec.weth.startswith("0x") and spec.weth == spec.weth.lower()
        assert spec.family in {"op-stack", "arbitrum", "ethereum", "bsc"}
        assert spec.ordering_policy in {"fee_sorted", "builder", "fcfs"}
        assert spec.explorer_tx.startswith("https://")
        assert spec.blocks_per_day > 0
        assert any(tok.price == "native" for tok in spec.tokens), name
    assert load_chain("robinhood").trigger_window_blocks == 50
    assert load_chain("ethereum").ordering_policy == "builder"
    assert 126 in load_chain("base").system_tx_types and 106 in load_chain("robinhood").system_tx_types
    bsc = load_chain("bsc")
    assert "0x4848489f0b2bedd788c696e2d79b6b69d7484848" in bsc.recipients_of_kind("builder")
    assert "0x743be0db30148336a3db479f19d4e1828b293869" in load_chain("base").recipients_of_kind("kickback")
    with pytest.raises(ValueError, match="Unknown chain"):
        load_chain("solana")


def _block_with(tx, receipt, base_fee=hex(1_000_000), number="0x10"):
    for obj in (tx, receipt):
        obj["blockHash"] = "0x" + "a1" * 32
        obj["blockNumber"] = number
        obj["transactionIndex"] = "0x0"
    return {
        "number": number,
        "hash": tx["blockHash"],
        "timestamp": "0x10",
        "baseFeePerGas": base_fee,
        "miner": "0x" + "c0" * 20,
        "transactions": [tx],
    }


def test_fee_families_arbitrum_ethereum_bsc():
    tx, receipt = make_tx([])
    receipt.pop("l1Fee")
    receipt["gasUsedForL1"] = hex(50_000)
    block = _block_with(tx, receipt)
    fees = receipt_fees(tx, receipt, block, load_chain("robinhood"))
    assert fees["complete"] and fees["total_fee_wei"] == 150_000 * 1_500_000
    assert fees["l1_fee_wei"] == 50_000 * 1_500_000 and fees["l1_fee_included_in_execution"]
    assert fees["priority_fee_wei"] == 150_000 * 500_000
    eth = receipt_fees(tx, receipt, block, load_chain("ethereum"))
    assert eth["complete"] and eth["total_fee_wei"] == 150_000 * 1_500_000 and eth["l1_fee_wei"] == 0
    block["baseFeePerGas"] = "0x0"
    bsc = receipt_fees(tx, receipt, block, load_chain("bsc"))
    assert (
        bsc["priority_fee_wei"] == bsc["execution_fee_wei"] == 150_000 * 1_500_000
        and bsc["base_fee_wei"] == 0
    )
    tx["type"] = "0x6a"
    system = receipt_fees(tx, receipt, block, load_chain("robinhood"))
    assert system["is_system"] and system["total_fee_wei"] == 0


def test_builder_chains_mark_first_user_tx_top_of_block_regardless_of_fee():
    touches = [
        TxTouch(0, "0xsys", 0, is_system=True),
        TxTouch(1, "0xa", 0, {POOL_A: 1}),
        TxTouch(2, "0xb", 10**9, {POOL_B: -1}),
    ]
    by_fee = classify_positions(touches, {1, 2}, tob_rule="fee")
    assert by_fee[1].trigger_type == "mid_block"
    by_order = classify_positions(touches, {1, 2}, tob_rule="first")
    assert by_order[1].trigger_type == "top_of_block" and by_order[2].trigger_type == "mid_block"


def test_fcfs_cross_block_trigger_search():
    ctx = TriggerContext(window_blocks=50)
    ctx.record([Touch(100, 3, "0xtrigger", "0x" + "11" * 20, "0x" + "22" * 20, {POOL_A: 1})])
    me = Touch(102, 1, "0xarb", EOA, BOT, {POOL_A: -1, POOL_B: 1})
    found = ctx.find(me)
    assert found and found[0].tx_hash == "0xtrigger" and found[1] == POOL_A
    far = Touch(160, 1, "0xlate", EOA, BOT, {POOL_A: -1})
    assert ctx.find(far) is None  # beyond the window
    same_party = Touch(103, 1, "0xself", "0x" + "11" * 20, "0x" + "22" * 20, {POOL_A: 1})
    assert (
        ctx.find(same_party) is None
    )  # the trigger's own operator reacting again is not a backrun of itself


async def test_fcfs_positions_label_cross_block_backrun_in_engine():
    spec = load_chain("robinhood")
    ctx = BlockContext(spec)
    ctx.triggers.record([Touch(15, 2, "0xtrigger", "0x" + "11" * 20, "0x" + "22" * 20, {POOL_A: 1})])
    ctx.triggers.previous_block = 15
    logs = [
        transfer(WETH, BOT, POOL_A, 10**18, 0),
        v3_swap(POOL_A, 10**18, -3000 * 10**6, 1),
        transfer(USDC, POOL_A, BOT, 3000 * 10**6, 2),
        transfer(USDC, BOT, POOL_B, 3000 * 10**6, 3),
        solidly_swap(POOL_B, 0, 3000 * 10**6, 101 * 10**16, 0, 4),
        transfer(WETH, POOL_B, BOT, 101 * 10**16, 5),
    ]
    tx, receipt = make_tx(logs)
    receipt.pop("l1Fee")
    block = _block_with(tx, receipt)
    rows = await classify_block(spec, block, [receipt], RESOLVER, ctx, trace_mode="none")
    row = rows[0]
    assert row["assessment"] == "event_candidate"
    assert row["trigger_type"] == "backrun" and row["trigger_tx_hash"] == "0xtrigger"
    assert row["trigger_distance_blocks"] == 1 and row["trigger_block_number"] == 15
    assert "cross_block_trigger:1" in row["reasons"]


def _swap_log(pool, a0, a1, idx, sender=BOT):
    return {
        "address": pool,
        "logIndex": hex(idx),
        "topics": [event_topic(SIG_V3_SWAP), t(sender), t(sender)],
        "data": "0x"
        + encode(["int256", "int256", "uint160", "uint128", "int24"], [a0, a1, 2**96, 10**20, 0]).hex(),
    }


def _row(index, tx_hash, frm, to, venues):
    return {
        "tx_index": index,
        "tx_hash": tx_hash,
        "tx_from": frm,
        "tx_to": to,
        "venues": venues,
        "block_number": 16,
        "fees": {"is_system": False, "total_fee_wei": 10**12},
        "assessment": "not_arb",
        "arb_class": "single_swap",
    }


def test_sandwich_detection_confirms_direction_and_measures_operator_result():
    victim_wallet = "0x" + "e1" * 20
    front, victim, back = "0x" + "f1" * 32, "0x" + "f2" * 32, "0x" + "f3" * 32
    rows = [
        _row(1, front, EOA, BOT, [POOL_A]),
        _row(2, victim, victim_wallet, "0x" + "77" * 20, [POOL_A]),
        _row(3, back, EOA, BOT, [POOL_A]),
    ]
    receipts = {
        front: {
            "logs": [
                _swap_log(POOL_A, 10**18, -3000 * 10**6, 0),
                transfer(WETH, BOT, POOL_A, 10**18, 1),
                transfer(USDC, POOL_A, BOT, 3000 * 10**6, 2),
            ]
        },
        victim: {"logs": [_swap_log(POOL_A, 5 * 10**18, -14000 * 10**6, 0, victim_wallet)]},
        back: {
            "logs": [
                _swap_log(POOL_A, -3100 * 10**6, 10**18, 0),
                transfer(USDC, BOT, POOL_A, 3000 * 10**6, 1),
                transfer(WETH, POOL_A, BOT, 102 * 10**16, 2),
            ]
        },
    }
    assert swap_direction(event_topic(SIG_V3_SWAP), receipts[front]["logs"][0]["data"]) == 1
    bundles = find_sandwiches(rows, receipts)
    assert len(bundles) == 1
    bundle = bundles[0]
    assert bundle["front"] == front and bundle["back"] == back and bundle["victims"] == [victim]
    assert bundle["operator"] == BOT and bundle["operator_kind"] == "contract"
    result = operator_result(bundle, receipts, WETH, None, EOA)
    assert result["profit_token"] == WETH and result["profit_raw"] == str(2 * 10**16)
    # the same wallet trading in the same direction twice is not a sandwich
    receipts[back]["logs"][0] = _swap_log(POOL_A, 10**18, -3000 * 10**6, 0)
    assert find_sandwiches(rows, receipts) == []


async def test_sandwich_rows_get_mev_types_and_bundle_net(tmp_path):
    spec = load_chain("ethereum")
    victim_wallet = "0x" + "e1" * 20
    txs, receipts = [], []
    specs = [
        (
            EOA,
            BOT,
            _swap_log(POOL_A, 10**18, -3000 * 10**6, 0),
            [transfer(WETH, BOT, POOL_A, 10**18, 1), transfer(USDC, POOL_A, BOT, 3000 * 10**6, 2)],
        ),
        (
            victim_wallet,
            "0x" + "77" * 20,
            _swap_log(POOL_A, 5 * 10**18, -14000 * 10**6, 0, victim_wallet),
            [],
        ),
        (
            EOA,
            BOT,
            _swap_log(POOL_A, -3100 * 10**6, 10**18, 0),
            [transfer(USDC, BOT, POOL_A, 3000 * 10**6, 1), transfer(WETH, POOL_A, BOT, 102 * 10**16, 2)],
        ),
    ]
    for i, (frm, to, swap, extra) in enumerate(specs):
        tx, receipt = make_tx([swap, *extra], to=to)
        tx["from"] = frm
        tx["hash"] = receipt["transactionHash"] = "0x" + f"{i + 1:02x}" * 32
        receipt.pop("l1Fee")
        for obj in (tx, receipt):
            obj["blockHash"] = "0x" + "a1" * 32
            obj["transactionIndex"] = hex(i)
        txs.append(tx)
        receipts.append(receipt)
    block = {
        "number": "0x10",
        "hash": "0x" + "a1" * 32,
        "timestamp": "0x10",
        "baseFeePerGas": hex(1_000_000),
        "miner": "0x" + "c0" * 20,
        "transactions": txs,
    }
    rows = await classify_block(spec, block, receipts, RESOLVER, trace_mode="none")
    assert [r["mev_type"] for r in rows] == ["sandwich_front", "sandwich_victim", "sandwich_back"]
    back = rows[2]["sandwich"]
    assert back["profit_token"] == WETH and back["profit_raw"] == str(2 * 10**16)
    assert back["bundle_fee_wei"] == 2 * 150_000 * 1_500_000


def test_mev_type_precedence():
    assert (
        mev_type_of({"assessment": "trace_supported", "arb_class": "atomic_cycle", "trigger_type": "backrun"})
        == "atomic_backrun"
    )
    assert (
        mev_type_of(
            {"assessment": "trace_supported", "arb_class": "embedded_cycle", "trigger_type": "backrun"}
        )
        == "embedded_cycle"
    )
    assert (
        mev_type_of({"assessment": "not_arb", "arb_class": "single_swap", "sandwich": {"role": "victim"}})
        == "sandwich_victim"
    )
    assert mev_type_of({"assessment": "trace_supported_loss", "arb_class": "loss_cycle"}) == "loss_cycle"
    assert mev_type_of({"assessment": "event_candidate", "arb_class": "atomic_cycle"}) == "candidate_cycle"
    assert mev_type_of({"assessment": "not_arb", "arb_class": "probe"}) == "none"


async def test_pricing_native_stable_and_leg_implied(tmp_path):
    book = PriceBook(tmp_path, offline=True)
    book.set("ETHUSDT", 16, 4000.0)
    other = "0x" + "99" * 20
    legs = [
        {
            "venue": POOL_A,
            "token_in": WETH,
            "amount_in": str(10**18),
            "token_out": other,
            "amount_out": str(200 * 10**18),
        },
        {
            "venue": POOL_B,
            "token_in": other,
            "amount_in": str(200 * 10**18),
            "token_out": WETH,
            "amount_out": str(10**18 + 10**16),
        },
    ]
    base = {
        "block_number": 16,
        "assessment": "trace_supported",
        "fees": {"total_fee_wei": 10**15},
        "builder_tip_wei": "0",
        "trace": {"status": "ok", "legs": legs},
        "operator_scope": None,
    }
    rows = [
        {**base, "profit_token": WETH, "gross_profit_raw": str(10**16)},
        {**base, "profit_token": USDC, "gross_profit_raw": str(5 * 10**6)},
        {**base, "profit_token": other, "gross_profit_raw": str(2 * 10**18)},
        {
            **base,
            "profit_token": "0x" + "55" * 20,
            "gross_profit_raw": "7",
            "trace": {"status": "ok", "legs": []},
        },
    ]
    await value_rows(BASE, rows, {16: 16}, book)
    assert (
        rows[0]["price_source"] == "native"
        and abs(rows[0]["gross_usd"] - 40.0) < 1e-9
        and abs(rows[0]["net_usd"] - 36.0) < 1e-9
    )
    assert rows[1]["price_source"] == "stable" and abs(rows[1]["gross_usd"] - 5.0) < 1e-9
    assert (
        rows[2]["price_source"].startswith("leg_implied:native") and abs(rows[2]["gross_usd"] - 40.0) < 1e-6
    )
    assert (
        rows[3]["price_source"] == "unpriced" and rows[3]["gross_usd"] is None and rows[3]["net_usd"] is None
    )
    assert implied_amount(other, 10**18, [], {WETH}) is None


def test_daily_aggregation_groups_by_utc_day_and_extrapolates():
    def row(block, kind, gross, net, fee=0.5):
        return {
            "block_number": block,
            "assessment": "trace_supported",
            "mev_type": kind,
            "trigger_type": "backrun",
            "gross_usd": gross,
            "net_usd": net,
            "fee_usd": fee,
            "builder_tip_usd": 0.0,
            "builder_tip_wei": "0",
            "fees": {"total_fee_wei": 10**12},
            "tx_to": BOT,
            "profit_symbol": "WETH",
        }

    day1 = 1_700_000_000 // 86400 * 86400
    report = {
        "created_at": "2026-10-08T00:00:00",
        "day_mode": "sample:3",
        "rows": [
            row(1, "atomic_backrun", 10.0, 9.0),
            row(2, "atomic_mid_block", 5.0, 4.0),
            {**row(3, "none", None, None), "assessment": "not_arb"},
        ],
        "blocks": [
            {"number": 1, "timestamp": day1},
            {"number": 2, "timestamp": day1 + 10},
            {"number": 3, "timestamp": day1 + 86400},
        ],
    }
    newer = {
        "created_at": "2026-10-09T00:00:00",
        "day_mode": "sample:1",
        "rows": [row(2, "atomic_mid_block", 6.0, 5.0)],
        "blocks": [{"number": 2, "timestamp": day1 + 10}],
    }
    result = aggregate_daily(BASE, [report, newer])
    first, second = result["days"]
    assert first["blocks_covered"] == 2 and first["cycles"] == 2
    assert first["observed"]["total_gross_usd"] == 16.0 and first["observed"]["total_net_usd"] == 14.0
    assert first["coverage"] == 2 / BASE.blocks_per_day and first["extrapolated_per_day"][
        "total_net_usd"
    ] == round(14.0 * BASE.blocks_per_day / 2, 2)
    assert first["top_operators"][0]["address"] == BOT and first["top_operators"][0]["cycles"] == 2
    assert second["cycles"] == 0 and second["blocks_covered"] == 1
    assert sample_blocks(100, 199, 10) == [100, 110, 120, 130, 140, 150, 160, 170, 180, 190]
    assert sample_blocks(100, 104, 50) == [100, 101, 102, 103, 104]


def test_new_swap_decoders_curve_balancer_v3_infinity():
    curve = {
        "address": POOL_A,
        "logIndex": "0x1",
        "topics": [event_topic(SIG_CURVE_STABLE_EXCHANGE), t(BOT)],
        "data": "0x" + encode(["int128", "uint256", "int128", "uint256"], [0, 1000, 1, 999]).hex(),
    }
    leg = decode_swap_log(curve)
    assert (
        leg.kind == "curve" and leg.amount_in == 1000 and leg.amount_out == 999 and leg.sender == BOT.lower()
    )
    crypto = {
        "address": POOL_A,
        "logIndex": "0x1",
        "topics": [event_topic(SIG_CURVE_CRYPTO_EXCHANGE), t(BOT)],
        "data": "0x" + encode(["uint256"] * 4, [1, 5, 0, 7]).hex(),
    }
    crypto_leg = decode_swap_log(crypto)
    assert crypto_leg.amount_in == 5 and crypto_leg.amount_out == 7  # sold coin 1, bought coin 0
    vault = "0x" + "ba" * 20
    b3 = {
        "address": vault,
        "logIndex": "0x2",
        "topics": [event_topic(SIG_BALANCER_V3_SWAP), t(POOL_B), t(WETH), t(USDC)],
        "data": "0x" + encode(["uint256"] * 4, [10**18, 3000 * 10**6, 0, 0]).hex(),
    }
    leg = decode_swap_log(b3)
    assert (
        leg.kind == "vault"
        and leg.token_in == WETH
        and leg.token_out == USDC
        and leg.amount_out == 3000 * 10**6
    )
    assert leg.key == f"{vault}:{t(POOL_B)}"
    manager = "0x" + "a0" * 20
    pool_id = "0x" + "ab" * 32
    inf = {
        "address": manager,
        "logIndex": "0x3",
        "topics": [event_topic(SIG_INFINITY_CL_SWAP), pool_id, t(BOT)],
        "data": "0x"
        + encode(
            ["int128", "int128", "uint160", "uint128", "int24", "uint24", "uint16"],
            [-(10**18), 3000 * 10**6, 2**96, 10**20, 0, 500, 0],
        ).hex(),
    }
    leg = decode_swap_log(inf)
    assert (
        leg.kind == "clmm_v4_singleton"
        and leg.pool_id == pool_id
        and leg.amount0 == 10**18
        and leg.amount1 == -3000 * 10**6
    )


def test_handlabel_compare_scores_expected_outcomes(tmp_path):
    from arbkit.verify.handlabels import compare

    labels = {
        "rows": [
            {
                "tx_hash": "0x" + "01" * 32,
                "block": 16,
                "expected": "atomic_cycle",
                "labelled_proceeds": "0.01 WETH",
            },
            {"tx_hash": "0x" + "02" * 32, "block": 16, "expected": "atomic_cycle"},
        ]
    }
    report = {
        "rows": [
            {
                "tx_hash": "0x" + "01" * 32,
                "assessment": "trace_supported",
                "arb_class": "atomic_cycle",
                "mev_type": "atomic_backrun",
                "trigger_type": "backrun",
                "n_legs": 2,
                "profit_symbol": "WETH",
                "gross_profit": "0.010000000000000000",
                "profit_decimals": 18,
                "gross_profit_raw": str(10**16),
                "fees": {"total_fee_eth": "0.000001"},
                "trace": {"verdict": "cycle", "status": "ok"},
                "explorer_url": "https://basescan.org/tx/0x01",
            }
        ]
    }
    result = compare(BASE, labels, report)
    assert result["ok"] == 1 and result["found"] == 1 and result["proceeds_exact"] == 1
    assert result["results"][1]["found"] is False
    assert json.dumps(result)


def test_maverick_and_woofi_decoders():
    from mevlab.decode.signatures import SIG_MAVERICK_V2_SWAP, SIG_WOOFI_SWAP

    mav = {
        "address": POOL_A,
        "logIndex": "0x1",
        "topics": [event_topic(SIG_MAVERICK_V2_SWAP)],
        "data": "0x"
        + encode(
            ["address", "address", "(uint256,bool,bool,int32)", "uint256", "uint256"],
            [BOT, BOT, (10**18, True, False, 0), 10**18, 3000 * 10**6],
        ).hex(),
    }
    leg = decode_swap_log(mav)
    assert leg.kind == "maverick_v2" and leg.amount_in == 10**18 and leg.amount_out == 3000 * 10**6
    woo = {
        "address": POOL_B,
        "logIndex": "0x2",
        "topics": [event_topic(SIG_WOOFI_SWAP), t(WETH), t(USDC), t(BOT)],
        "data": "0x"
        + encode(
            ["uint256", "uint256", "address", "address", "uint256", "uint256"],
            [10**18, 3000 * 10**6, BOT, BOT, 1, 2],
        ).hex(),
    }
    leg = decode_swap_log(woo)
    assert (
        leg.kind == "pmm_woofi"
        and leg.token_in == WETH
        and leg.token_out == USDC
        and leg.amount_out == 3000 * 10**6
    )


def test_daily_extrapolates_only_uniform_day_samples():
    row = {
        "block_number": 1,
        "assessment": "trace_supported",
        "mev_type": "atomic_backrun",
        "trigger_type": "backrun",
        "gross_usd": 10.0,
        "net_usd": 9.0,
        "fee_usd": 0.5,
        "builder_tip_usd": 0.0,
        "builder_tip_wei": "0",
        "kickback_wei": "0",
        "fees": {"total_fee_wei": 10**12},
        "tx_to": BOT,
        "profit_symbol": "WETH",
    }
    day1 = 1_700_000_000 // 86400 * 86400
    selected = {
        "created_at": "2026-10-08T00:00:00",
        "rows": [row],
        "blocks": [{"number": 1, "timestamp": day1}],
    }
    assert aggregate_daily(BASE, [selected])["days"][0]["extrapolated_per_day"] is None
    sampled = {**selected, "day_mode": "sample:200"}
    assert aggregate_daily(BASE, [sampled])["days"][0]["extrapolated_per_day"]["total_net_usd"] > 9.0


def test_ui_store_serves_blocks_and_transactions(tmp_path):
    import json as _json

    from arbkit.site.store import Store

    reports = tmp_path / "base"
    reports.mkdir()
    row = {
        "block_number": 16,
        "tx_index": 1,
        "tx_hash": "0x" + "01" * 32,
        "tx_from": EOA,
        "tx_to": BOT,
        "assessment": "trace_supported",
        "arb_class": "atomic_cycle",
        "mev_type": "atomic_backrun",
        "trigger_type": "backrun",
        "n_legs": 2,
        "profit_symbol": "WETH",
        "gross_profit": "0.01",
        "gross_usd": 40.0,
        "net_usd": 36.0,
        "fee_usd": 4.0,
        "builder_tip_wei": "0",
        "kickback_wei": "0",
        "fees": {"total_fee_wei": 10**15, "total_fee_eth": "0.001", "priority_fee_per_gas_gwei": "0.1"},
        "trace": {"status": "ok", "venues": [POOL_A, POOL_B]},
        "explorer_url": "https://basescan.org/tx/0x01",
        "venues": [POOL_A, POOL_B],
        "price_source": "native",
    }
    report = {
        "chain": "base",
        "created_at": "2026-10-08T00:00:00",
        "rows": [row],
        "blocks": [{"number": 16, "timestamp": 1_700_000_000}],
        "requested_blocks": [16],
        "failures": [],
        "summary": {},
    }
    (reports / "base_16-16_x.json").write_text(_json.dumps(report))
    store = Store(tmp_path)
    chain = store.get("base")
    summary = chain.summary()
    assert summary["cycles"] == 1 and summary["gross_usd"] == 40.0 and summary["blocks_covered"] == 1
    assert chain.block(16)["rows"][0]["mev_type"] == "atomic_backrun"
    assert chain.transaction("0x" + "01" * 32)["net_usd"] == 36.0
    assert chain.transactions(limit=10)[0]["tx_hash"] == "0x" + "01" * 32
    assert chain.operators()[0]["address"] == BOT


def test_pass_through_helper_is_not_a_venue_and_inventory_sale_is_not_a_cycle():
    """Ethereum 0x0c1e551a…: operator -> helper -> pool; the helper pays USDT from its own inventory, receives the
    pool's WETH and forwards it to the operator. Not a cycle; the trace must not close a 7.45 WETH gain."""
    from mevlab.trace.verdict import trace_verdict
    from tests.mevlab.test_trace import call
    from tests.mevlab.test_trace import transfer as trace_transfer

    HELPER = "0x" + "77" * 20
    POOL = POOL_A
    trace = call(
        EOA,
        BOT,
        calls=[
            call(
                BOT,
                HELPER,
                calls=[
                    call(
                        HELPER,
                        POOL,
                        calls=[
                            call(POOL, WETH, trace_transfer(HELPER, 7454070625485171127)),
                            call(HELPER, USDC, trace_transfer(POOL, 18187320655)),
                        ],
                    ),
                    call(HELPER, WETH, trace_transfer(BOT, 7454070625485171116)),
                ],
            ),
        ],
    )
    logs = [
        transfer(WETH, POOL, HELPER, 7454070625485171127, 45),
        transfer(USDC, HELPER, POOL, 18187320655, 46),
        transfer(WETH, HELPER, BOT, 7454070625485171116, 48),
    ]
    tv = trace_verdict(trace, WETH, EOA, BOT, logs=logs)
    assert tv.verdict != "cycle"
    assert any(n.startswith("pass_through_helpers:") for n in tv.notes)
    assert HELPER not in tv.venues and POOL in tv.venues
    # the helper is part of the operator: the group paid USDC and received WETH, an open one-way sale
    assert tv.group_asset_deltas.get(USDC, 0) < 0 and tv.group_asset_deltas.get(WETH, 0) > 0


def test_positive_trace_cycle_carries_its_returning_route_and_continuity():
    from mevlab.trace.route import continuity_gap
    from mevlab.trace.verdict import trace_verdict
    from tests.mevlab.test_trace import POOL_A as TRACE_POOL_A
    from tests.mevlab.test_trace import POOL_B as TRACE_POOL_B
    from tests.mevlab.test_trace import two_leg_cycle_trace

    tv = trace_verdict(two_leg_cycle_trace(), WETH)
    assert tv.verdict == "cycle" and len(tv.cycle_path) == 2
    assert [leg["venue"] for leg in tv.cycle_path] == [TRACE_POOL_A, TRACE_POOL_B]
    assert not any(n.startswith("amount_discontinuity") for n in tv.notes)
    assert (
        continuity_gap([{"amount_out": "100", "amount_in": "1"}, {"amount_out": "5", "amount_in": "120"}])
        == 0.2
    )
    assert (
        continuity_gap([{"amount_out": "100", "amount_in": "1"}, {"amount_out": "5", "amount_in": "100"}])
        is None
    )


async def test_base_flashblock_estimate_yields_top_of_flashblock_positions():
    spec = load_chain("base")
    txs, receipts = [], []
    # two estimated flashblocks: fees 9,8 then a jump to 20,19; the cycle at index 2 opens the second slot
    for i, (fee, is_cycle) in enumerate([(9, False), (8, False), (20, True), (19, False)]):
        logs = []
        if is_cycle:
            logs = [
                transfer(WETH, BOT, POOL_A, 10**18, 0),
                v3_swap(POOL_A, 10**18, -3000 * 10**6, 1),
                transfer(USDC, POOL_A, BOT, 3000 * 10**6, 2),
                transfer(USDC, BOT, POOL_B, 3000 * 10**6, 3),
                solidly_swap(POOL_B, 0, 3000 * 10**6, 101 * 10**16, 0, 4),
                transfer(WETH, POOL_B, BOT, 101 * 10**16, 5),
            ]
        tx, receipt = make_tx(logs)
        tx["hash"] = receipt["transactionHash"] = "0x" + f"{i + 1:02x}" * 32
        receipt["effectiveGasPrice"] = hex(1_000_000 + fee * 10**6)
        for obj in (tx, receipt):
            obj["blockHash"] = "0x" + "a1" * 32
            obj["transactionIndex"] = hex(i)
        txs.append(tx)
        receipts.append(receipt)
    block = {
        "number": "0x10",
        "hash": "0x" + "a1" * 32,
        "timestamp": "0x10",
        "baseFeePerGas": hex(1_000_000),
        "miner": "0x" + "42" * 20,
        "transactions": txs,
    }
    rows = await classify_block(spec, block, receipts, RESOLVER, trace_mode="none")
    assert [r["slot_index_est"] for r in rows] == [1, 1, 2, 2]
    assert rows[2]["trigger_type"] == "top_of_flashblock" and rows[2]["tof_est"] is True
    assert rows[2]["mev_type"] == "candidate_cycle"


def test_singleton_route_is_promoted_only_when_the_group_funded_it():
    """Flash accounting settles only the net: a singleton route may show no explicit input (Robinhood 0x72d00197…
    takes 0.372 WETH net from the PoolManager). The receipt-backed gain is accepted when it does not exceed the gain
    the Swap events establish; a larger receipt gain (someone else funded the output) stays under review."""
    from arbkit.scan.trace import apply_trace
    from tests.mevlab.test_trace import call
    from tests.mevlab.test_trace import transfer as trace_transfer

    spec = load_chain("base")
    PM = "0x" + "aa" * 20

    def row_for():
        return {
            "tx_hash": "0x" + "ab" * 32,
            "is_arb": True,
            "arb_class": "atomic_cycle",
            "assessment": "event_candidate",
            "n_legs": 2,
            "venues": [PM + ":0x01", PM + ":0x02"],
            "event_label": {
                "venues": [PM + ":0x01", PM + ":0x02"],
                "gross_profit_raw": 1000,
                "profit_token": WETH,
            },
            "reasons": [],
            "gross_unreliable": False,
            "fees": {"is_system": False},
            "profit_token": WETH,
            "gross_profit_raw": "1000",
            "gross_source": "events",
        }

    # receipts show a 1 ETH net take while the Swap events only establish 1000 wei: not the operator's own result
    trace = call(EOA, BOT, calls=[call(PM, BOT, value=10**18)])
    tx, receipt = {"from": EOA, "to": BOT, "hash": "0x" + "ab" * 32}, {"status": "0x1", "logs": []}
    row = row_for()
    apply_trace(spec, row, trace, tx, receipt)
    assert row["assessment"] == "needs_review"
    # net settlement of a self-funded route: events say 1 ETH, receipts show the same 1 ETH net take
    row = row_for()
    row["gross_profit_raw"] = row["event_label"]["gross_profit_raw"] = str(10**18)
    row["event_label"]["profit_token"] = WETH
    apply_trace(spec, row, trace, tx, receipt)
    assert row["assessment"] == "trace_supported" and row["gross_profit_raw"] == str(10**18)
    # the operator explicitly settles 0.99 and takes 1.0: 0.01 result, consistent with events saying 0.01
    trace = call(
        EOA, BOT, calls=[call(BOT, WETH, trace_transfer(PM, 99 * 10**16)), call(PM, BOT, value=10**18)]
    )
    receipt = {"status": "0x1", "logs": [transfer(WETH, BOT, PM, 99 * 10**16, 1)]}
    row = row_for()
    row["gross_profit_raw"] = row["event_label"]["gross_profit_raw"] = str(10**16)
    row["event_label"]["profit_token"] = WETH
    apply_trace(spec, row, trace, tx, receipt)
    assert row["assessment"] == "trace_supported" and row["gross_profit_raw"] == str(10**16)


def test_builder_tag_decoding_and_audit_reconciliation(tmp_path):
    from arbkit.scan.bundle import decode_extra_data
    from arbkit.verify.audit import cash_check, expected_fee
    from tests.mevlab.test_trace import call
    from tests.mevlab.test_trace import transfer as trace_transfer

    assert decode_extra_data("0x" + b"Titan (titanbuilder.xyz)".hex()) == "Titan (titanbuilder.xyz)"
    assert decode_extra_data("0x" + bytes(32).hex()) is None  # OP Stack / BSC binary extraData
    assert decode_extra_data("0x") is None and decode_extra_data(None) is None
    # independent reconciliation of a two-leg cycle with a payout: receipts + native calls rebuild the gross
    trace = call(
        EOA,
        BOT,
        calls=[
            call(BOT, WETH, trace_transfer(POOL_A, 10**18)),
            call(BOT, POOL_A, calls=[call(POOL_A, USDC, trace_transfer(BOT, 3000 * 10**6))]),
            call(BOT, USDC, trace_transfer(POOL_B, 3000 * 10**6)),
            call(BOT, POOL_B, calls=[call(POOL_B, WETH, trace_transfer(BOT, 101 * 10**16))]),
            call(BOT, "0x" + "c0" * 20, value=3 * 10**15),
        ],
    )
    receipt = {
        "logs": [
            transfer(WETH, BOT, POOL_A, 10**18, 0),
            transfer(USDC, POOL_A, BOT, 3000 * 10**6, 1),
            transfer(USDC, BOT, POOL_B, 3000 * 10**6, 2),
            transfer(WETH, POOL_B, BOT, 101 * 10**16, 3),
        ],
        "gasUsed": hex(150_000),
        "effectiveGasPrice": hex(1_500_000),
    }
    row = {
        "profit_token": WETH,
        "gross_profit_raw": str(10**16),
        "trace": {"group": [BOT, EOA], "payouts_by_token": {WETH: {"0x" + "c0" * 20: str(3 * 10**15)}}},
    }
    check = cash_check(row, receipt, trace, WETH)
    assert check["gross_matches"] and check["gross_from_receipts"] == 10**16 and not check["open_losses"]
    total, priority = expected_fee(load_chain("bsc"), {"type": "0x2"}, receipt, {"baseFeePerGas": "0x0"})
    assert total == priority == 150_000 * 1_500_000
    total, _ = expected_fee(
        load_chain("base"), {"type": "0x2"}, {**receipt, "l1Fee": hex(7)}, {"baseFeePerGas": hex(1_000_000)}
    )
    assert total == 150_000 * 1_500_000 + 7


async def test_sandwich_bundle_is_valued_over_every_token_not_the_largest_raw_integer(tmp_path):
    """Ethereum 26150003:44: +0.000625 USDe dust (18 decimals) and +1.47 USDC (6 decimals); the raw maximum picked
    the unpriced dust and the bundle stayed unpriced. Every priced net is summed; the shown profit token is the
    largest priced gain; unpriced tokens are listed."""
    from decimal import Decimal

    book = PriceBook(tmp_path, offline=True)
    book.set("ETHUSDT", 16, 4000.0)
    usdc = next(t for t in BASE.tokens if t.symbol == "USDC").address
    dust = "0x" + "77" * 20

    def back(net_raw, profit_token, profit_raw):
        return {
            "block_number": 16,
            "assessment": "not_arb",
            "fees": {"total_fee_wei": 10**14},
            "builder_tip_wei": "0",
            "trace": {"status": "not_requested"},
            "operator_scope": None,
            "sandwich": {
                "role": "back",
                "net_raw": net_raw,
                "profit_token": profit_token,
                "profit_raw": str(profit_raw),
                "profit_symbol": "DUST",
                "profit_decimals": 18,
                "bundle_fee_wei": 10**15,
                "bundle_builder_tip_wei": "0",
            },
        }

    rows = [
        back({dust: str(10**15), usdc: "1472617"}, dust, 10**15),
        back({dust: str(10**15)}, dust, 10**15),
        back({BASE.weth: str(10**16), usdc: "-5000000"}, BASE.weth, 10**16),
    ]
    await value_rows(BASE, rows, {16: 16}, book)
    fixed = rows[0]["sandwich"]
    assert (
        fixed["profit_token"] == usdc
        and fixed["profit_symbol"] == "USDC"
        and Decimal(fixed["profit"]) == Decimal("1.472617")
    )
    assert abs(fixed["profit_usd"] - 1.472617) < 1e-9 and fixed["price_source"] == "stable"
    assert fixed["unpriced_tokens"] == {dust: str(10**15)}
    assert abs(fixed["net_usd"] - (1.472617 - 4.0)) < 1e-9  # 0.001 ETH of bundle fees at $4000
    only_dust = rows[1]["sandwich"]
    assert (
        only_dust["price_source"] == "unpriced"
        and only_dust["profit_usd"] is None
        and only_dust["net_usd"] is None
    )
    mixed = rows[2]["sandwich"]
    assert (
        mixed["profit_token"] == BASE.weth
        and abs(mixed["profit_usd"] - 35.0) < 1e-9
        and abs(mixed["net_usd"] - 31.0) < 1e-9
    )
    assert mixed["price_source"] == "mixed:native+stable" and mixed["unpriced_tokens"] == {}


def test_sandwich_legs_must_be_swaps_and_the_bundle_a_round_trip():
    from arbkit.scan.sandwich import round_trip_verdict

    victim_wallet = "0x" + "e1" * 20
    front, victim, back = "0x" + "f1" * 32, "0x" + "f2" * 32, "0x" + "f3" * 32
    rows = [
        _row(1, front, EOA, BOT, [POOL_A]),
        _row(2, victim, victim_wallet, "0x" + "77" * 20, [POOL_A]),
        _row(3, back, EOA, BOT, [POOL_A]),
    ]
    receipts = {
        front: {
            "logs": [
                _swap_log(POOL_A, 10**18, -3000 * 10**6, 0),
                transfer(WETH, BOT, POOL_A, 10**18, 1),
                transfer(USDC, POOL_A, BOT, 3000 * 10**6, 2),
            ]
        },
        victim: {"logs": [_swap_log(POOL_A, 5 * 10**18, -14000 * 10**6, 0, victim_wallet)]},
        back: {
            "logs": [
                _swap_log(POOL_A, -3100 * 10**6, 10**18, 0),
                transfer(USDC, BOT, POOL_A, 3000 * 10**6, 1),
                transfer(WETH, POOL_A, BOT, 102 * 10**16, 2),
            ]
        },
    }
    bundle = find_sandwiches(rows, receipts)[0]
    result = operator_result(bundle, receipts, WETH, None, EOA)
    assert result["moved_raw"] == {WETH: str(202 * 10**16)}  # USDC nets to zero and is not reported
    assert USDC not in result["net_raw"]
    assert round_trip_verdict(result) is None
    # add -> victim swap -> remove is JIT liquidity, not a sandwich; a counted cycle after the victim is a backrun
    for key, value in (("arb_class", "lp_action"), ("assessment", "trace_supported")):
        changed = [dict(r) for r in rows]
        changed[2][key] = value
        assert find_sandwiches(changed, receipts) == [], key
    # the victim may be a zap; only the legs are restricted
    zapped = [dict(r) for r in rows]
    zapped[1]["arb_class"] = "lp_action"
    assert len(find_sandwiches(zapped, receipts)) == 1
    # front buys 3000 USDC, back sells only 300 of them: the operator kept 90 % of what it bought
    receipts[back]["logs"] = [
        _swap_log(POOL_A, -300 * 10**6, 10**17, 0),
        transfer(USDC, BOT, POOL_A, 300 * 10**6, 1),
        transfer(WETH, POOL_A, BOT, 10**17, 2),
    ]
    kept = operator_result(find_sandwiches(rows, receipts)[0], receipts, WETH, None, EOA)
    verdict = round_trip_verdict(
        kept
    )  # both pool tokens moved 82 % one way: the first by address is reported
    assert verdict is not None and verdict.split(":")[1] in {USDC, WETH} and verdict.endswith(":0.82")
    # top-of-block inventory trade: USDT spent in the front and never recovered
    receipts[front]["logs"].append(transfer("0x" + "ab" * 20, BOT, "0x" + "cd" * 20, 500 * 10**6, 3))
    receipts[back]["logs"] = [
        _swap_log(POOL_A, -3100 * 10**6, 10**18, 0),
        transfer(USDC, BOT, POOL_A, 3000 * 10**6, 1),
        transfer(WETH, POOL_A, BOT, 102 * 10**16, 2),
    ]
    spent = operator_result(find_sandwiches(rows, receipts)[0], receipts, WETH, None, EOA)
    assert round_trip_verdict(spent) == "inventory_change:0x" + "ab" * 20 + ":1.00"


async def test_engine_rejects_a_bracketing_position_change_and_records_why(tmp_path):
    spec = load_chain("ethereum")
    victim_wallet = "0x" + "e1" * 20
    txs, receipts = [], []
    specs = [
        (
            EOA,
            BOT,
            _swap_log(POOL_A, 10**18, -3000 * 10**6, 0),
            [transfer(WETH, BOT, POOL_A, 10**18, 1), transfer(USDC, POOL_A, BOT, 3000 * 10**6, 2)],
        ),
        (
            victim_wallet,
            "0x" + "77" * 20,
            _swap_log(POOL_A, 5 * 10**18, -14000 * 10**6, 0, victim_wallet),
            [],
        ),
        (
            EOA,
            BOT,
            _swap_log(POOL_A, -300 * 10**6, 10**17, 0),
            [transfer(USDC, BOT, POOL_A, 300 * 10**6, 1), transfer(WETH, POOL_A, BOT, 10**17, 2)],
        ),
    ]
    for i, (frm, to, swap, extra) in enumerate(specs):
        tx, receipt = make_tx([swap, *extra], to=to)
        tx["from"] = frm
        tx["hash"] = receipt["transactionHash"] = "0x" + f"{i + 1:02x}" * 32
        receipt.pop("l1Fee")
        for obj in (tx, receipt):
            obj["blockHash"] = "0x" + "a1" * 32
            obj["transactionIndex"] = hex(i)
        txs.append(tx)
        receipts.append(receipt)
    block = {
        "number": "0x10",
        "hash": "0x" + "a1" * 32,
        "timestamp": "0x10",
        "baseFeePerGas": hex(1_000_000),
        "miner": "0x" + "c0" * 20,
        "transactions": txs,
    }
    rows = await classify_block(spec, block, receipts, RESOLVER, trace_mode="none")
    assert not any(str(r.get("mev_type", "")).startswith("sandwich") for r in rows)
    rejected = rows[2]["sandwich_rejected"]
    assert (
        rejected["reason"].split(":")[1] in {USDC, WETH}
        and rejected["reason"].endswith(":0.82")
        and rejected["front"] == txs[0]["hash"]
    )


def test_sandwich_operator_result_finds_the_holding_contract_behind_a_forwarder():
    """Base 52315111:106: the called contract forwards; the tokens move between the pool and a holding contract."""
    from arbkit.scan.sandwich import round_trip_verdict

    victim_wallet, holder, forwarder = "0x" + "e1" * 20, "0x" + "a5" * 20, "0x" + "f0" * 20
    front, victim, back = "0x" + "f1" * 32, "0x" + "f2" * 32, "0x" + "f3" * 32
    rows = [
        _row(1, front, EOA, forwarder, [POOL_A]),
        _row(2, victim, victim_wallet, "0x" + "77" * 20, [POOL_A]),
        _row(3, back, EOA, forwarder, [POOL_A]),
    ]
    receipts = {
        front: {
            "logs": [
                _swap_log(POOL_A, 10**18, -3000 * 10**6, 0),
                transfer(WETH, holder, POOL_A, 10**18, 1),
                transfer(USDC, POOL_A, holder, 3000 * 10**6, 2),
            ]
        },
        victim: {"logs": [_swap_log(POOL_A, 5 * 10**18, -14000 * 10**6, 0, victim_wallet)]},
        back: {
            "logs": [
                _swap_log(POOL_A, -3100 * 10**6, 10**18, 0),
                transfer(USDC, holder, POOL_A, 3000 * 10**6, 1),
                transfer(WETH, POOL_A, holder, 102 * 10**16, 2),
            ]
        },
    }
    bundle = find_sandwiches(rows, receipts)[0]
    assert bundle["operator"] == forwarder
    result = operator_result(bundle, receipts, WETH, None, EOA, {POOL_A})
    assert result["holder_inferred"] == holder and result["result_status"] == "measured"
    assert (
        result["profit_token"] == WETH
        and result["profit_raw"] == str(2 * 10**16)
        and round_trip_verdict(result) is None
    )
    # without a single consistent holder nothing is inferred and the result is marked as unmeasured
    receipts[back]["logs"][1:] = [
        transfer(USDC, "0x" + "b6" * 20, POOL_A, 3000 * 10**6, 1),
        transfer(WETH, POOL_A, "0x" + "b6" * 20, 102 * 10**16, 2),
    ]
    unmeasured = operator_result(bundle, receipts, WETH, None, EOA, {POOL_A})
    assert (
        unmeasured["holder_inferred"] is None
        and unmeasured["result_status"] == "no_operator_flows"
        and unmeasured["net_raw"] == {}
    )


def test_two_wallets_through_one_router_are_not_a_sandwich_operator():
    """KyberSwap / Uniswap V2 Router brackets: unrelated users of a public contract around a third user."""
    victim_wallet = "0x" + "e1" * 20
    front, victim, back = "0x" + "f1" * 32, "0x" + "f2" * 32, "0x" + "f3" * 32
    receipts = {
        front: {
            "logs": [
                _swap_log(POOL_A, 10**18, -3000 * 10**6, 0),
                transfer(WETH, BOT, POOL_A, 10**18, 1),
                transfer(USDC, POOL_A, BOT, 3000 * 10**6, 2),
            ]
        },
        victim: {"logs": [_swap_log(POOL_A, 5 * 10**18, -14000 * 10**6, 0, victim_wallet)]},
        back: {
            "logs": [
                _swap_log(POOL_A, -3100 * 10**6, 10**18, 0),
                transfer(USDC, BOT, POOL_A, 3000 * 10**6, 1),
                transfer(WETH, POOL_A, BOT, 102 * 10**16, 2),
            ]
        },
    }
    rows = [
        _row(1, front, EOA, BOT, [POOL_A]),
        _row(2, victim, victim_wallet, "0x" + "77" * 20, [POOL_A]),
        _row(3, back, "0x" + "a9" * 20, BOT, [POOL_A]),
    ]
    assert find_sandwiches(rows, receipts) == []
    rows[2]["tx_from"] = EOA
    bundle = find_sandwiches(rows, receipts)[0]
    assert bundle["operator"] == BOT and bundle["operator_kind"] == "contract"
    # the same wallet through two different contracts is still one operator: the wallet
    rows[2]["tx_to"] = "0x" + "b7" * 20
    bundle = find_sandwiches(rows, receipts)[0]
    assert bundle["operator"] == EOA and bundle["operator_kind"] == "wallet"


async def test_base_has_no_flashblock_slots_after_200ms_blocks():
    """From block 53,556,127 (2026-11-05) Base seals a block every 200 ms: one auction per block, no slots inside."""
    spec = load_chain("base")
    assert spec.block_time_ms == 200 and spec.flashblocks_until_block == 53_556_127
    txs, receipts = [], []
    for i, fee in enumerate([9, 8, 20, 19]):
        tx, receipt = make_tx([])
        tx["hash"] = receipt["transactionHash"] = "0x" + f"{i + 1:02x}" * 32
        receipt["effectiveGasPrice"] = hex(1_000_000 + fee * 10**6)
        for obj in (tx, receipt):
            obj["blockHash"] = "0x" + "a1" * 32
            obj["transactionIndex"] = hex(i)
            obj["blockNumber"] = hex(53_556_127)
        txs.append(tx)
        receipts.append(receipt)
    block = {
        "number": hex(53_556_127),
        "hash": "0x" + "a1" * 32,
        "timestamp": "0x10",
        "baseFeePerGas": hex(1_000_000),
        "miner": "0x" + "42" * 20,
        "transactions": txs,
    }
    rows = await classify_block(spec, block, receipts, RESOLVER, trace_mode="none")
    assert all(r.get("slot_index_est") is None for r in rows)
    assert all(r.get("trigger_type") != "top_of_flashblock" for r in rows)
