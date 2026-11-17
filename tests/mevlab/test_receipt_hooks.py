"""C-CLS-30 / C-CLS-31 at event level (Robinhood package only): a V4 hook paid by the PoolManager inside the
Swap delta must be netted out of the leg. Fixtures mirror robinscan-verified transactions
0x47c16f94… (Pons hook, native ETH cut announced by its fee event), 0x1146db99… (splitter hook, USDG cut paid
by the PoolManager to two recipients) and 0xce075916… (Pons cut in USDG, both an event and a transfer: net once)."""

from __future__ import annotations

import pytest
from eth_abi import encode

from mevlab.decode.signatures import (
    HOOK_FEE_EVENTS,
    SIG_V3_SWAP,
    SIG_V4_SWAP,
    TRANSFER_TOPIC,
    ZERO_ADDRESS,
    event_topic,
)
from mevlab.pools.meta import DictPoolMeta, PoolMeta
from mevlab.receipt.classify import classify_tx

WETH = "0x4200000000000000000000000000000000000006"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
MEME = "0x16391c40e85fb2246a2c8c17bfa2594c5d3ef84b"
PM = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
V3_WETH_USDG = "0x52e65b17fb6e5ba00e2fa7fe1a7ee3bd6a0271ca"
PONS = "0xe5e702641ea86f4ae6cc3cdaed2b886f976be044"
SPLITTER = "0x3685cc4aa1266813fa0ef346853341b0778c80cc"
BOT = "0xef6869e7329f48370794c152a6c7ec486a81a44f"
EOA = "0xf9ccb80da38e0000000000000000000000000001"
COLLECTOR = "0x33eabd63853e74ff70d8b89982dfb5bc3eb0a189"
FEE_A = "0xf6b78cde1a2b9a0b47d1a2b2360bd9571ea4491a"
FEE_B = "0x38b82fb41d7cc0cc5d3dc00eed2eeba23ce48853"
POOL_MEME_USDG = "0x" + "11" * 32
POOL_ETH_MEME = "0x64c5dbbee60473344dc6f7b11391ff9c7bb7464c0d5ecfb0d311ff26df9f8c77"
POOL_USDG_PRINTER = "0xf6c9f0a8bf94ecda6751465a3097bc4c69914421126d2cbc3df723e36b8cf47b"
PRINTER = "0x85a574f2ff0795685f58d1d7b0d4b51f148ac489"
V3_PRINTER_USDG = "0xd7da7ac01bd67b3785ad6f4a1a2b10ea2f6bb54c"
PONS_TOPIC = next(k for k, v in HOOK_FEE_EVENTS.items() if v == "pons")
SPLIT_TOPIC = next(k for k, v in HOOK_FEE_EVENTS.items() if v == "splitter")


def t(a: str) -> str:
    return "0x" + a[2:].rjust(64, "0")


def w(n: int) -> str:
    return n.to_bytes(32, "big").hex()


def v4_swap(i: int, pool_id: str, sender: str, a0: int, a1: int) -> dict:
    """V4 Swap, user-perspective deltas (negative = user paid)."""
    return {
        "address": PM,
        "logIndex": hex(i),
        "topics": [event_topic(SIG_V4_SWAP), pool_id, t(sender)],
        "data": "0x"
        + encode(
            ["int128", "int128", "uint160", "uint128", "int24", "uint24"], [a0, a1, 2**96, 10**18, 0, 3000]
        ).hex(),
    }


def v3_swap(i: int, pool: str, sender: str, a0: int, a1: int) -> dict:
    """V3 Swap, pool-perspective (positive = pool received)."""
    return {
        "address": pool,
        "logIndex": hex(i),
        "topics": [event_topic(SIG_V3_SWAP), t(sender), t(sender)],
        "data": "0x"
        + encode(["int256", "int256", "uint160", "uint128", "int24"], [a0, a1, 2**96, 10**18, 0]).hex(),
    }


def xfer(i: int, token: str, frm: str, to: str, v: int) -> dict:
    return {
        "address": token,
        "logIndex": hex(i),
        "topics": [TRANSFER_TOPIC, t(frm), t(to)],
        "data": "0x" + w(v),
    }


def run(logs, resolver):
    tx = {
        "hash": "0x" + "ab" * 32,
        "blockNumber": "0x10",
        "transactionIndex": "0x1",
        "from": EOA,
        "to": BOT,
        "value": "0x0",
    }
    rc = {"status": "0x1", "gasUsed": "0x30000", "effectiveGasPrice": "0x1", "logs": logs}
    return classify_tx(tx, rc, resolver, base_fee_per_gas=1, weth=WETH, family="op-stack")


@pytest.mark.asyncio
async def test_pons_native_fee_event_is_netted_out_of_the_v4_leg():
    """0x47c16f94…: the pool gives 2.420324 ETH, the Pons hook keeps 0.024203 + 0.048406 by CALL (no log), the
    bot gets 2.347714 and wraps it; the cycle's gain is 0.161590, not 0.234199."""
    usdg_in, meme, eth_out = 5_366_596_896, 2_181_990_615_971_755_013_016_349, 2_420_324_104_203_283_550
    fee1, fee2 = 24_203_241_042_032_835, 48_406_482_084_065_671
    got = eth_out - fee1 - fee2
    weth_sold = 2_186_124_657_434_725_632
    logs = [
        xfer(98, USDG, V3_WETH_USDG, BOT, usdg_in),
        v4_swap(100, POOL_MEME_USDG, BOT, meme, -usdg_in),  # MEME out, USDG in (user view)
        v4_swap(101, POOL_ETH_MEME, BOT, eth_out, -meme),  # ETH out, MEME in
        {
            "address": PONS,
            "logIndex": hex(102),
            "topics": [PONS_TOPIC, POOL_ETH_MEME],
            "data": "0x" + w(0) + w(fee1) + w(fee2),
        },
        xfer(103, WETH, ZERO_ADDRESS, BOT, got),  # proxy WETH mint = wrap
        xfer(104, USDG, BOT, PM, usdg_in),
        xfer(106, WETH, BOT, V3_WETH_USDG, weth_sold),
        v3_swap(107, V3_WETH_USDG, BOT, weth_sold, -usdg_in),
        xfer(108, WETH, BOT, COLLECTOR, got - weth_sold),
    ]
    resolver = DictPoolMeta(
        {
            f"{PM}:{POOL_MEME_USDG}": PoolMeta(MEME, USDG, "uniswap_v4", hooks=ZERO_ADDRESS),
            f"{PM}:{POOL_ETH_MEME}": PoolMeta(ZERO_ADDRESS, MEME, "uniswap_v4", hooks=PONS),
            V3_WETH_USDG: PoolMeta(WETH, USDG, "uniswap_v3"),
        }
    )
    lab = await run(logs, resolver)
    assert lab.arb_class == "atomic_cycle"
    assert lab.profit_token == WETH
    assert lab.gross_profit_raw == got - weth_sold == 161_589_723_642_459_412
    assert any(r.startswith("v4_hook_fee_event:") for r in lab.reasons)


@pytest.mark.asyncio
async def test_splitter_hook_usdg_cut_paid_by_pool_manager_is_netted_once():
    """0x1146db99…: the pool gives 1,364.421981 USDG; the PoolManager pays 10.915375 and 43.661504 to two fee
    recipients and 1,309.845102 to the bot's helper; the splitter's event says total 54.576879. Gross to the
    collector is 105.455535 USDG, not 160.032414."""
    printer, usdg_out = 186_515_140_797_723_543_390_947, 1_364_421_981
    fa, fb = 10_915_375, 43_661_504
    got = usdg_out - fa - fb
    usdg_paid_v3 = 1_204_389_567
    logs = [
        xfer(118, PRINTER, V3_PRINTER_USDG, BOT, printer),
        v4_swap(120, POOL_USDG_PRINTER, BOT, usdg_out, -printer),  # USDG out (user view), PRINTER in
        xfer(121, USDG, PM, FEE_A, fa),
        xfer(122, USDG, PM, FEE_B, fb),
        {
            "address": SPLITTER,
            "logIndex": hex(123),
            "topics": [SPLIT_TOPIC, t(USDG)],
            "data": "0x" + w(fa + fb) + w(fa) + w(fb) + w(400),
        },
        xfer(124, USDG, PM, BOT, got),
        xfer(125, PRINTER, BOT, PM, printer),
        xfer(126, USDG, BOT, V3_PRINTER_USDG, usdg_paid_v3),
        v3_swap(127, V3_PRINTER_USDG, BOT, usdg_paid_v3, -printer),
        xfer(128, USDG, BOT, COLLECTOR, got - usdg_paid_v3),
    ]
    resolver = DictPoolMeta(
        {
            f"{PM}:{POOL_USDG_PRINTER}": PoolMeta(USDG, PRINTER, "uniswap_v4", hooks=SPLITTER),
            V3_PRINTER_USDG: PoolMeta(USDG, PRINTER, "uniswap_v3"),
        }
    )
    lab = await run(logs, resolver)
    assert lab.arb_class == "atomic_cycle"
    assert lab.profit_token == USDG
    assert lab.gross_profit_raw == got - usdg_paid_v3 == 105_455_535
    # the event netted the cut; the two PM -> recipient transfers must not be netted a second time
    assert sum(1 for r in lab.reasons if r.startswith("v4_")) == 1


@pytest.mark.asyncio
async def test_pm_transfer_to_registered_hook_is_netted_and_a_collector_take_is_not():
    """Without a fee event, an ERC-20 transfer from the PoolManager to the pool's registered hook is a cut; a V4
    take that hands the collector a slice of the output is the bot's own money and must not be netted (inside one
    tx the two are indistinguishable for an unregistered recipient, so only the registered hook counts)."""
    printer, usdg_out, cut = 10**24, 1_000_000_000, 30_000_000
    got = usdg_out - cut
    usdg_paid_v3 = 900_000_000
    base = [
        xfer(1, PRINTER, V3_PRINTER_USDG, BOT, printer),
        v4_swap(2, POOL_USDG_PRINTER, BOT, usdg_out, -printer),
        xfer(3, USDG, PM, FEE_A, cut),
        xfer(4, USDG, PM, BOT, got),
        xfer(5, PRINTER, BOT, PM, printer),
        xfer(6, USDG, BOT, V3_PRINTER_USDG, usdg_paid_v3),
        v3_swap(7, V3_PRINTER_USDG, BOT, usdg_paid_v3, -printer),
        xfer(8, USDG, BOT, COLLECTOR, got - usdg_paid_v3),
    ]
    resolver = DictPoolMeta(
        {
            f"{PM}:{POOL_USDG_PRINTER}": PoolMeta(USDG, PRINTER, "uniswap_v4", hooks=FEE_A),
            V3_PRINTER_USDG: PoolMeta(USDG, PRINTER, "uniswap_v3"),
        }
    )
    lab = await run(base, resolver)
    assert lab.gross_profit_raw == got - usdg_paid_v3 == 70_000_000
    assert any(r.startswith("v4_hook_paid_by_pm:") for r in lab.reasons)

    # same cycle, but the PoolManager hands the entire output straight to the collector (no cut anywhere)
    whole = [
        xfer(1, PRINTER, V3_PRINTER_USDG, BOT, printer),
        v4_swap(2, POOL_USDG_PRINTER, BOT, usdg_out, -printer),
        xfer(3, USDG, PM, COLLECTOR, usdg_out - usdg_paid_v3),
        xfer(4, USDG, PM, BOT, usdg_paid_v3),
        xfer(5, PRINTER, BOT, PM, printer),
        xfer(6, USDG, BOT, V3_PRINTER_USDG, usdg_paid_v3),
        v3_swap(7, V3_PRINTER_USDG, BOT, usdg_paid_v3, -printer),
    ]
    lab = await run(whole, resolver)
    assert lab.gross_profit_raw == usdg_out - usdg_paid_v3 == 100_000_000
    assert not any(r.startswith("v4_") for r in lab.reasons)


@pytest.mark.asyncio
async def test_hooked_native_leg_without_evidence_is_flagged():
    """0x0e7eb75f…: hook 0xdaa90d06… takes 3 % of the ETH output by CALL and emits nothing. The event layer
    cannot see it; the row must carry the flag so the accounting prefers the trace."""
    hook = "0xdaa90d06bec66ea94a5fc53f1d232255f78800c4"
    pool = "0xc0177537169bedeb406346dea1591b46c6de0395caae80d03bab958da5c106d8"
    usdg_in, eth_out, wrapped = 1_930_723_721, 787_733_216_503_953_567, 764_101_000_000_000_000
    weth_sold = 744_147_000_000_000_000
    logs = [
        xfer(1, USDG, V3_WETH_USDG, BOT, usdg_in),
        v4_swap(2, pool, BOT, eth_out, -usdg_in),
        xfer(3, WETH, ZERO_ADDRESS, BOT, wrapped),
        xfer(4, USDG, BOT, PM, usdg_in),
        xfer(5, WETH, BOT, V3_WETH_USDG, weth_sold),
        v3_swap(6, V3_WETH_USDG, BOT, weth_sold, -usdg_in),
    ]
    resolver = DictPoolMeta(
        {
            f"{PM}:{pool}": PoolMeta(ZERO_ADDRESS, USDG, "uniswap_v4", hooks=hook),
            V3_WETH_USDG: PoolMeta(WETH, USDG, "uniswap_v3"),
        }
    )
    lab = await run(logs, resolver)
    assert lab.arb_class == "atomic_cycle"
    assert "hooked_v4_native_unverified:1" in lab.reasons
    assert lab.gross_profit_raw == eth_out - weth_sold  # still the event figure: the trace pass decides
