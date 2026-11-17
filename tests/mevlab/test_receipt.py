import pytest
from eth_abi import encode

from mevlab.block.position import TxTouch, classify_positions
from mevlab.decode.signatures import SIG_SOLIDLY_SWAP, SIG_V2_MINT, SIG_V3_SWAP, TRANSFER_TOPIC, event_topic
from mevlab.pools.meta import DictPoolMeta, PoolMeta
from mevlab.receipt.classify import classify_tx
from mevlab.receipt.path import analyze_path

WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
BOT = "0x" + "b0" * 20
EOA = "0x" + "e0" * 20
POOL_A = "0x" + "a1" * 20  # V3 WETH/USDC
POOL_B = "0x" + "b2" * 20  # Aerodrome WETH/USDC


def t(a: str) -> str:
    return "0x" + a[2:].lower().rjust(64, "0")


def v3_swap(pool, a0, a1, li):
    return {
        "address": pool,
        "logIndex": hex(li),
        "topics": [event_topic(SIG_V3_SWAP), t(BOT), t(BOT)],
        "data": "0x"
        + encode(["int256", "int256", "uint160", "uint128", "int24"], [a0, a1, 2**96, 10**20, 0]).hex(),
    }


def solidly_swap(pool, a0in, a1in, a0out, a1out, li):
    return {
        "address": pool,
        "logIndex": hex(li),
        "topics": [event_topic(SIG_SOLIDLY_SWAP), t(BOT), t(BOT)],
        "data": "0x" + encode(["uint256"] * 4, [a0in, a1in, a0out, a1out]).hex(),
    }


def transfer(token, frm, to, v, li):
    return {
        "address": token,
        "logIndex": hex(li),
        "topics": [TRANSFER_TOPIC, t(frm), t(to)],
        "data": "0x" + encode(["uint256"], [v]).hex(),
    }


RESOLVER = DictPoolMeta(
    {POOL_A: PoolMeta(WETH, USDC, "uniswap_v3"), POOL_B: PoolMeta(WETH, USDC, "aerodrome_v2")}
)


def make_tx(logs, status=1, value=0, to=BOT):
    tx = {
        "hash": "0x" + "01" * 32,
        "blockNumber": "0x10",
        "transactionIndex": "0x7",
        "from": EOA,
        "to": to,
        "value": hex(value),
    }
    rc = {
        "transactionHash": tx["hash"],
        "blockNumber": "0x10",
        "transactionIndex": "0x7",
        "status": hex(status),
        "gasUsed": hex(150_000),
        "effectiveGasPrice": hex(1_500_000),
        "l1Fee": hex(2 * 10**12),
        "logs": logs,
    }
    return tx, rc


@pytest.mark.asyncio
async def test_two_leg_cycle_is_arb():
    # sell 1 WETH into pool A for 3000 USDC, sell 3000 USDC into pool B for 1.01 WETH
    logs = [
        transfer(WETH, BOT, POOL_A, 10**18, 0),
        v3_swap(POOL_A, 10**18, -3000 * 10**6, 1),
        transfer(USDC, POOL_A, BOT, 3000 * 10**6, 2),
        transfer(USDC, BOT, POOL_B, 3000 * 10**6, 3),
        solidly_swap(POOL_B, 0, 3000 * 10**6, 101 * 10**16, 0, 4),
        transfer(WETH, POOL_B, BOT, 101 * 10**16, 5),
    ]
    tx, rc = make_tx(logs)
    lab = await classify_tx(tx, rc, RESOLVER, base_fee_per_gas=1_000_000, weth=WETH)
    assert lab.is_arb and lab.arb_class == "atomic_cycle"
    assert lab.n_legs == 2 and lab.sequential_chain
    assert lab.beneficiary == BOT and lab.profit_token == WETH and lab.gross_profit_raw == 10**16
    assert lab.dexes == ["aerodrome_v2", "uniswap_v3"]
    e = lab.economics
    assert e["priority_fee_per_gas"] == 500_000 and e["priority_fee_wei"] == 150_000 * 500_000
    assert e["l1_fee_wei"] == 2 * 10**12 and e["total_fee_wei"] == 150_000 * 1_500_000 + 2 * 10**12
    assert e["base_fee_wei"] == 150_000 * 1_000_000 and e["family"] == "op-stack"


@pytest.mark.asyncio
async def test_single_swap_and_split_route_are_not_arb():
    tx, rc = make_tx([v3_swap(POOL_A, 10**18, -3000 * 10**6, 0)])
    lab = await classify_tx(tx, rc, RESOLVER, base_fee_per_gas=1, weth=WETH)
    assert not lab.is_arb and lab.arb_class == "single_swap"

    # two legs both selling WETH for USDC in different pools: aggregator split, no loop
    tx, rc = make_tx(
        [v3_swap(POOL_A, 10**18, -3000 * 10**6, 0), solidly_swap(POOL_B, 10**18, 0, 0, 3001 * 10**6, 1)]
    )
    lab = await classify_tx(tx, rc, RESOLVER, base_fee_per_gas=1, weth=WETH)
    assert not lab.is_arb and lab.arb_class == "split_route"
    assert "unconsumed_outputs" in lab.reasons


@pytest.mark.asyncio
async def test_layer0_liquidity_event_excludes():
    mint = {
        "address": POOL_B,
        "logIndex": "0x9",
        "topics": [event_topic(SIG_V2_MINT), t(BOT)],
        "data": "0x" + encode(["uint256", "uint256"], [1, 1]).hex(),
    }
    logs = [
        v3_swap(POOL_A, 10**18, -3000 * 10**6, 0),
        solidly_swap(POOL_B, 0, 3000 * 10**6, 10**18, 0, 1),
        mint,
    ]
    tx, rc = make_tx(logs)
    lab = await classify_tx(tx, rc, RESOLVER, base_fee_per_gas=1, weth=WETH)
    assert lab.arb_class == "lp_action" and not lab.is_arb


@pytest.mark.asyncio
async def test_reverted_and_probe():
    tx, rc = make_tx([], status=0)
    assert (await classify_tx(tx, rc, RESOLVER, base_fee_per_gas=1, weth=WETH)).arb_class == "reverted"
    tx, rc = make_tx([], status=1, value=1)
    assert (await classify_tx(tx, rc, RESOLVER, base_fee_per_gas=1, weth=WETH)).arb_class == "probe"


def test_unordered_cycle_detected():
    from mevlab.decode.model import SwapLeg

    # legs emitted out of order (reverse-ordered cyclic arb): still flow-balanced
    a = SwapLeg("clmm_v3", POOL_A, 5, token_in=USDC, token_out=WETH, amount_in=3000, amount_out=101)
    b = SwapLeg("clmm_v3", POOL_B, 1, token_in=WETH, token_out=USDC, amount_in=100, amount_out=3000)
    pc = analyze_path([a, b])
    assert pc.is_cycle and pc.sequential_chain  # sorted by log index -> b then a, which chains
    c = SwapLeg("clmm_v3", "0x" + "c3" * 20, 2, token_in=USDC, token_out="0xdai", amount_in=1, amount_out=1)
    pc = analyze_path([a, b, c])
    assert not pc.is_cycle and pc.unconsumed_outputs == ["0xdai"] and "leftover_token" in pc.reasons


def test_positions_backrun_and_tob():
    txs = [
        TxTouch(0, "0x0", 0, {}, is_system=True),
        TxTouch(1, "0x1", 10**9, {POOL_A: 0}),  # first user tx, max fee, touches A -> TOB arb
        TxTouch(2, "0x2", 10**6, {POOL_B: +1}),  # victim swap on B (zero_for_one)
        TxTouch(3, "0x3", 10**6 + 1, {POOL_B: -1, POOL_A: 0}),  # arb: opposite direction on B
        TxTouch(4, "0x4", 5 * 10**5, {"0xother": 0}),  # arb with no prior toucher: mid block
    ]
    pos = classify_positions(txs, {1, 3, 4})
    assert pos[1].trigger_type == "top_of_block"
    assert pos[3].trigger_type == "backrun" and pos[3].trigger_tx_index == 2 and pos[3].trigger_pool == POOL_B
    assert pos[4].trigger_type == "mid_block"


def test_beneficiary_fallback_to_collector():
    from mevlab.decode.model import Transfer
    from mevlab.receipt.flows import beneficiary, net_deltas

    collector = "0x" + "c0" * 20
    tr = [
        Transfer(WETH, BOT, POOL_A, 100, 0),
        Transfer(WETH, POOL_B, collector, 5, 1),
        Transfer(USDC, POOL_A, POOL_B, 300, 2),
    ]
    d = net_deltas(tr)
    # neither tx.to nor tx.from gained; the collector did, pools/tokens are excluded
    who, delta = beneficiary(d, [BOT, EOA], exclude={POOL_A, POOL_B, WETH, USDC})
    assert who == collector and delta == {WETH: 5}
    who, _ = beneficiary(d, [BOT, EOA], exclude={POOL_A, POOL_B, WETH, USDC, collector})
    assert who is None


def test_economics_families():
    from mevlab.receipt.economics import economics_from_receipt

    rc = {
        "gasUsed": hex(1_000_000),
        "effectiveGasPrice": hex(20_000_000),
        "gasUsedForL1": hex(300_000),
        "timeboosted": True,
    }
    e = economics_from_receipt(rc, 20_000_000, {}, "arbitrum")
    assert e.l1_fee_wei == 300_000 * 20_000_000 and e.total_fee_wei == 1_000_000 * 20_000_000
    assert e.base_fee_wei == 700_000 * 20_000_000 and e.priority_fee_wei == 0 and e.timeboosted is True
    e = economics_from_receipt({"gasUsed": hex(21000), "effectiveGasPrice": hex(30)}, 25, {}, "ethereum")
    assert e.l1_fee_wei == 0 and e.priority_fee_wei == 21000 * 5 and e.base_fee_wei == 21000 * 25


@pytest.mark.asyncio
async def test_single_venue_round_trip_is_not_arb():
    logs = [v3_swap(POOL_A, 10**18, -3000 * 10**6, 0), v3_swap(POOL_A, -99 * 10**16, 3000 * 10**6, 1)]
    tx, rc = make_tx(logs)
    lab = await classify_tx(tx, rc, RESOLVER, base_fee_per_gas=1, weth=WETH)
    assert not lab.is_arb and lab.arb_class == "round_trip" and "single_venue_loop" in lab.reasons


@pytest.mark.asyncio
async def test_geometry_pre_cycle_and_backrun_sign():
    from mevlab.pools.prices import PoolPriceTracker

    tr = PoolPriceTracker()
    # earlier state: pool A price (token1/token0) = 3000e6/1e18 -> ln ratio; pool B price higher (WETH pricier)
    pa = 2 * __import__("math").log((3000e6 / 1e18) ** 0.5)
    pb = 2 * __import__("math").log((3030e6 / 1e18) ** 0.5)
    tr.log_price[POOL_A] = pa
    tr.log_price[POOL_B] = pb
    sqrt_b_after = int(((3010e6 / 1e18) ** 0.5) * 2**96)
    sqrt_a_after = int(((3005e6 / 1e18) ** 0.5) * 2**96)
    # arb: sell WETH into B (price of WETH there falls), buy WETH back in A (price rises)
    logs = [
        {
            "address": POOL_B,
            "logIndex": "0x0",
            "topics": [event_topic(SIG_V3_SWAP), t(BOT), t(BOT)],
            "data": "0x"
            + encode(
                ["int256", "int256", "uint160", "uint128", "int24"],
                [10**18, -3020 * 10**6, sqrt_b_after, 10**20, 0],
            ).hex(),
        },
        {
            "address": POOL_A,
            "logIndex": "0x1",
            "topics": [event_topic(SIG_V3_SWAP), t(BOT), t(BOT)],
            "data": "0x"
            + encode(
                ["int256", "int256", "uint160", "uint128", "int24"],
                [-101 * 10**16, 3020 * 10**6, sqrt_a_after, 10**20, 0],
            ).hex(),
        },
        transfer(WETH, POOL_A, BOT, 101 * 10**16, 2),
    ]
    tx, rc = make_tx(logs)
    lab = await classify_tx(tx, rc, RESOLVER, base_fee_per_gas=1, weth=WETH, tracker=tr)
    assert lab.is_arb and lab.dislocation_evidence["pre_log_product"] > 0
    assert lab.dislocation_evidence["continuity_gap"] < 0.01  # each leg funded by the one before
    assert lab.price_moves[POOL_B] == -1 and lab.price_moves[POOL_A] == 1
    # tracker advanced to post prices
    assert abs(tr.get(POOL_A) - 2 * __import__("math").log(sqrt_a_after / 2**96)) < 1e-9


@pytest.mark.asyncio
async def test_attempted_cycle_and_probe_with_own_logs():
    # cycle through two venues but nobody gains -> attempted_cycle
    logs = [
        v3_swap(POOL_A, 10**18, -3000 * 10**6, 0),
        solidly_swap(POOL_B, 0, 3000 * 10**6, 99 * 10**16, 0, 1),
        transfer(WETH, BOT, POOL_A, 10**18, 2),
        transfer(WETH, POOL_B, BOT, 99 * 10**16, 3),
    ]
    tx, rc = make_tx(logs)
    lab = await classify_tx(tx, rc, RESOLVER, base_fee_per_gas=1, weth=WETH)
    assert lab.arb_class == "attempted_cycle" and not lab.is_arb
    # a probe may emit logs from the bot contract itself
    own = {"address": BOT, "logIndex": "0x0", "topics": ["0x" + "77" * 32], "data": "0x"}
    tx, rc = make_tx([own], value=0)
    assert (await classify_tx(tx, rc, RESOLVER, base_fee_per_gas=1, weth=WETH)).arb_class == "probe"


def test_backrun_sign_test():
    txs = [
        TxTouch(0, "0x0", 0, {}, is_system=True),
        TxTouch(1, "0x1", 10**6, {POOL_B: +1}),  # victim pushed B's price up
        TxTouch(2, "0x2", 10**6, {POOL_B: -1, POOL_A: +1}),
    ]  # arb pushed it back down
    pos = classify_positions(txs, {2})
    assert (
        pos[2].trigger_type == "backrun"
        and pos[2].trigger_price_reversal is True
        and pos[2].trigger_tx_index == 1  # the victim one transaction earlier
    )
    txs[2].pools[POOL_B] = +1  # same direction as the victim: adjacency without reversal
    pos = classify_positions(txs, {2})
    assert pos[2].trigger_type != "backrun" or pos[2].trigger_price_reversal is not True


def test_flashblock_boundary_estimate():
    from mevlab.block.flashblocks import estimate_boundaries, monotone_share, slot_indexes

    # three "flashblocks": fees fall within each, jump at boundaries
    fees = [900, 500, 100, 50] + [800, 300, 20] + [1000, 400, 400, 10]
    starts = estimate_boundaries(fees, n_slots=3)
    assert starts == [0, 4, 7]
    assert slot_indexes(fees, 3) == [1, 1, 1, 1, 2, 2, 2, 3, 3, 3, 3]
    assert monotone_share(fees, starts) == 1.0
    assert estimate_boundaries([5], 10) == [0] and estimate_boundaries([], 10) == []


@pytest.mark.asyncio
async def test_v4_native_leg_equivalence_and_payout_gross():
    """#1-style tx: V4 native pool (ETH->token, settle invisible) then V3 token->WETH, profit paid to a collector."""
    from mevlab.decode.signatures import SIG_V4_SWAP, SIG_WETH_WITHDRAWAL, ZERO_ADDRESS
    from mevlab.pools.meta import DictPoolMeta, PoolMeta

    PM = "0x498581ff718922c3f8e6a244956af099b2652b2b"
    TOK = "0x" + "b2" * 20
    POOL_ID = "0x" + "ab" * 32
    COLLECTOR = "0x" + "c0" * 20
    resolver = DictPoolMeta(
        {
            POOL_A: PoolMeta(TOK, WETH, "uniswap_v3"),
            f"{PM}:{POOL_ID}": PoolMeta(ZERO_ADDRESS, TOK, "uniswap_v4"),
        }
    )
    v4 = {
        "address": PM,
        "logIndex": "0x1",
        "topics": [event_topic(SIG_V4_SWAP), POOL_ID, t(BOT)],
        "data": "0x"
        + encode(
            ["int128", "int128", "uint160", "uint128", "int24", "uint24"],
            [-(10**18), 45_000 * 10**18, 2**96, 10**20, 0, 3000],
        ).hex(),
    }
    logs = [
        transfer(WETH, POOL_A, BOT, 1035 * 10**15, 0),  # V3 flash pays WETH first
        v4,  # ETH -> TOK on V4 (native in)
        transfer(TOK, PM, BOT, 45_000 * 10**18, 2),  # V4 take
        transfer(TOK, BOT, POOL_A, 45_000 * 10**18, 3),  # repay V3 with TOK
        v3_swap(POOL_A, 45_000 * 10**18, -1035 * 10**15, 4),  # V3: TOK in, WETH out
        {
            "address": WETH,
            "logIndex": "0x5",
            "topics": [event_topic(SIG_WETH_WITHDRAWAL), t(BOT)],
            "data": "0x" + encode(["uint256"], [10**18]).hex(),
        },  # unwrap 1 ETH to settle V4
        transfer(WETH, BOT, COLLECTOR, 35 * 10**15, 6),  # payout 0.035 WETH
    ]
    tx, rc = make_tx(logs)
    lab = await classify_tx(tx, rc, resolver, base_fee_per_gas=1, weth=WETH)
    assert lab.is_arb and lab.arb_class == "atomic_cycle"
    assert lab.beneficiary == BOT and lab.profit_token == WETH and lab.gross_profit_raw == 35 * 10**15
    assert "v4_native_synthetic_transfers" in lab.reasons
    assert lab.payouts[WETH][COLLECTOR] == str(35 * 10**15)


@pytest.mark.asyncio
async def test_unresolved_leg_blocks_cycle_verdict():
    from mevlab.pools.meta import DictPoolMeta, PoolMeta

    resolver = DictPoolMeta({POOL_A: PoolMeta(WETH, USDC, "uniswap_v3")})  # POOL_B unknown
    logs = [
        v3_swap(POOL_A, 10**18, -3000 * 10**6, 0),
        v3_swap(POOL_B, -(10**18), 3000 * 10**6, 1),
        transfer(WETH, POOL_B, BOT, 10**16, 2),
    ]
    tx, rc = make_tx(logs)
    lab = await classify_tx(tx, rc, resolver, base_fee_per_gas=1, weth=WETH)
    assert lab.arb_class == "unknown" and not lab.is_arb and "unresolved_leg_blocks_verdict" in lab.reasons


@pytest.mark.asyncio
async def test_hooked_v4_zero_amounts_marks_gross_unreliable():
    from mevlab.decode.signatures import SIG_V4_SWAP, ZERO_ADDRESS
    from mevlab.pools.meta import DictPoolMeta, PoolMeta

    PM = "0x498581ff718922c3f8e6a244956af099b2652b2b"
    POOL_ID = "0x" + "cd" * 32
    resolver = DictPoolMeta(
        {
            POOL_A: PoolMeta(WETH, USDC, "uniswap_v3"),
            f"{PM}:{POOL_ID}": PoolMeta(ZERO_ADDRESS, USDC, "uniswap_v4"),
        }
    )
    v4 = {
        "address": PM,
        "logIndex": "0x1",
        "topics": [event_topic(SIG_V4_SWAP), POOL_ID, t(BOT)],
        "data": "0x"
        + encode(["int128", "int128", "uint160", "uint128", "int24", "uint24"], [0, 0, 2**96, 0, 0, 0]).hex(),
    }
    logs = [
        v4,
        transfer(USDC, PM, POOL_A, 39 * 10**6, 2),
        v3_swap(POOL_A, -(10**16), 39 * 10**6, 3),
        transfer(WETH, POOL_A, BOT, 10**16, 4),
    ]
    tx, rc = make_tx(logs)
    lab = await classify_tx(tx, rc, resolver, base_fee_per_gas=1, weth=WETH)
    assert "hooked_v4_zero_amounts" in lab.reasons and lab.gross_unreliable is True


@pytest.mark.asyncio
async def test_flash_loan_repayment_is_not_a_payout():
    """Morpho-style flash loan: borrow 77k WETH, cycle earns 5e12 wei, repay 77k WETH. Gross must be 5e12."""
    MORPHO = "0x" + "bb" * 20
    BIG = 77_000 * 10**18
    logs = [
        transfer(WETH, MORPHO, BOT, BIG, 0),
        transfer(WETH, BOT, POOL_A, 10**18, 1),
        v3_swap(POOL_A, 10**18, -3000 * 10**6, 2),
        transfer(USDC, POOL_A, BOT, 3000 * 10**6, 3),
        transfer(USDC, BOT, POOL_B, 3000 * 10**6, 4),
        solidly_swap(POOL_B, 0, 3000 * 10**6, 10**18 + 5 * 10**12, 0, 5),
        transfer(WETH, POOL_B, BOT, 10**18 + 5 * 10**12, 6),
        transfer(WETH, BOT, MORPHO, BIG, 7),
    ]
    tx, rc = make_tx(logs)
    lab = await classify_tx(tx, rc, RESOLVER, base_fee_per_gas=1, weth=WETH)
    assert lab.is_arb and lab.gross_profit_raw == 5 * 10**12 and lab.gross_delta_raw == 5 * 10**12
    assert lab.payouts == {}


def test_amount_closure_rejects_sell_plus_dust_and_accepts_unordered_profit():
    from mevlab.decode.model import SwapLeg
    from mevlab.receipt.path import analyze_path

    # sell 1.637 WETH for 4000 USDC + dust swap back: two unbalanced tokens -> not a cycle
    a = SwapLeg(
        "clmm_v3", POOL_A, 1, token_in=WETH, token_out=USDC, amount_in=1637 * 10**15, amount_out=4000 * 10**6
    )
    b = SwapLeg(
        "clmm_v3", POOL_B, 2, token_in=USDC, token_out=WETH, amount_in=210_600, amount_out=86 * 10**12
    )
    pc = analyze_path([a, b])
    assert not pc.is_cycle and "multi_token_imbalance" in pc.reasons and set(pc.unbalanced) == {WETH, USDC}
    # unordered 3-leg cycle where the profit token is not the first leg's input: WETH +0.25
    B20 = "0x" + "b2" * 20
    l1 = SwapLeg(
        "clmm_v4_singleton",
        "0x" + "49" * 20,
        1,
        token_in=USDC,
        token_out=B20,
        amount_in=12_924 * 10**6,
        amount_out=290_455 * 10**18,
    )
    l2 = SwapLeg(
        "clmm_v3",
        POOL_A,
        2,
        token_in=WETH,
        token_out=USDC,
        amount_in=5_267 * 10**15,
        amount_out=12_924 * 10**6,
    )
    l3 = SwapLeg(
        "clmm_v3",
        POOL_B,
        3,
        token_in=B20,
        token_out=WETH,
        amount_in=290_455 * 10**18,
        amount_out=5_521 * 10**15,
    )
    pc = analyze_path([l1, l2, l3])
    assert pc.is_cycle and pc.profit_token == WETH and pc.gain == 254 * 10**15 and not pc.lost
    # same cycle but losing: attempted
    l3b = SwapLeg(
        "clmm_v3",
        POOL_B,
        3,
        token_in=B20,
        token_out=WETH,
        amount_in=290_455 * 10**18,
        amount_out=5_000 * 10**15,
    )
    pc = analyze_path([l1, l2, l3b])
    assert pc.is_cycle and pc.lost and "cycle_lost" in pc.reasons


@pytest.mark.asyncio
async def test_v4_hook_claim_is_netted_out_of_the_event_gross():
    """C-CLS-16 (BaseScan-checked 0x0d1b83c0…, 0x33a7b88a…): a Basecat-style hook mints itself 1 % of the
    ETH input as an ERC-6909 claim; the Swap event carries the pool's delta net of it, so the event gross is
    high by exactly the claim unless the claim mint is attributed to the swapper."""
    from mevlab.decode.signatures import (
        ERC6909_TRANSFER_TOPIC,
        SIG_V4_SWAP,
        SIG_WETH_WITHDRAWAL,
        ZERO_ADDRESS,
    )
    from mevlab.pools.meta import DictPoolMeta, PoolMeta

    PM = "0x498581ff718922c3f8e6a244956af099b2652b2b"
    TOK = "0x" + "b2" * 20
    POOL_ID = "0x" + "ab" * 32
    HOOK_HOLDING = "0x" + "a2" * 20
    COLLECTOR = "0x" + "c0" * 20
    resolver = DictPoolMeta(
        {
            POOL_A: PoolMeta(TOK, WETH, "uniswap_v3"),
            f"{PM}:{POOL_ID}": PoolMeta(ZERO_ADDRESS, TOK, "uniswap_v4"),
        }
    )
    v4 = {
        "address": PM,
        "logIndex": "0x2",
        "topics": [event_topic(SIG_V4_SWAP), POOL_ID, t(BOT)],
        "data": "0x"
        + encode(
            ["int128", "int128", "uint160", "uint128", "int24", "uint24"],
            [-99 * 10**16, 45_000 * 10**18, 2**96, 10**20, 0, 3000],
        ).hex(),
    }  # pool saw 0.99 ETH
    claim = {
        "address": PM,
        "logIndex": "0x1",
        "topics": [ERC6909_TRANSFER_TOPIC, t(ZERO_ADDRESS), t(HOOK_HOLDING), t(ZERO_ADDRESS)],
        "data": "0x" + encode(["address", "uint256"], [PM, 10**16]).hex(),
    }  # hook keeps 0.01 ETH as a claim
    logs = [
        transfer(WETH, POOL_A, BOT, 1035 * 10**15, 0),
        claim,
        v4,
        transfer(TOK, PM, BOT, 45_000 * 10**18, 3),
        transfer(TOK, BOT, POOL_A, 45_000 * 10**18, 4),
        v3_swap(POOL_A, 45_000 * 10**18, -1035 * 10**15, 5),
        {
            "address": WETH,
            "logIndex": "0x6",
            "topics": [event_topic(SIG_WETH_WITHDRAWAL), t(BOT)],
            "data": "0x" + encode(["uint256"], [10**18]).hex(),
        },  # the swapper actually paid 1.00 ETH
        transfer(WETH, BOT, COLLECTOR, 35 * 10**15, 7),
    ]
    tx, rc = make_tx(logs)
    lab = await classify_tx(tx, rc, resolver, base_fee_per_gas=1, weth=WETH)
    assert lab.is_arb and lab.profit_token == WETH
    assert lab.gross_profit_raw == 35 * 10**15  # 1.035 − 1.00, not 1.035 − 0.99
    assert "v4_hook_claims:1" in lab.reasons


@pytest.mark.asyncio
async def test_liquidity_events_inside_hook_callbacks_do_not_hold_the_cycle():
    """Ethereum 26150007 #441 / 26137938 #441, Base 52340882 #443, BSC 83600063 #2 / 83600097 #1: the hook of the Uniswap
    V4 pool collected its own position's fees inside the swap it served (a ModifyLiquidity with liquidityDelta 0);
    Robinhood 84259655 #4 / 70067057 #13: the hook's rebalancer moved liquidity inside the callback. The cycles had been
    held as lp_action; the engine reads the trace and passes `hook_liquidity`. A collect the operator makes itself (a
    fee harvest sold on the spot: Ethereum 26139831 #180, 26143936 #138) still holds the row."""
    from mevlab.decode.lifecycle import liquidity_change
    from mevlab.decode.signatures import SIG_V3_BURN, SIG_V4_MODIFY

    manager = "0x498581ff718922c3f8e6a244956af099b2652b2b"
    cycle = [
        transfer(WETH, BOT, POOL_A, 10**18, 0),
        v3_swap(POOL_A, 10**18, -3000 * 10**6, 1),
        transfer(USDC, POOL_A, BOT, 3000 * 10**6, 2),
        transfer(USDC, BOT, POOL_B, 3000 * 10**6, 3),
        solidly_swap(POOL_B, 0, 3000 * 10**6, 101 * 10**16, 0, 4),
        transfer(WETH, POOL_B, BOT, 101 * 10**16, 5),
    ]

    def modify(delta):
        return {
            "address": manager,
            "logIndex": "0x9",
            "topics": [event_topic(SIG_V4_MODIFY), "0x" + "ab" * 32, t(BOT)],
            "data": "0x"
            + encode(["int24", "int24", "int256", "bytes32"], [-887220, 887220, delta, b"\0" * 32]).hex(),
        }

    def burn(amount):
        return {
            "address": POOL_B,
            "logIndex": "0x9",
            "topics": [event_topic(SIG_V3_BURN), t(BOT), t("0x0"), t("0x64")],
            "data": "0x" + encode(["uint128", "uint256", "uint256"], [amount, 10**18, 3000 * 10**6]).hex(),
        }

    assert liquidity_change(modify(0)) and liquidity_change(burn(0)) and liquidity_change(modify(10**12))
    for collect in (modify(0), burn(0)):  # the operator's own fee collect still holds the row
        tx, rc = make_tx([*cycle, collect])
        assert (await classify_tx(tx, rc, RESOLVER, base_fee_per_gas=1, weth=WETH)).arb_class == "lp_action"
    tx, rc = make_tx([*cycle, modify(0)])
    assert (await classify_tx(tx, rc, RESOLVER, base_fee_per_gas=1, weth=WETH, hook_liquidity=True)).is_arb
    tx, rc = make_tx([*cycle, modify(10**12)])
    held = await classify_tx(tx, rc, RESOLVER, base_fee_per_gas=1, weth=WETH)
    assert held.arb_class == "lp_action" and "same_tx_liquidity_event" in held.reasons
    lifted = await classify_tx(tx, rc, RESOLVER, base_fee_per_gas=1, weth=WETH, hook_liquidity=True)
    assert lifted.is_arb and "liquidity_changes_inside_hook_callbacks" in lifted.reasons
