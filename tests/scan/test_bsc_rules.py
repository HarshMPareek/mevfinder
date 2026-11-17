"""BSC fixes of 2026-11-08: holder-linked cyclic sandwiches (0x070f82ae), builder-address tips in sandwich legs,
PancakeSwap Infinity swap directions, inventory routes paired with their predecessor (0x976aed19), original-trigger
linking and pool creation as a trigger."""

from arbkit.scan.sandwich import (
    INFINITY_CL_SWAP_TOPIC,
    V3_SWAP_TOPIC,
    find_cyclic_sandwiches,
    holder_paid,
    operator_result,
    swap_direction,
)
from mevlab.decode.signatures import TRANSFER_TOPIC

POOL_A, POOL_B = "0x" + "3d" * 20, "0x" + "4a" * 20  # victim's pool, second pool of the route
BOT, FRONT_C = "0x" + "07" * 20, "0x" + "20" * 20  # holder (back's contract), front's contract
W_FRONT, W_BACK = "0x" + "71" * 20, "0x" + "d2" * 20  # two wallets
WBNB, USD1 = "0x" + "bb" * 20, "0x" + "8d" * 20
BUILDER = "0x" + "48" * 20


def word(v: int) -> str:
    return f"{v % (1 << 256):064x}"


def topic(a: str) -> str:
    return "0x" + "0" * 24 + a[2:]


def transfer(token, frm, to, value):
    return {"address": token, "topics": [TRANSFER_TOPIC, topic(frm), topic(to)], "data": "0x" + word(value)}


def v3_swap(pool, amount0):  # amount0 > 0: token0 into the pool
    return {
        "address": pool,
        "topics": [V3_SWAP_TOPIC, "0x" + "0" * 64, "0x" + "0" * 64],
        "data": "0x" + word(amount0) + word(-amount0) + word(2**96) + word(10**18) + word(0),
    }


def row(i, h, frm, to, assessment, venues, arb_class="atomic_cycle", reasons=()):
    return {
        "tx_index": i,
        "tx_hash": h,
        "tx_from": frm,
        "tx_to": to,
        "assessment": assessment,
        "venues": venues,
        "block_number": 131994371,
        "fees": {"is_system": False},
        "arb_class": arb_class,
        "reasons": list(reasons),
    }


def block(front_logs=None):
    """131994371 #11-#13 in miniature: the front (another wallet, another contract) spends the bot's WBNB through
    POOL_B -> POOL_A, a victim swaps POOL_A the same way, the back (bot) reverses for a little more WBNB."""
    rows = [
        row(
            11,
            "0xf",
            W_FRONT,
            FRONT_C,
            "not_arb",
            [POOL_A, POOL_B],
            "attempted_cycle",
            ["cycle_lost", "no_beneficiary"],
        ),
        row(12, "0xv", "0x" + "e6" * 20, "0x" + "02" * 20, "not_arb", [POOL_A], "split_route"),
        row(13, "0xb", W_BACK, BOT, "trace_supported", [POOL_B, POOL_A]),
    ]
    front = (
        front_logs
        if front_logs is not None
        else [
            transfer(USD1, POOL_B, BOT, 5742),
            transfer(WBNB, POOL_A, BOT, 7778),
            transfer(USD1, BOT, POOL_A, 5742),
            transfer(WBNB, BOT, POOL_B, 8256),
            v3_swap(POOL_A, 5742),
        ]
    )
    receipts = {
        "0xf": {"logs": front},
        "0xv": {"logs": [v3_swap(POOL_A, 32)]},
        "0xb": {
            "logs": [
                transfer(USD1, POOL_A, BOT, 5734),
                transfer(WBNB, POOL_B, BOT, 8236),
                transfer(USD1, BOT, POOL_B, 5734),
                transfer(WBNB, BOT, POOL_A, 7750),
                v3_swap(POOL_A, -5734),
            ]
        },
    }
    return rows, receipts


def test_a_front_that_spends_the_bots_tokens_links_two_wallets_into_one_sandwich():
    rows, receipts = block()
    [bundle] = find_cyclic_sandwiches(rows, receipts)
    assert (bundle["front"], bundle["back"], bundle["victims"], bundle["pool"]) == (
        "0xf",
        "0xb",
        ["0xv"],
        POOL_A,
    )
    assert bundle["link"] == "holder" and bundle["operator"] == BOT
    assert set(bundle["party_extra"]) == {W_FRONT, FRONT_C, W_BACK}
    result = operator_result(bundle, receipts, WBNB, None, W_FRONT)
    assert result["net_raw"] == {
        WBNB: str(8236 - 7750 - 8256 + 7778)
    }  # the pair's true result: +8 WBNB units


def test_no_link_when_the_bot_did_not_pay_in_the_front():
    # the bot gained in the front (it was paid, not paying): a user trading against it, not its own leg
    rows, receipts = block([transfer(WBNB, POOL_A, BOT, 7778), v3_swap(POOL_A, 5742)])
    assert find_cyclic_sandwiches(rows, receipts) == []
    # the bot's tokens never touched a pool of the back's route
    rows, receipts = block([transfer(WBNB, BOT, "0x" + "99" * 20, 10), v3_swap(POOL_A, 5742)])
    assert find_cyclic_sandwiches(rows, receipts) == []
    # a counted cycle in front is a winner, not a sandwich front
    rows, receipts = block()
    rows[0]["assessment"] = "trace_supported"
    assert find_cyclic_sandwiches(rows, receipts) == []


def test_rounding_dust_and_native_payments_in_the_front():
    logs = [
        transfer(USD1, POOL_B, BOT, 5743),
        transfer(USD1, BOT, POOL_A, 5742),
        transfer(WBNB, BOT, POOL_B, 10),
    ]
    assert holder_paid(logs, BOT, {POOL_A, POOL_B})  # +1 unit of USD1 is dust next to its volume
    # BNB paid into a V4-style pool shows only in the trace: the WBNB proceeds alone would read as a pure gain
    logs = [
        transfer("0x" + "55" * 20, POOL_A, BOT, 76),
        transfer(WBNB, POOL_B, BOT, 105),
        transfer("0x" + "55" * 20, BOT, POOL_B, 76),
    ]
    assert not holder_paid(logs, BOT, {POOL_A, POOL_B})
    trace = {
        "type": "CALL",
        "from": W_FRONT,
        "to": FRONT_C,
        "value": "0x0",
        "calls": [{"type": "CALL", "from": BOT, "to": POOL_A, "value": hex(110)}],
    }
    assert holder_paid(logs, BOT, {POOL_A, POOL_B}, trace, WBNB)


def test_builder_address_payments_are_the_tip_not_part_of_the_result():
    rows, receipts = block()
    [bundle] = find_cyclic_sandwiches(rows, receipts)
    receipts["0xb"]["logs"].append(transfer(WBNB, BOT, BUILDER, 3))
    blind = operator_result(bundle, receipts, WBNB, None, W_FRONT)
    known = operator_result(bundle, receipts, WBNB, None, W_FRONT, builders={BUILDER})
    assert int(blind["net_raw"][WBNB]) == 8 - 3 and blind["paid_to_producer_raw"] == "0"
    assert int(known["net_raw"][WBNB]) == 8 and known["paid_to_producer_raw"] == "3"


def test_infinity_cl_swaps_have_a_direction():
    data = "0x" + word(-100) + word(95) + word(2**96) + word(10**18) + word(0) + word(100) + word(0)
    assert swap_direction(INFINITY_CL_SWAP_TOPIC, data) == 1  # the user paid token0: token0 into the pool
    assert swap_direction(INFINITY_CL_SWAP_TOPIC, "0x" + word(100) + word(-95) + word(0) * 5) == -1


def _inventory_row(pred_net):
    return {
        "mev_type": "inventory_arbitrage",
        "reasons": [],
        "inventory_arb": {
            "gain_usd": 248.37,
            "paid_usd": 912.0,
            "received_usd": 1160.37,
            "stable_pair": False,
            "deltas": {},
        },
        "paired_predecessor": {
            "tx_hash": "0x87",
            "tx_index": 87,
            "link": "same_gas_price",
            "party_net_raw": pred_net,
        },
    }


def test_an_inventory_gain_that_is_its_predecessors_loss_is_no_arbitrage():
    from arbkit.chain.spec import load_chain
    from arbkit.value.pricing import PriceBook, pair_with_predecessor

    spec = load_chain("bsc")
    book = PriceBook(spec.directory / "validation", offline=True)
    usdt = next(t.address for t in spec.tokens if t.symbol == "USDT").lower()
    busd = next(t.address for t in spec.tokens if t.symbol == "BUSD").lower()
    pair = _inventory_row({usdt: str(-919 * 10**18), busd: str(662 * 10**18)})  # predecessor lost $257
    pair_with_predecessor(spec, book, pair, 1_762_400_000)
    assert pair["mev_type"] == "none" and "inventory_arb" not in pair
    assert round(pair["inventory_arb_rejected"]["joint_usd"], 2) == round(248.37 - 257, 2)
    small = _inventory_row({usdt: str(-50 * 10**18), busd: str(40 * 10**18)})  # lost $10: unrelated
    pair_with_predecessor(spec, book, small, 1_762_400_000)
    assert small["mev_type"] == "inventory_arbitrage" and "inventory_arb_rejected" not in small
    unpriced = _inventory_row({"0x" + "ab" * 20: str(-(10**18))})  # no value judgement on an unpriced token
    pair_with_predecessor(spec, book, unpriced, 1_762_400_000)
    assert unpriced["mev_type"] == "inventory_arbitrage"


def _pos_row(i, frm, to, assessment, moves, **extra):
    return {
        "tx_index": i,
        "tx_hash": f"0x{i:02x}",
        "tx_from": frm,
        "tx_to": to,
        "assessment": assessment,
        "venues": list(moves),
        "price_moves": dict(moves),
        "fees": {"is_system": False},
        "reasons": [],
        "trigger_type": "mid_block",
        **extra,
    }


def test_original_trigger_passes_siblings_and_competitors():
    from arbkit.scan.positions import link_original_triggers

    user = _pos_row(1, "0xuser", "0xrouter", "not_arb", {POOL_A: 1})
    rival = _pos_row(2, "0xrival", "0xrivalbot", "trace_supported", {POOL_A: -1}, trigger_type="backrun")
    sibling = _pos_row(3, "0xme", "0xbot", "not_arb", {POOL_A: -1})
    me = _pos_row(4, "0xme2", "0xbot", "trace_supported", {POOL_A: -1, POOL_B: 1})
    txs = {r["tx_index"]: {"hash": r["tx_hash"]} for r in (user, rival, sibling, me)}
    link_original_triggers([user, rival, sibling, me], txs, 100)
    assert (
        me["trigger_type"] == "backrun"
        and me["trigger_tx_index"] == 1
        and me["trigger_price_reversal"] is True
    )
    assert "original_trigger_linked" in me["reasons"]
    # a non-arb transaction that moved the pool the same way stops the search: the move it made is not ours to reverse
    same_way = _pos_row(2, "0xother", "0xrouter2", "not_arb", {POOL_A: -1})
    me2 = _pos_row(4, "0xme2", "0xbot", "trace_supported", {POOL_A: -1})
    link_original_triggers([user, same_way, me2], {i: {"hash": f"0x{i:02x}"} for i in (1, 2, 4)}, 100)
    assert me2["trigger_type"] == "mid_block"


def test_a_pools_first_mint_is_its_creation():
    from arbkit.scan.liquidity import TRANSFER, created_pools
    from arbkit.scan.positions import created_pools as touched_by_creation

    zero = "0x" + "0" * 64
    logs = [
        {"address": POOL_A, "topics": [TRANSFER, zero, zero], "data": "0x" + word(1000)},
        {"address": POOL_A, "topics": [TRANSFER, zero, topic(W_FRONT)], "data": "0x" + word(79 * 10**18)},
    ]
    assert created_pools(logs) == [POOL_A]
    assert created_pools(logs[1:]) == []  # a later add mints to the provider only
    rows = [{"tx_index": 1, "venues": [], "created_pools": [POOL_A]}, {"tx_index": 2, "venues": [POOL_A]}]
    assert touched_by_creation(rows) == {1: [POOL_A]}
