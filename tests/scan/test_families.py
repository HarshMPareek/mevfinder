"""Other MEV families: liquidations, launch snipes, JIT liquidity, CEX-DEX candidates, and the builder economics
module. Receipts, block order and prices only; nothing here changes a cycle verdict."""

import asyncio

from eth_abi import encode

from arbkit.chain.spec import load_chain
from arbkit.scan.families import LIQUIDATION_EVENTS, annotate_block, decode_liquidation, family_of
from arbkit.value.builders import measure_builders
from arbkit.value.pricing import PriceBook, cex_dex_candidate, value_rows
from mevlab.decode.signatures import SIG_V3_BURN, SIG_V3_MINT, event_topic
from tests.mevlab.test_receipt import BOT, POOL_A, USDC, WETH, t, transfer, v3_swap

SPEC = load_chain("base")
AAVE = "0x" + "aa" * 20
BORROWER = "0x" + "b0" * 20
LIQUIDATOR = "0x" + "11" * 20
USER = "0x" + "77" * 20
LP = "0x" + "99" * 20


def aave_liquidation(li):
    topic = next(k for k, v in LIQUIDATION_EVENTS.items() if v == "aave_v3")
    data = encode(["uint256", "uint256", "address", "bool"], [1_000 * 10**6, 5 * 10**17, LIQUIDATOR, False])
    return {
        "address": AAVE,
        "logIndex": hex(li),
        "topics": [topic, t(WETH), t(USDC), t(BORROWER)],
        "data": "0x" + data.hex(),
    }


def test_aave_v3_liquidation_is_decoded():
    liq = decode_liquidation(aave_liquidation(0))
    assert liq["protocol"] == "aave_v3" and liq["liquidator"] == LIQUIDATOR and liq["borrower"] == BORROWER
    assert liq["debt_asset"] == USDC and liq["debt_repaid_raw"] == str(1_000 * 10**6)
    assert liq["collateral_asset"] == WETH and liq["collateral_seized_raw"] == str(5 * 10**17)


def _row(idx, tx_from, tx_to, venues=(), arb_class="single_swap", n_legs=1, slot=None):
    return {
        "tx_hash": "0x" + f"{idx:02x}" * 32,
        "tx_index": idx,
        "tx_from": tx_from,
        "tx_to": tx_to,
        "venues": list(venues),
        "dexes": ["uniswap_v3"] * len(venues),
        "arb_class": arb_class,
        "assessment": "not_arb",
        "n_legs": n_legs,
        "fees": {"is_system": False},
        "slot_index_est": slot,
        "reasons": [],
    }


def _receipt(idx, logs):
    return {"transactionHash": "0x" + f"{idx:02x}" * 32, "status": "0x1", "logs": logs}


def test_launch_snipe_tags_a_swap_on_a_pool_created_earlier_in_the_block_by_someone_else():
    pool_created = event_topic("PoolCreated(address,address,uint24,int24,address)")
    creation = {
        "address": "0x" + "fa" * 20,
        "logIndex": "0x0",
        "topics": [pool_created, t(WETH), t(USDC), "0x" + "0" * 63 + "1"],
        "data": "0x" + encode(["int24", "address"], [60, POOL_A]).hex(),
    }
    rows = [
        _row(0, "0x" + "c0" * 20, "0x" + "fa" * 20, arb_class="no_swaps", n_legs=0),
        _row(1, USER, BOT, [POOL_A]),
    ]
    receipts = {
        rows[0]["tx_hash"]: _receipt(0, [creation]),
        rows[1]["tx_hash"]: _receipt(
            1,
            [
                transfer(WETH, USER, POOL_A, 10**18, 0),
                v3_swap(POOL_A, 10**18, -3000 * 10**6, 1),
                transfer(USDC, POOL_A, USER, 3000 * 10**6, 2),
            ],
        ),
    }
    txs = {0: {"from": "0x" + "c0" * 20, "to": "0x" + "fa" * 20}, 1: {"from": USER, "to": BOT}}
    annotate_block(SPEC, rows, {"number": 1}, receipts, txs)
    assert rows[1]["snipe"]["pool"] == POOL_A and rows[1]["snipe"]["created_in_tx_index"] == 0
    assert rows[1]["snipe"]["flow"]["token_in"] == WETH and rows[1]["snipe"]["flow"]["token_out"] == USDC
    assert family_of(rows[1]) == "launch_snipe" and "snipe" not in rows[0]
    assert rows[1]["swap_flow"]["amount_out_raw"] == str(3000 * 10**6)


def test_jit_liquidity_needs_add_other_party_swap_remove_by_the_same_sender():
    mint = {
        "address": POOL_A,
        "logIndex": "0x0",
        "topics": [event_topic(SIG_V3_MINT), t(LP), "0x" + "0" * 64, "0x" + "0" * 64],
        "data": "0x"
        + encode(["address", "uint128", "uint256", "uint256"], [LP, 10**18, 10**18, 3000 * 10**6]).hex(),
    }
    burn = {
        "address": POOL_A,
        "logIndex": "0x0",
        "topics": [event_topic(SIG_V3_BURN), t(LP), "0x" + "0" * 64, "0x" + "0" * 64],
        "data": "0x" + encode(["uint128", "uint256", "uint256"], [10**18, 10**18, 3000 * 10**6]).hex(),
    }
    rows = [
        _row(0, LP, BOT, [POOL_A], arb_class="lp_action", n_legs=0),
        _row(1, USER, "0x" + "22" * 20, [POOL_A]),
        _row(2, LP, BOT, [POOL_A], arb_class="lp_action", n_legs=0),
    ]
    receipts = {
        rows[0]["tx_hash"]: _receipt(0, [mint]),
        rows[1]["tx_hash"]: _receipt(1, [v3_swap(POOL_A, 10**18, -3000 * 10**6, 1)]),
        rows[2]["tx_hash"]: _receipt(2, [burn]),
    }
    txs = {0: {"from": LP, "to": BOT}, 1: {"from": USER, "to": "0x" + "22" * 20}, 2: {"from": LP, "to": BOT}}
    annotate_block(SPEC, rows, {"number": 7}, receipts, txs)
    assert rows[0]["jit"]["role"] == "add" and rows[2]["jit"]["role"] == "remove"
    assert rows[0]["jit"]["victims"] == [USER] and rows[0]["jit"]["bundle_id"] == rows[2]["jit"]["bundle_id"]
    assert "jit" not in rows[1]
    # without a swap in between there is no JIT bundle
    rows2 = [
        _row(0, LP, BOT, [POOL_A], arb_class="lp_action", n_legs=0),
        _row(1, LP, BOT, [POOL_A], arb_class="lp_action", n_legs=0),
    ]
    annotate_block(
        SPEC,
        rows2,
        {"number": 8},
        {rows2[0]["tx_hash"]: _receipt(0, [mint]), rows2[1]["tx_hash"]: _receipt(1, [burn])},
        {0: {"from": LP, "to": BOT}, 1: {"from": LP, "to": BOT}},
    )
    assert "jit" not in rows2[0] and "jit" not in rows2[1]


def test_cex_dex_candidate_needs_direct_prices_a_top_position_and_an_edge_beyond_noise():
    book = PriceBook(SPEC.directory / "validation", offline=True)
    ts = 1_700_000_000
    book.set("ETHUSDT", ts, 3000.0)
    base = {
        "swap_flow": {
            "token_in": USDC,
            "amount_in_raw": str(30_000 * 10**6),
            "token_out": WETH,
            "amount_out_raw": str(10_030 * 10**15),
            "venues": [POOL_A],
            "dexes": ["uniswap_v3"],
        },
        "user_index_in_block": 0,
        "first_in_slot": False,
        "assessment": "not_arb",
    }
    # bought 10.03 ETH for 30,000 USDC at the top of the block: 30 bps better than Binance
    cand = cex_dex_candidate(SPEC, book, dict(base), ts)
    assert (
        cand and cand["edge_bps"] == 30.0 and cand["position"] == "top_of_block" and cand["edge_usd"] == 90.0
    )
    # the same swap mid-block is never a candidate: an intra-minute CEX move gives every swap of the block an "edge"
    assert cex_dex_candidate(SPEC, book, {**base, "user_index_in_block": 40}, ts) is None
    assert (
        cex_dex_candidate(
            SPEC,
            book,
            {
                **base,
                "user_index_in_block": 40,
                "swap_flow": {**base["swap_flow"], "amount_out_raw": str(10_100 * 10**15)},
            },
            ts,
        )
        is None
    )
    # first in its estimated flashblock counts as top
    assert (
        cex_dex_candidate(SPEC, book, {**base, "user_index_in_block": 40, "first_in_slot": True}, ts)[
            "position"
        ]
        == "top_of_flashblock"
    )
    # retail size is never a candidate
    small = {
        **base,
        "swap_flow": {
            **base["swap_flow"],
            "amount_in_raw": str(300 * 10**6),
            "amount_out_raw": str(1003 * 10**14),
        },
    }
    assert cex_dex_candidate(SPEC, book, small, ts) is None
    # a token without a direct price (no Binance symbol) is skipped rather than priced through its own leg
    unpriced = {**base, "swap_flow": {**base["swap_flow"], "token_out": "0x" + "ee" * 20}}
    assert cex_dex_candidate(SPEC, book, unpriced, ts) is None


def test_value_rows_tags_cex_dex_rows_and_prices_liquidations():
    book = PriceBook(SPEC.directory / "validation", offline=True)
    ts = 1_700_000_000
    book.set("ETHUSDT", ts, 3000.0)
    row = {
        "block_number": 5,
        "assessment": "not_arb",
        "fees": {"total_fee_wei": 10**15},
        "builder_tip_wei": None,
        "kickback_wei": None,
        "mev_type": "none",
        "user_index_in_block": 1,
        "first_in_slot": False,
        "swap_flow": {
            "token_in": USDC,
            "amount_in_raw": str(30_000 * 10**6),
            "token_out": WETH,
            "amount_out_raw": str(10_030 * 10**15),
        },
        "liquidations": [decode_liquidation(aave_liquidation(0))],
    }
    asyncio.run(value_rows(SPEC, [row], {5: ts}, book))
    assert row["mev_type"] == "cex_dex_candidate" and row["cex_dex"]["edge_bps"] == 30.0
    liq = row["liquidations"][0]
    assert liq["debt_usd"] == 1_000.0 and liq["collateral_usd"] == 1_500.0 and liq["bonus_usd"] == 500.0


def test_builder_economics_sum_priority_fees_tips_and_proposer_payment():
    spec = load_chain("ethereum")
    miner = "0x" + "be" * 20
    proposer = "0x" + "fe" * 20
    rows = [
        {
            "block_number": 1,
            "tx_hash": "0x01",
            "assessment": "trace_supported",
            "fees": {"is_system": False, "priority_fee_wei": 5 * 10**15, "base_fee_wei": 10**15},
            "trace": {"status": "ok"},
            "builder_tip_wei": str(2 * 10**15),
            "gross_usd": 100.0,
            "net_usd": 80.0,
            "native_usd": 2000.0,
        },
        {
            "block_number": 1,
            "tx_hash": "0x02",
            "assessment": "not_arb",
            "fees": {"is_system": False, "priority_fee_wei": 10**15, "base_fee_wei": 10**15},
            "trace": {"status": "not_requested"},
            "builder_tip_wei": None,
            "native_usd": 2000.0,
        },
    ]
    report = {
        "rows": rows,
        "blocks": [{"number": 1, "timestamp": 0, "miner": miner, "builder_tag": "Titan (titanbuilder.xyz)"}],
        "created_at": "now",
    }
    data_dir = SPEC.directory / "validation" / "_no_such_dir"
    facts = measure_builders(spec, report, data_dir)
    b = facts["builders"][0]
    assert (
        b["builder"] == "Titan (titanbuilder.xyz)"
        and b["blocks"] == 1
        and b["traced"] == 1
        and b["transactions"] == 2
    )
    assert (
        b["priority_fees_wei"] == 6 * 10**15
        and b["tips_traced_wei"] == 2 * 10**15
        and b["proposer_payment_wei"] == 0
    )
    assert (
        b["searcher_gross_usd"] == 100.0
        and b["searcher_paid_wei"] == 7 * 10**15
        and b["searcher_paid_share_pct"] == 14.0
    )
    assert facts["per_block"][0]["body_available"] is False and facts["trace_coverage_pct"] == 50.0
    _ = proposer  # the proposer payment needs the cached block body; covered by the live `builders` command


def test_jit_needs_the_same_wallet_not_just_the_same_contract():
    """Optimism 159083366: two wallets using one liquidity manager (an add at #16, a remove at #70) are no JIT bundle."""
    mint = {
        "address": POOL_A,
        "logIndex": "0x0",
        "topics": [event_topic(SIG_V3_MINT), t(BOT), "0x" + "0" * 64, "0x" + "0" * 64],
        "data": "0x"
        + encode(["address", "uint128", "uint256", "uint256"], [BOT, 10**18, 10**18, 3000 * 10**6]).hex(),
    }
    burn = {
        "address": POOL_A,
        "logIndex": "0x0",
        "topics": [event_topic(SIG_V3_BURN), t(BOT), "0x" + "0" * 64, "0x" + "0" * 64],
        "data": "0x" + encode(["uint128", "uint256", "uint256"], [10**18, 10**18, 3000 * 10**6]).hex(),
    }
    other = "0x" + "98" * 20
    rows = [
        _row(0, LP, BOT, [POOL_A], arb_class="lp_action", n_legs=0),
        _row(1, USER, "0x" + "22" * 20, [POOL_A]),
        _row(2, other, BOT, [POOL_A], arb_class="lp_action", n_legs=0),
    ]
    receipts = {
        rows[0]["tx_hash"]: _receipt(0, [mint]),
        rows[1]["tx_hash"]: _receipt(1, [v3_swap(POOL_A, 10**18, -3000 * 10**6, 1)]),
        rows[2]["tx_hash"]: _receipt(2, [burn]),
    }
    annotate_block(
        SPEC,
        rows,
        {"number": 9},
        receipts,
        {
            0: {"from": LP, "to": BOT},
            1: {"from": USER, "to": "0x" + "22" * 20},
            2: {"from": other, "to": BOT},
        },
    )
    assert not any("jit" in r for r in rows)


def test_a_rebalance_chain_is_no_jit_bundle():
    """Ethereum 26339453: #25 add, #30 remove + add, #32 remove by one wallet had read as a bundle #30-#32 whose result
    counted #25's principal ($83,599). A leg that removes and adds is no JIT leg, nor is a pair with the wallet's own
    liquidity action between."""
    mint = {
        "address": POOL_A,
        "logIndex": "0x0",
        "topics": [event_topic(SIG_V3_MINT), t(LP), "0x" + "0" * 64, "0x" + "0" * 64],
        "data": "0x"
        + encode(["address", "uint128", "uint256", "uint256"], [LP, 10**18, 10**18, 3000 * 10**6]).hex(),
    }
    burn = {
        "address": POOL_A,
        "logIndex": "0x1",
        "topics": [event_topic(SIG_V3_BURN), t(LP), "0x" + "0" * 64, "0x" + "0" * 64],
        "data": "0x" + encode(["uint128", "uint256", "uint256"], [10**18, 10**18, 3000 * 10**6]).hex(),
    }
    swap = lambda i: _receipt(i, [v3_swap(POOL_A, 10**18, -3000 * 10**6, 1)])  # noqa: E731
    rows = [
        _row(0, LP, BOT, [POOL_A], arb_class="lp_action", n_legs=0),
        _row(1, USER, "0x" + "22" * 20, [POOL_A]),
        _row(2, LP, BOT, [POOL_A], arb_class="lp_action", n_legs=0),
        _row(3, USER, "0x" + "22" * 20, [POOL_A]),
        _row(4, LP, BOT, [POOL_A], arb_class="lp_action", n_legs=0),
    ]
    receipts = {
        rows[0]["tx_hash"]: _receipt(0, [mint]),
        rows[1]["tx_hash"]: swap(1),
        rows[2]["tx_hash"]: _receipt(2, [burn, mint]),
        rows[3]["tx_hash"]: swap(3),
        rows[4]["tx_hash"]: _receipt(4, [burn]),
    }
    txs = {i: {"from": r["tx_from"], "to": r["tx_to"]} for i, r in enumerate(rows)}
    annotate_block(SPEC, rows, {"number": 10}, receipts, txs)
    assert not any("jit" in r for r in rows)
