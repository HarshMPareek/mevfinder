"""Pool-math invariant (geometry.invariant_violations): a pool exploit is never an arbitrage cycle."""

import json

from mevlab.decode.swaps import decode_swap_log
from mevlab.pools.prices import invariant_violations
from tests import FIXTURES

FIX = FIXTURES / "kyber_elastic_exploit_op_782ca99b_swaps.json"
WETH, USDCE = "0x4200000000000000000000000000000000000006", "0x7f5c764cbc14f9669b88837ca1490cca17c31607"
KYBER_POOL, UNI_POOL = (
    "0x683860e93fab18b8e2e52f9be310d9b36b49677a",
    "0x85149247691df622eaf1a8bd0cafd40bc45154a9",
)


def log_price(usdc_per_weth: float) -> float:
    import math

    return math.log(usdc_per_weth * 1e6 / 1e18)  # raw token1/token0 (USDC.e 6 decimals per WETH 18)


def legs():
    return [decode_swap_log(x).resolve_tokens(WETH, USDCE) for x in json.loads(FIX.read_text())]


def test_kyber_elastic_drain_breaks_the_flow_price_invariant():
    # OP 158942523 #8: the bot sold WETH into the KyberSwap Elastic pool at 2,070.9 (so it started >= 2,070.9),
    # then the pool paid out 0.447 WETH net and still ended at 2,038.3: broken liquidity math, a pool exploit
    hits = invariant_violations(legs())
    assert [k for k, _ in hits] == [KYBER_POOL]
    # with the tracker's real pre-transaction price the excess is larger still
    pre = {KYBER_POOL: log_price(2_080.0)}  # a start consistent with the first sale (>= 2,070.9)
    assert invariant_violations(legs(), pre_prices=pre)[0][1] > 0.02


def test_a_correct_pool_never_fires():
    # the Uniswap V3 leg of the same transaction, alone and with an honest pre-trade price
    uni = [x for x in legs() if x.pool == UNI_POOL]
    assert invariant_violations(uni) == []
    # the bot sold 0.398 WETH into it at 2,232.7 and it ended at 2,217.5: a start of 2,240 is consistent
    assert invariant_violations(uni, pre_prices={UNI_POOL: log_price(2_240.0)}) == []
    # a start below where a seller's trade ended is impossible, and the check says so
    assert invariant_violations(uni, pre_prices={UNI_POOL: log_price(2_181.0)}) != []


def test_hooked_pools_are_skipped():
    assert invariant_violations(legs(), hooked={KYBER_POOL}) == []


def _bsc_inventory():
    from arbkit.chain.spec import load_chain

    class Book:
        def price_at(self, symbol, ts):
            return 703.0

    return load_chain("bsc"), Book()


USDT_BSC, USDC_BSC, BUSD_BSC = (
    "0x55d398326f99059ff775485246999027b3197955",
    "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",
    "0xe9e7cea3dedca5984780bafc599bd69add087d56",
)


def _brkl_row(wbnb):
    # BSC 130965616 #5 (legs as stored): WBNB -> USDT (PCS V3), 478.23 USDT -> BRKL -> BUSD -> USDC through stale V2 pools
    brkl = "0x66cafcf6c32315623c7ffd3f2ff690aa36ebed38"
    legs = [
        {
            "venue": "0x172fcd41e0913e95784454622d1c3724f546f849",
            "token_in": wbnb,
            "amount_in": "982444942247967803",
            "token_out": USDT_BSC,
            "amount_out": "691390730231209594066",
            "order": 1,
        },
        {
            "venue": "0xf06818d93d506459a8df15de451aa0c26084e2a5",
            "token_in": USDT_BSC,
            "amount_in": "478228506961671086899",
            "token_out": brkl,
            "amount_out": "337265315511878191785715",
            "order": 2,
        },
        {
            "venue": "0x0e537bb44eb6064d12326ff2543d918e9b9a5482",
            "token_in": brkl,
            "amount_in": "337265315511878191785715",
            "token_out": BUSD_BSC,
            "amount_out": "691390044511125426932",
            "order": 3,
        },
        {
            "venue": "0x22536030b9ce783b6ddfb9a39ac7f439f568e5e6",
            "token_in": BUSD_BSC,
            "amount_in": "691390044511125426932",
            "token_out": USDC_BSC,
            "amount_out": "690915400338512514457",
            "order": 4,
        },
    ]
    return {
        "assessment": "not_arb",
        "mev_type": "none",
        "arb_class": "split_route",
        "n_legs": 4,
        "reasons": [],
        "tx_to": "0x976aed195978c088dc0a9d170987f1b59cf698da",
        "sender_wallet_trades": False,
        "event_label": {"n_legs": 4, "venues": [lg["venue"] for lg in legs]},
        "trace": {
            "status": "ok",
            "verdict": "no_taker",
            "legs": legs,
            "asset_deltas": {
                wbnb: "-982444942247967803",
                USDT_BSC: "213162223269538507167",
                USDC_BSC: "690915400338512514457",
            },
        },
    }


def test_inventory_arbitrage_family_from_a_value_closed_route():
    from arbkit.value.pricing import inventory_arbitrage

    spec, book = _bsc_inventory()
    row = _brkl_row(spec.weth)
    inventory_arbitrage(spec, book, row, 1)
    assert row["mev_type"] == "inventory_arbitrage" and 210 < row["inventory_arb"]["gain_usd"] < 216
    # a user's multi-hop sell that loses value to fees is never the family
    loser = _brkl_row(spec.weth)
    loser["trace"]["legs"][-1]["amount_out"] = "400000000000000000000"
    loser["trace"]["asset_deltas"][USDC_BSC] = "400000000000000000000"
    inventory_arbitrage(spec, book, loser, 1)
    assert loser["mev_type"] == "none"
    # an unpriced token that does not net to zero: no judgement
    odd = _brkl_row(spec.weth)
    odd["trace"]["asset_deltas"] = {"0x" + "ab" * 20: "-5", USDC_BSC: str(10**20)}
    inventory_arbitrage(spec, book, odd, 1)
    assert odd["mev_type"] == "none"


def test_inventory_arbitrage_is_never_a_users_swap_or_an_unexplained_asset():
    from arbkit.value.pricing import inventory_arbitrage

    spec, book = _bsc_inventory()
    # the sender's own wallet pays or receives through the callee (Ethereum 26317003 #52, a router swap): a user
    user = {**_brkl_row(spec.weth), "sender_wallet_trades": True}
    inventory_arbitrage(spec, book, user, 1)
    assert user["mev_type"] == "none"
    # an ERC-4337 user operation
    op = {**_brkl_row(spec.weth), "tx_to": "0x5ff137d4b0fdcd49dca30c7cf57e578a026d2789"}
    inventory_arbitrage(spec, book, op, 1)
    assert op["mev_type"] == "none"
    # an asset the swaps do not explain: the PSM adapter's DAI folded into the group (Ethereum 26317003 #52)
    phantom = _brkl_row(spec.weth)
    phantom["trace"]["asset_deltas"][BUSD_BSC] = str(11428 * 10**18)
    inventory_arbitrage(spec, book, phantom, 1)
    assert phantom["mev_type"] == "none"


def test_inventory_arbitrage_one_pool_off_market_only_when_its_output_is_kept():
    from arbkit.value.pricing import inventory_arbitrage

    spec, book = _bsc_inventory()
    wbnb = spec.weth
    # one venue against the outside price, output kept (Ethereum 26315600 #2 sold WETH on Ekubo 6 % over Binance):
    # 1 WBNB -> 760 USDT in one pool (market 703) plus a separate dust loop USDT -> USDC -> USDT
    legs = [
        {
            "venue": "0xaa",
            "token_in": wbnb,
            "amount_in": str(10**18),
            "token_out": USDT_BSC,
            "amount_out": str(760 * 10**18),
            "order": 1,
        },
        {
            "venue": "0xbb",
            "token_in": USDT_BSC,
            "amount_in": str(10 * 10**18),
            "token_out": USDC_BSC,
            "amount_out": str(10 * 10**18),
            "order": 2,
        },
        {
            "venue": "0xcc",
            "token_in": USDC_BSC,
            "amount_in": str(10 * 10**18),
            "token_out": USDT_BSC,
            "amount_out": str(10 * 10**18),
            "order": 3,
        },
    ]
    cex_dex = {
        **_brkl_row(wbnb),
        "trace": {
            "status": "ok",
            "verdict": "no_taker",
            "legs": legs,
            "asset_deltas": {wbnb: str(-(10**18)), USDT_BSC: str(760 * 10**18)},
        },
    }
    inventory_arbitrage(spec, book, cex_dex, 1)
    assert cex_dex["mev_type"] == "none" and "inventory_gain_in_one_pool" in cex_dex["reasons"]
    # the stale pool's output sold on through another pool is the route itself (BSC 130945368 #3, BTCB -> CAKE stale)
    legs = [
        {
            "venue": "0xaa",
            "token_in": wbnb,
            "amount_in": str(10**18),
            "token_out": USDT_BSC,
            "amount_out": str(760 * 10**18),
            "order": 1,
        },
        {
            "venue": "0xbb",
            "token_in": USDT_BSC,
            "amount_in": str(760 * 10**18),
            "token_out": USDC_BSC,
            "amount_out": str(760 * 10**18),
            "order": 2,
        },
    ]
    routed = {
        **_brkl_row(wbnb),
        "trace": {
            "status": "ok",
            "verdict": "no_taker",
            "legs": legs,
            "asset_deltas": {wbnb: str(-(10**18)), USDC_BSC: str(760 * 10**18)},
        },
    }
    inventory_arbitrage(spec, book, routed, 1)
    assert routed["mev_type"] == "inventory_arbitrage"
