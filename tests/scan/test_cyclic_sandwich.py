"""Cyclic sandwich (Ethereum 26317113 #6/#7/#8): a losing cycle in front, a victim, the reversing cycle behind."""

from arbkit.scan.sandwich import V3_SWAP_TOPIC, find_cyclic_sandwiches, find_sandwiches

V2_POOL, V3_POOL = "0x3c442bab170f19dd40d0b1a405c9d93b088b9332", "0xe3fe800b0de664bf0bca8ad58ecbc73b259047b0"
BOT, WALLET = "0xe56f811fb6c50180e512e4465fc1a6b18f4ee756", "0xa98e43a4ec35d0a9612051df351dca70f8d4562b"


def word(v: int) -> str:
    return f"{v % (1 << 256):064x}"


def v3_swap(amount0: int) -> dict:  # amount0 > 0: token0 (POWR) into the pool
    return {
        "address": V3_POOL,
        "topics": [V3_SWAP_TOPIC, "0x" + "0" * 64, "0x" + "0" * 64],
        "data": "0x" + word(amount0) + word(-amount0) + word(2**96) + word(10**18) + word(0),
    }


def row(i, h, frm, to, assessment, venues):
    return {
        "tx_index": i,
        "tx_hash": h,
        "tx_from": frm,
        "tx_to": to,
        "assessment": assessment,
        "venues": venues,
        "block_number": 26317113,
        "fees": {"is_system": False},
        "arb_class": "atomic_cycle",
    }


def block():
    rows = [
        row(6, "0xf", WALLET, BOT, "trace_supported_loss", [V2_POOL, V3_POOL]),
        row(7, "0xv", "0xc24d", "0x364e", "not_arb", [V3_POOL, "0x11b815ef"]),
        row(8, "0xb", WALLET, BOT, "trace_supported", [V3_POOL, V2_POOL]),
    ]
    receipts = {
        "0xf": {"logs": [v3_swap(4709)]},
        "0xv": {"logs": [v3_swap(6901)]},
        "0xb": {"logs": [v3_swap(-4709)]},
    }
    return rows, receipts


def test_cycle_legs_bracketing_a_victim_are_one_sandwich():
    rows, receipts = block()
    assert find_sandwiches(rows, receipts) == []  # cycles are never plain sandwich legs
    [bundle] = find_cyclic_sandwiches(rows, receipts)
    assert (bundle["front"], bundle["back"], bundle["victims"], bundle["pool"]) == (
        "0xf",
        "0xb",
        ["0xv"],
        V3_POOL,
    )
    assert bundle["kind"] == "cyclic" and bundle["operator"] == BOT


def test_no_victim_or_another_operator_is_no_cyclic_sandwich():
    rows, receipts = block()
    receipts["0xv"] = {"logs": [v3_swap(-6901)]}  # the third party traded against the front: not a victim
    assert find_cyclic_sandwiches(rows, receipts) == []
    rows, receipts = block()
    rows[2]["tx_to"] = "0x" + "77" * 20  # the back leg runs through another contract
    assert find_cyclic_sandwiches(rows, receipts) == []
    rows, receipts = block()
    rows[0]["assessment"] = (
        "trace_supported"  # a winning cycle in front is a backrun pair, not a sandwich front
    )
    assert find_cyclic_sandwiches(rows, receipts) == []


def test_a_losing_bot_is_linked_to_the_bot_that_beat_it():
    from arbkit.scan.block import mark_beaten

    powr, weth = "0x595832f8", "0xc02aaa39"

    def with_legs(r, *legs):
        r["trace"] = {"legs": [{"venue": v, "token_in": a, "token_out": b} for v, a, b in legs]}
        return r

    winner = with_legs(
        row(3, "0xw", "0xaaa", "0xbot1", "trace_supported", [V2_POOL, V3_POOL]),
        (V3_POOL, powr, weth),
        (V2_POOL, weth, powr),
    )
    against = with_legs(
        row(4, "0xo", "0xccc", "0xbot3", "trace_supported", [V3_POOL]), (V3_POOL, weth, powr)
    )  # other direction
    loser = with_legs(
        row(5, "0xl", "0xbbb", "0xbot2", "trace_supported_loss", [V3_POOL]),
        (V3_POOL, powr, weth),
        ("0xpool", weth, powr),
    )
    mark_beaten([winner, against, loser])
    assert loser["beaten_by"] == {"tx_hash": "0xw", "tx_index": 3, "operator": "0xbot1"} and winner[
        "beat"
    ] == ["0xl"]
    assert "beat" not in against  # a pool shared in the opposite direction is not the same opportunity
    # the same operator losing then winning is never "beaten" (that pair is a sandwich or its own round trip)
    own = with_legs(
        row(5, "0xl2", "0xaaa", "0xbot1", "trace_supported_loss", [V3_POOL]), (V3_POOL, powr, weth)
    )
    mark_beaten(
        [
            with_legs(row(3, "0xw2", "0xaaa", "0xbot1", "trace_supported", [V3_POOL]), (V3_POOL, powr, weth)),
            own,
        ]
    )
    assert "beaten_by" not in own
