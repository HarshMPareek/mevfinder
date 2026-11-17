from eth_abi import encode

from mevlab.decode.signatures import (
    SIG_ALGEBRA_SWAP,
    SIG_BALANCER_SWAP,
    SIG_DODO_SWAP,
    SIG_LB_SWAP,
    SIG_V2_SWAP,
    SIG_V3_SWAP,
    SIG_V4_SWAP,
    TOPIC_MM_POOL_SWAP,
    TOPIC_PAIR_CD8B_SWAP,
    TRANSFER_TOPIC,
    event_topic,
    selector,
)
from mevlab.decode.swaps import decode_swap_log
from mevlab.decode.transfers import decode_transfer_log

# Widely published topic hashes; computing them from the signature must reproduce them exactly.
KNOWN = {
    SIG_V2_SWAP: "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822",
    SIG_V3_SWAP: "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67",
    # [chain] topic0 seen on Base logs (Algebra Integral pools of factory 0x36077d39…: 0x121cb44e…;
    # DODO V2 pool 0xdc120fce…: 0xc2c0245e…); the full hashes follow from the signatures.
    SIG_ALGEBRA_SWAP: "0x121cb44ee54098b1a04743c487e7460d8dd429b27f88b1f4d4767396e1a59f79",
    SIG_DODO_SWAP: "0xc2c0245e056d5fb095f04cd6373bc770802ebd1e6c918eb78fdef843cdb37b0f",
    # [chain] Liquidity Book pools on Base (factory 0x217da3e5…) emit topic0 0xc528cda9…
    SIG_LB_SWAP: "0xc528cda9e500228b16ce84fadae290d9a49aecb17483110004c5af0a07f6fd73",  # matched 2026-09-06
    "Transfer(address,address,uint256)": "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
}


def test_known_topics():
    for sig, topic in KNOWN.items():
        assert event_topic(sig) == topic
    assert TRANSFER_TOPIC == KNOWN["Transfer(address,address,uint256)"]


def test_selectors():
    assert selector("transfer(address,uint256)") == "0xa9059cbb"
    assert selector("balanceOf(address)") == "0x70a08231"


def _topic_addr(a: str) -> str:
    return "0x" + a[2:].lower().rjust(64, "0")


def test_decode_v3_swap_pool_perspective():
    data = (
        "0x"
        + encode(
            ["int256", "int256", "uint160", "uint128", "int24"],
            [10**18, -3000 * 10**6, 2**96, 10**20, -200000],
        ).hex()
    )
    log = {
        "address": "0xAbC0000000000000000000000000000000000001",
        "logIndex": "0x5",
        "topics": [event_topic(SIG_V3_SWAP), _topic_addr("0x" + "11" * 20), _topic_addr("0x" + "22" * 20)],
        "data": data,
    }
    leg = decode_swap_log(log)
    assert leg and leg.kind == "clmm_v3"
    assert leg.amount0 == 10**18 and leg.amount1 == -3000 * 10**6
    leg.resolve_tokens("0xweth", "0xusdc")
    assert leg.token_in == "0xweth" and leg.amount_in == 10**18
    assert leg.token_out == "0xusdc" and leg.amount_out == 3000 * 10**6
    assert leg.zero_for_one is True


def test_decode_v4_swap_negates_user_perspective():
    data = (
        "0x"
        + encode(
            ["int128", "int128", "uint160", "uint128", "int24", "uint24"],
            [-(10**18), 2999 * 10**6, 2**96, 10**20, -200000, 500],
        ).hex()
    )
    log = {
        "address": "0x498581fF718922c3f8e6A244956aF099B2652b2b",
        "logIndex": "0x1",
        "topics": [event_topic(SIG_V4_SWAP), "0x" + "ab" * 32, _topic_addr("0x" + "33" * 20)],
        "data": data,
    }
    leg = decode_swap_log(log)
    assert leg and leg.kind == "clmm_v4_singleton"
    assert leg.pool_id == "0x" + "ab" * 32
    assert leg.amount0 == 10**18 and leg.amount1 == -2999 * 10**6  # pool received token0


def test_decode_v2_and_balancer():
    data = "0x" + encode(["uint256"] * 4, [0, 5 * 10**6, 10**15, 0]).hex()
    log = {
        "address": "0x" + "44" * 20,
        "logIndex": "0x2",
        "topics": [event_topic(SIG_V2_SWAP), _topic_addr("0x" + "11" * 20), _topic_addr("0x" + "22" * 20)],
        "data": data,
    }
    leg = decode_swap_log(log)
    assert leg and leg.amount0 == -(10**15) and leg.amount1 == 5 * 10**6

    data = "0x" + encode(["uint256", "uint256"], [7, 9]).hex()
    log = {
        "address": "0xBA12222222228d8Ba445958a75a0704d566BF2C8",
        "logIndex": "0x3",
        "topics": [
            event_topic(SIG_BALANCER_SWAP),
            "0x" + "cd" * 32,
            _topic_addr("0x" + "aa" * 20),
            _topic_addr("0x" + "bb" * 20),
        ],
        "data": data,
    }
    leg = decode_swap_log(log)
    assert leg and leg.kind == "vault" and leg.token_in == "0x" + "aa" * 20
    assert leg.amount_in == 7 and leg.amount_out == 9


def test_transfer_skips_erc721():
    data = "0x" + encode(["uint256"], [5]).hex()
    log20 = {
        "address": "0x" + "55" * 20,
        "logIndex": "0x0",
        "topics": [TRANSFER_TOPIC, _topic_addr("0x" + "11" * 20), _topic_addr("0x" + "22" * 20)],
        "data": data,
    }
    log721 = dict(log20, topics=log20["topics"] + ["0x" + "00" * 32], data="0x")
    assert decode_transfer_log(log20).value == 5
    assert decode_transfer_log(log721) is None


def test_unknown_topic_returns_none():
    assert decode_swap_log({"address": "0x" + "00" * 20, "topics": ["0x" + "ff" * 32], "data": "0x"}) is None


def test_decode_algebra_swap_like_v3_with_fee_fields():
    data = (
        "0x"
        + encode(
            ["int256", "int256", "uint160", "uint128", "int24", "uint24", "uint24"],
            [-2 * 10**18, 5000 * 10**6, 2**96, 10**20, 100, 3000, 100],
        ).hex()
    )
    log = {
        "address": "0x0ba69825c4c033e72309f6ac0bde0023b15cc97c",
        "logIndex": "0x7",
        "topics": [
            event_topic(SIG_ALGEBRA_SWAP),
            _topic_addr("0x" + "11" * 20),
            _topic_addr("0x" + "22" * 20),
        ],
        "data": data,
    }
    leg = decode_swap_log(log)
    assert leg and leg.kind == "clmm_algebra"
    assert leg.amount0 == -2 * 10**18 and leg.amount1 == 5000 * 10**6
    leg.resolve_tokens("0xweth", "0xusdc")
    assert leg.token_in == "0xusdc" and leg.amount_in == 5000 * 10**6
    assert leg.token_out == "0xweth" and leg.amount_out == 2 * 10**18
    assert leg.zero_for_one is False


def test_decode_dodo_swap_sets_tokens_pool_perspective():
    data = (
        "0x"
        + encode(
            ["address", "address", "uint256", "uint256", "address", "address"],
            ["0x" + "aa" * 20, "0x" + "bb" * 20, 10**18, 2500 * 10**6, "0x" + "cc" * 20, "0x" + "dd" * 20],
        ).hex()
    )
    log = {
        "address": "0xdc120fce000000000000000000000000000000000",
        "logIndex": "0x9",
        "topics": [event_topic(SIG_DODO_SWAP)],
        "data": data,
    }
    leg = decode_swap_log(log)
    assert leg and leg.kind == "pmm_dodo"
    assert leg.token_in == "0x" + "aa" * 20 and leg.amount_in == 10**18
    assert leg.token_out == "0x" + "bb" * 20 and leg.amount_out == 2500 * 10**6
    assert leg.sender == "0x" + "cc" * 20 and leg.recipient == "0x" + "dd" * 20
    assert leg.amount0 is None  # no token0/token1 order on a PMM leg; tokens come from the event


def test_decode_pair_cd8b_and_infer_tokens_from_pool_transfers():
    from mevlab.decode.model import Transfer
    from mevlab.decode.swaps import infer_tokens_from_transfers

    pool = "0x11d5a5e1633c3a99fd023b8fd2d3486773c50993"
    weth, usdc, bot = (
        "0x4200000000000000000000000000000000000006",
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
        "0x" + "f3" * 20,
    )
    data = (
        "0x"
        + encode(
            ["address", "address", "uint256", "int256", "int256", "int256", "uint256"],
            [bot, bot, 1, 188_000_000_000_000_000, -461_809_049, 0, 12149899774919683489639524716118],
        ).hex()
    )
    leg = decode_swap_log(
        {"address": pool, "logIndex": "0x8", "topics": [TOPIC_PAIR_CD8B_SWAP], "data": data}
    )
    assert leg and leg.kind == "clmm_pair_cd8b" and leg.amount0 > 0 and leg.amount1 < 0
    transfers = [
        Transfer(weth, bot, pool, 188_000_000_000_000_000, 7),
        Transfer(usdc, pool, bot, 461_809_049, 9),
        Transfer(usdc, bot, "0x" + "aa" * 20, 461_809_049, 10),
    ]  # a same-size transfer elsewhere: ignored
    assert infer_tokens_from_transfers(leg, transfers)
    assert leg.token_in == weth and leg.amount_in == 188_000_000_000_000_000
    assert leg.token_out == usdc and leg.amount_out == 461_809_049
    # ambiguous (two tokens sent in the same amount) -> not inferred
    leg2 = decode_swap_log(
        {"address": pool, "logIndex": "0x8", "topics": [TOPIC_PAIR_CD8B_SWAP], "data": data}
    )
    assert not infer_tokens_from_transfers(
        leg2, transfers + [Transfer("0x" + "bb" * 20, pool, bot, 461_809_049, 11)]
    )


def test_decode_mm_pool_swap_infers_token_out():
    from mevlab.decode.model import Transfer
    from mevlab.decode.swaps import infer_tokens_from_transfers

    pool = "0xcde89660ef8b3f62d0fdd150eb02dbd7f30162ea"
    usdt, weth, bot = (
        "0xfde4c96c8593536e31f229ea8f37b2ada2699bb2",
        "0x4200000000000000000000000000000000000006",
        "0x" + "f3" * 20,
    )
    data = (
        "0x"
        + encode(
            ["address", "uint256", "uint256", "uint256", "address"],
            [usdt, 462_160_560, 188_762_000_000_000_000, 1, bot],
        ).hex()
    )
    leg = decode_swap_log({"address": pool, "logIndex": "0x3", "topics": [TOPIC_MM_POOL_SWAP], "data": data})
    assert leg and leg.kind == "mm_pool_fcfa" and leg.token_in == usdt and leg.amount_in == 462_160_560
    assert leg.token_out is None and leg.amount_out == 188_762_000_000_000_000 and leg.recipient == bot
    assert infer_tokens_from_transfers(
        leg,
        [Transfer(usdt, bot, pool, 462_160_560, 1), Transfer(weth, pool, bot, 188_762_000_000_000_000, 4)],
    )
    assert leg.token_out == weth


def test_liquidity_book_bins_are_summed_and_tokens_inferred():
    from mevlab.decode.model import Transfer
    from mevlab.decode.swaps import infer_tokens_from_transfers, merge_bin_legs

    pool = "0xbdf175a75c8bbbf140461248d8080156879d16a0"
    hype, weth, bot = "0x" + "ab" * 20, "0x4200000000000000000000000000000000000006", "0x" + "f3" * 20

    def lb_log(li, xin, fee, yout):  # swapForY: the pool receives X (amountIn + fees) and sends Y
        data = (
            "0x"
            + encode(["bool", "uint256", "uint256", "uint256", "uint256"], [True, xin, yout, 0, fee]).hex()
        )
        return {
            "address": pool,
            "logIndex": hex(li),
            "topics": [
                event_topic(SIG_LB_SWAP),
                _topic_addr(bot),
                _topic_addr(bot),
                "0x" + "7ff2c5".rjust(64, "0"),
            ],
            "data": data,
        }

    legs = [
        decode_swap_log(lb_log(3, 10**18 - 10**15, 10**15, 4 * 10**15)),
        decode_swap_log(lb_log(4, 2 * 10**18, 0, 7 * 10**15)),
    ]
    assert all(lg and lg.kind == "liquidity_book" for lg in legs)
    merged = merge_bin_legs(legs)
    assert len(merged) == 1 and merged[0].amount0 == 3 * 10**18 and merged[0].amount1 == -11 * 10**15
    ok = infer_tokens_from_transfers(
        merged[0], [Transfer(hype, bot, pool, 3 * 10**18, 2), Transfer(weth, pool, bot, 11 * 10**15, 5)]
    )
    assert ok and merged[0].token_in == hype and merged[0].amount_in == 3 * 10**18
    assert merged[0].token_out == weth and merged[0].amount_out == 11 * 10**15


def test_weth_mint_burn_is_a_conversion_not_a_gain():
    """Robinhood Chain WETH emits Transfer(0x0 -> depositor) on wrap; the classifier must model it as ETH -> WETH."""
    from mevlab.decode.model import Transfer
    from mevlab.decode.signatures import NATIVE_ETH, ZERO_ADDRESS
    from mevlab.decode.transfers import weth_mint_burn

    weth = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
    bot = "0xd4074624d81a0bf35675230d2fcee1bdf6ef26da"
    mint = Transfer(weth, ZERO_ADDRESS, bot, 3009038521811650995, 29)
    out = weth_mint_burn(mint, weth)
    assert [(t.token, t.sender, t.receiver, t.value) for t in out] == [
        (weth, weth, bot, 3009038521811650995),
        (NATIVE_ETH, bot, weth, 3009038521811650995),
    ]
    burn = Transfer(weth, bot, ZERO_ADDRESS, 5, 30)
    out = weth_mint_burn(burn, weth)
    assert [(t.token, t.sender, t.receiver) for t in out] == [(weth, bot, weth), (NATIVE_ETH, weth, bot)]
    plain = Transfer(weth, bot, "0x52e65b17fb6e5ba00ed806f37afcd2daa50271ca", 5, 31)
    assert weth_mint_burn(plain, weth) == []
    other = Transfer("0x5fc5360d0400a0fd4f2af552add042d716f1d168", ZERO_ADDRESS, bot, 5, 32)
    assert weth_mint_burn(other, weth) == []
