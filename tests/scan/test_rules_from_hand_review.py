"""Rules from the 2026-10-09 hand review: operator-side payers are not venues, bundler fees are not gains, unregistered
tokens through unresolved venues stay under review, CEX-DEX candidates on FCFS chains must persist and be alone."""

import asyncio
from dataclasses import replace

from arbkit.chain.spec import load_chain
from arbkit.scan.trace import apply_trace
from arbkit.value.pricing import PriceBook, cex_dex_candidate, value_rows
from mevlab.trace.verdict import trace_verdict
from tests.mevlab.test_trace import BOT, EOA, POOL_A, POOL_B, call, transfer, transfer_from

BUILDER = "0x4848489f0b2bedd788c696e2d79b6b69d7484848"
VAULT = "0x60b9770900000000000000000000000000000060"
FORWARDER = "0x000b102d00000000000000000000000000000000"
TOKEN = "0xa0c56a8c0000000000000000000000000000000a"
OTHER = "0x5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e"
BSC = load_chain("bsc")
BASE = load_chain("base")
ARB = load_chain("arbitrum")
USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
USDC_BSC = "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d"
ROUTER_PCS = "0x10ed43c718714eb63d5aa57b78b54704e256024e"


def vault_buy_with_tip(weth):
    """BSC 126512303 #14: the vault funds a buy on one pool, keeps the token and tips the builder in native coin."""
    usdc = "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d"
    return call(
        EOA,
        FORWARDER,
        calls=[
            call(
                FORWARDER,
                VAULT,
                calls=[
                    call(VAULT, usdc, transfer(POOL_A, 384_862_292_092)),
                    call(VAULT, POOL_A, calls=[call(POOL_A, TOKEN, transfer(VAULT, 130_900_000_000))]),
                    call(VAULT, BUILDER, "0x", value=3_589_557_839_666),
                ],
            )
        ],
    )


def test_a_contract_that_pays_the_builder_is_operator_side_not_a_venue():
    weth = BSC.weth
    before = trace_verdict(vault_buy_with_tip(weth), weth, EOA, FORWARDER)
    assert (
        before.verdict == "cycle" and VAULT in before.venues
    )  # the old reading: vault as second venue, tip as gain
    after = trace_verdict(vault_buy_with_tip(weth), weth, EOA, FORWARDER, known_recipients={BUILDER})
    assert VAULT not in after.venues and after.verdict != "cycle" and after.gross is None
    # BSC 126500003 #2: the executor hands the forwarder (tx_to) the exact tip the forwarder passes to the builder
    usdt = "0x55d398326f99059ff775485246999027b3197955"
    via_forwarder = call(
        EOA,
        FORWARDER,
        calls=[
            call(
                FORWARDER,
                VAULT,
                calls=[
                    call(VAULT, TOKEN, transfer(POOL_A, 62_750_000_000)),
                    call(VAULT, POOL_A, calls=[call(POOL_A, usdt, transfer(VAULT, 319_326_984_891))]),
                    call(VAULT, FORWARDER, "0x", value=62_152_000_000_000),
                ],
            ),
            call(FORWARDER, BUILDER, "0x", value=62_152_000_000_000),
        ],
    )
    assert trace_verdict(via_forwarder, weth, EOA, FORWARDER).verdict == "cycle"  # the old reading
    fixed = trace_verdict(via_forwarder, weth, EOA, FORWARDER, known_recipients={BUILDER})
    assert VAULT not in fixed.venues and fixed.verdict != "cycle"


def cycle_trace(weth, token_a, token_b, gain, fee_to=None, fee=0):
    calls = [
        call(BOT, token_a, transfer(POOL_A, 10**18)),
        call(BOT, POOL_A, calls=[call(POOL_A, token_b, transfer(BOT, 3000 * 10**6))]),
        call(BOT, token_b, transfer(POOL_B, 3000 * 10**6)),
        call(BOT, POOL_B, calls=[call(POOL_B, token_a, transfer(BOT, 10**18 + gain))]),
    ]
    if fee_to:
        calls.append(call(BOT, fee_to, "0x", value=fee))
    return call(EOA, BOT, calls=calls)


def row_template():
    return {
        "tx_hash": "0x" + "ab" * 32,
        "arb_class": "single_swap",
        "is_arb": False,
        "assessment": "not_arb",
        "reasons": [],
        "n_legs": 1,
        "venues": [],
        "kinds": [],
        "tokens": [],
        "event_label": {"venues": []},
    }


def run(spec, trace):
    row = row_template()
    tx = {"from": EOA, "to": BOT, "hash": row["tx_hash"]}
    apply_trace(
        spec, row, trace, tx, {"status": "0x1"}
    )  # no receipt logs: amounts come from the calldata, as in test_tracelegs
    return row


def test_a_payment_to_a_declared_bundler_fee_collector_makes_the_row_a_bundled_operation():
    collector = next(iter(BSC.recipients_of_kind("bundler_fee")))
    usdt = "0x55d398326f99059ff775485246999027b3197955"
    plain = run(BSC, cycle_trace(BSC.weth, BSC.weth, usdt, 10**15))
    assert plain["assessment"] == "trace_supported" and plain["arb_class"] == "atomic_cycle"
    bundled = run(BSC, cycle_trace(BSC.weth, BSC.weth, usdt, 10**15, fee_to=collector, fee=2 * 10**14))
    assert (
        bundled["arb_class"] == "bundled_operation"
        and bundled["assessment"] == "not_arb"
        and not bundled["is_arb"]
    )
    assert any(r.startswith("bundler_fee_paid:") for r in bundled["reasons"])


def test_an_unregistered_profit_token_through_unresolved_venues_stays_under_review():
    counted = run(BASE, cycle_trace(BASE.weth, BASE.weth, USDC_BASE, 10**15))
    assert counted["assessment"] == "trace_supported"
    suspect = run(BASE, cycle_trace(BASE.weth, TOKEN, OTHER, 10**15))
    assert suspect["assessment"] == "needs_review" and not suspect["is_arb"]
    assert "unresolved_venues_unregistered_profit_token" in suspect["reasons"]
    # the same unregistered token with a decoded venue kind is still a counted cycle (the registry resolved the route)
    row = row_template()
    row["kinds"] = ["clmm_v3"]
    apply_trace(
        BASE, row, cycle_trace(BASE.weth, TOKEN, OTHER, 10**15), {"from": EOA, "to": BOT}, {"status": "0x1"}
    )
    assert row["assessment"] == "trace_supported"


def test_cex_dex_on_a_fcfs_chain_needs_a_persisting_edge_and_few_wallets(tmp_path):
    usdc = "0xaf88d065e77c8cc2239327c5edb3a432268e5831"
    fcfs = replace(ARB, ordering_policy="fcfs")
    book = PriceBook(tmp_path, offline=True)
    ts = 1_700_000_000
    book.set("ETHUSDT", ts, 3000.0)
    flow = {
        "token_in": usdc,
        "amount_in_raw": str(30_000 * 10**6),
        "token_out": fcfs.weth,
        "amount_out_raw": str(10_030 * 10**15),
        "venues": [POOL_A],
        "dexes": ["uniswap_v3"],
    }
    base = {"swap_flow": flow, "user_index_in_block": 0, "first_in_slot": False, "assessment": "not_arb"}
    # Arbitrum One runs priority gas auctions since 2026-09-24 (chain.toml: fee_sorted); the first-come behaviour under test
    # is that of its pre-PGA blocks and of Robinhood Chain, so the spec is read with the fcfs policy here.
    assert (
        cex_dex_candidate(fcfs, book, dict(base), ts) is None
    )  # no next-minute price: cannot show persistence
    book.set("ETHUSDT", ts + 60, 2985.0)  # the CEX caught up with the DEX: the DEX led, nobody was arbitraged
    assert cex_dex_candidate(fcfs, book, dict(base), ts) is None
    book.set("ETHUSDT", ts + 60, 3005.0)
    assert cex_dex_candidate(fcfs, book, dict(base), ts)["edge_bps"] == 30.0

    # three wallets with the same edge in one block: a stale minute, every candidate of the block is dropped
    def row(i):
        return {
            **base,
            "block_number": 7,
            "tx_from": "0x" + f"{i:040x}",
            "fees": {"total_fee_wei": 10**15},
            "builder_tip_wei": None,
            "kickback_wei": None,
            "mev_type": "none",
        }

    rows = [row(1), row(2)]
    asyncio.run(value_rows(fcfs, rows, {7: ts}, book))
    assert all(r["mev_type"] == "cex_dex_candidate" for r in rows)
    rows = [row(1), row(2), row(3)]
    asyncio.run(value_rows(fcfs, rows, {7: ts}, book))
    assert all(
        r["mev_type"] == "none" and r["cex_dex"] is None and r["cex_dex_dropped"].startswith("shared_edge:3")
        for r in rows
    )


def test_a_borrow_event_makes_the_row_a_debt_operation():
    from arbkit.scan.trace import DEBT_EVENT_TOPICS

    morpho_borrow = next(t for t, n in DEBT_EVENT_TOPICS.items() if n == "morpho_blue_borrow")
    row = row_template()
    trace = cycle_trace(BASE.weth, BASE.weth, USDC_BASE, 10**15)
    logs = [
        {
            "address": "0xbbbbbbbbbb9cc5e90e3b3af64bdaf62c37eeffcb",
            "topics": [
                morpho_borrow,
                "0x" + "01" * 32,
                "0x" + "00" * 12 + BOT[2:],
                "0x" + "00" * 12 + BOT[2:],
            ],
            "data": "0x" + "00" * 64,
        }
    ]
    apply_trace(BASE, row, trace, {"from": EOA, "to": BOT}, {"status": "0x1", "logs": logs})
    assert row["arb_class"] == "debt_operation" and row["assessment"] == "not_arb" and not row["is_arb"]
    assert "debt_inflow:morpho_blue_borrow" in row["reasons"]


def test_a_declared_two_contract_venue_closes_the_cycle():
    """Ethereum 26137261 #41: the Maker LitePSM takes DAI into one contract and pays USDC from its pocket."""
    weth, dai, usdc = BASE.weth, "0x50c5725949a6f0c72e6c4a641f24049a917db0cb", USDC_BASE
    psm, pocket = "0x37305b1cd40574e4c5ce33f8e8306be057fd7341", "0xf6e72db5454dd049d0788e411b06cfaf16853042"
    trace = call(
        EOA,
        BOT,
        calls=[
            call(BOT, weth, transfer(POOL_A, 10**18)),
            call(BOT, POOL_A, calls=[call(POOL_A, dai, transfer(BOT, 3000 * 10**18))]),
            call(BOT, dai, transfer(pocket, 3000 * 10**18)),
            call(BOT, psm, calls=[call(psm, usdc, transfer(BOT, 3000 * 10**6))]),
            call(BOT, usdc, transfer(POOL_B, 3000 * 10**6)),
            call(BOT, POOL_B, calls=[call(POOL_B, weth, transfer(BOT, 101 * 10**16))]),
        ],
    )
    assert trace_verdict(trace, weth, EOA, BOT).verdict != "cycle"
    tv = trace_verdict(trace, weth, EOA, BOT, venue_hints={psm}, venue_aliases={pocket: psm})
    assert tv.verdict == "cycle" and tv.profit_token == weth and tv.gross == 10**16 and psm in tv.venues
    spec = load_chain("ethereum")
    assert spec.venue_aliases == {pocket: psm} and psm in spec.declared_venues


def test_exact_closure_tolerates_receipt_rounding_dust_only():
    from mevlab.trace.operator import closed_gain

    weth, dai = BASE.weth, "0x50c5725949a6f0c72e6c4a641f24049a917db0cb"
    flows = {weth: 2 * 10**18, dai: 2 * 14_248 * 10**18}
    tok, gain, residual = closed_gain(
        {weth: 10**16, dai: -595_488_763_712}, flows, weth, exact=True
    )  # 6e-7 DAI on a 14,248 DAI leg
    assert tok == weth and gain == 10**16 and residual == [f"dust:{dai}"]
    assert (
        closed_gain({weth: 10**16, dai: -3 * 10**18}, flows, weth, exact=True) is None
    )  # a real 3 DAI loss still opens the position


def test_an_unregistered_token_minted_on_the_route_is_not_a_profit():
    """Ethereum 0xfb2c7bd6…: 0.41 USDC into a conversion path, 0.61 vault shares out, counted as a cycle."""
    from arbkit.scan.trace import review_unregistered_profit

    def row(profit, kinds, notes):
        return {
            "assessment": "trace_supported",
            "is_arb": True,
            "reasons": [],
            "profit_token": profit,
            "kinds": kinds,
            "trace": {"notes": notes},
        }

    r = row(TOKEN, ["cpmm_v2"], ["token_mint_redemption_flows", f"cycle_token:{USDC_BASE}"])
    review_unregistered_profit(BASE, r)
    assert r["assessment"] == "needs_review" and "unregistered_minted_profit_token" in r["reasons"]
    # the cycle's own token, even unregistered, through resolved venues: still counted (no mint involved)
    r = row(TOKEN, ["cpmm_v2"], [f"cycle_token:{TOKEN}"])
    review_unregistered_profit(BASE, r)
    assert r["assessment"] == "trace_supported"
    # a registered profit token minted on the route (WETH wrapped) is never questioned
    r = row(BASE.weth, ["cpmm_v2"], ["token_mint_redemption_flows", f"cycle_token:{USDC_BASE}"])
    review_unregistered_profit(BASE, r)
    assert r["assessment"] == "trace_supported"
    # unresolved venues and an unregistered token: review regardless of mints
    r = row(TOKEN, [], [])
    review_unregistered_profit(BASE, r)
    assert "unresolved_venues_unregistered_profit_token" in r["reasons"]


# ---------------------------------------------------------------- 2026-10-09 (second session): BSC random-block sheet, seed 20261011

MARKETING = "0x3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c"
HOOK_VAULT = "0x144d395b00000000000000000000000000000144"
HELPER = "0x36accdfa00000000000000000000000000000036"
USER_CONTRACT = "0xb589cd3a0000000000000000000000000000b589"
FEE_TAKER = "0x32303ffc0000000000000000000000000000f3c3"


def transfer_log(token, frm, to, amount):
    return {
        "address": token,
        "topics": [
            "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
            "0x" + f"{int(frm, 16):064x}",
            "0x" + f"{int(to, 16):064x}",
        ],
        "data": "0x" + f"{amount:064x}",
    }


def test_a_token_contract_selling_its_own_balance_is_not_a_venue():
    """BSC 126573593 #37: a taxed token's contract sells 5.17 of its accumulated tax tokens on PancakeSwap V2 through
    the router and forwards the 0.001454 BNB to its marketing wallet during a user's claim. The router (received WBNB,
    paid BNB on) and the marketing wallet had been read as the operator, the token contract as the first venue."""
    weth = BSC.weth
    claim = "0x9b1eb30700000000000000000000000000009b1e"
    sale = 5_166_270_538_202_281_304
    proceeds = 1_454_066_105_563_668
    trace = call(
        EOA,
        claim,
        calls=[
            call(
                claim,
                TOKEN,
                calls=[
                    call(
                        TOKEN,
                        ROUTER_PCS,
                        calls=[
                            call(ROUTER_PCS, TOKEN, transfer_from(TOKEN, POOL_A, sale)),
                            call(
                                ROUTER_PCS, POOL_A, calls=[call(POOL_A, weth, transfer(ROUTER_PCS, proceeds))]
                            ),
                            call(
                                ROUTER_PCS,
                                weth,
                                "0x2e1a7d4d",
                                calls=[call(weth, ROUTER_PCS, "0x", value=proceeds)],
                            ),
                            call(ROUTER_PCS, TOKEN, "0x", value=proceeds),
                        ],
                    ),
                    call(TOKEN, MARKETING, "0x", value=proceeds),
                ],
            )
        ],
    )
    logs = [transfer_log(TOKEN, TOKEN, POOL_A, sale), transfer_log(weth, POOL_A, ROUTER_PCS, proceeds)]
    tv = trace_verdict(trace, weth, EOA, claim, logs=logs)
    assert TOKEN not in tv.venues and tv.verdict != "cycle" and tv.gross is None
    assert any(n.startswith("self_selling_token_contracts:") for n in tv.notes)
    # a mint from the issuer (vault shares for collateral) is still a venue leg: nothing changes for deposits
    minted = call(EOA, BOT, calls=[call(BOT, USDC_BSC, transfer(TOKEN, 10**18)), call(BOT, TOKEN, calls=[])])
    mint_logs = [
        transfer_log(USDC_BSC, BOT, TOKEN, 10**18),
        transfer_log(TOKEN, "0x" + "0" * 40, BOT, 10**18),
    ]
    assert not any(
        n.startswith("self_selling_token_contracts:")
        for n in trace_verdict(minted, weth, EOA, BOT, logs=mint_logs).notes
    )


def test_a_dust_surplus_on_a_closed_route_is_not_a_profit():
    """BSC 126516560 #3 / #9: a protocol vault rebalances 5,300 CT against the Infinity pool inside a token's transfer
    hook; the helper's residue is 2 wei of CT on a 1,680 USDT flow."""
    weth = BSC.weth
    usdt = "0x55d398326f99059ff775485246999027b3197955"
    ct = "0x0a092e540000000000000000000000000000000a"
    sold, got = 1_749_250_474_832_929_920_471, 556_197_480_831_505_144_531
    trace = call(
        EOA,
        BOT,
        calls=[
            call(BOT, usdt, transfer(POOL_A, got)),
            call(BOT, POOL_A, calls=[call(POOL_A, ct, transfer(BOT, sold + 2))]),
            call(BOT, ct, transfer(POOL_B, sold)),
            call(BOT, POOL_B, calls=[call(POOL_B, usdt, transfer(BOT, got))]),
            call(BOT, ct, transfer(POOL_B, 2)),
        ],
    )
    logs = [
        transfer_log(usdt, BOT, POOL_A, got),
        transfer_log(ct, POOL_A, BOT, sold + 2),
        transfer_log(ct, BOT, POOL_B, sold),
        transfer_log(usdt, POOL_B, BOT, got),
    ]
    dust = trace_verdict(trace, weth, EOA, BOT, logs=logs)
    assert (
        dust.verdict != "cycle"
        and dust.gross is None
        and any(n.startswith("round_trip_dust_surplus:2") for n in dust.notes)
    )
    real = trace_verdict(
        trace, weth, EOA, BOT, logs=logs[:3] + [transfer_log(usdt, POOL_B, BOT, got + 10**15)]
    )
    assert real.verdict == "cycle" and real.gross == 10**15
    # without receipts (calldata amounts) the bound does not apply: a 2-wei calldata surplus still closes as a cycle
    calldata_only = call(EOA, BOT, calls=trace["calls"][:4])
    assert trace_verdict(calldata_only, weth, EOA, BOT).verdict == "cycle"


def test_the_transactions_own_parties_are_never_a_secondary_operators_venue():
    """BSC 126640601 #54: a user's 1-USDT buy of a memecoin through a fee-taking router chain closed as an "embedded
    cycle" whose last venue was the buyer's own contract (tx.to) and whose gain was the 0.005 USDT router fee."""
    weth = BSC.weth
    usdt = "0x55d398326f99059ff775485246999027b3197955"
    meme = "0x64bbd2310000000000000000000000000000064b"
    chain_ = "0x3c53f4c40000000000000000000000000000c53f"
    trace = call(
        EOA,
        USER_CONTRACT,
        calls=[
            call(USER_CONTRACT, usdt, transfer(FEE_TAKER, 5 * 10**15)),
            call(USER_CONTRACT, usdt, transfer(chain_, 995 * 10**15)),
            call(
                USER_CONTRACT,
                chain_,
                calls=[
                    call(chain_, usdt, transfer(POOL_A, 995 * 10**15)),
                    call(chain_, POOL_A, calls=[call(POOL_A, meme, transfer(chain_, 1_218 * 10**18))]),
                    call(chain_, meme, transfer(FEE_TAKER, 1_218 * 10**18)),
                ],
            ),
            call(FEE_TAKER, meme, transfer(USER_CONTRACT, 1_218 * 10**18)),
        ],
    )
    logs = [
        transfer_log(usdt, USER_CONTRACT, FEE_TAKER, 5 * 10**15),
        transfer_log(usdt, USER_CONTRACT, chain_, 995 * 10**15),
        transfer_log(usdt, chain_, POOL_A, 995 * 10**15),
        transfer_log(meme, POOL_A, chain_, 1_218 * 10**18),
        transfer_log(meme, chain_, FEE_TAKER, 1_218 * 10**18),
        transfer_log(meme, FEE_TAKER, USER_CONTRACT, 1_218 * 10**18),
    ]
    primary = trace_verdict(trace, weth, EOA, USER_CONTRACT, logs=logs)
    assert primary.verdict != "cycle"
    from mevlab.trace.embedded import embedded_cycles

    assert embedded_cycles(trace, weth, logs, primary) == []


def test_an_executor_fed_by_the_transactions_parties_is_not_a_secondary_operators_venue():
    """Base 52340882 #214: the callee hands 25.6 WETH to its executor, which sells across venues and runs a helper; the
    helper's WETH round trip closed through the executor as an "embedded cycle". The executor is undecoded and fed by the
    callee: operator-side. A decoded pool the callee trades on (a venue hint) stays a venue for a hook's cycle."""
    weth = BSC.weth
    usdc = "0x" + "a0" * 20
    executor, helper = "0x" + "4f" * 20, "0x" + "f4" * 20
    trace = call(
        EOA,
        USER_CONTRACT,
        calls=[
            call(USER_CONTRACT, weth, transfer(executor, 10**18)),
            call(
                USER_CONTRACT,
                executor,
                calls=[
                    call(
                        executor,
                        helper,
                        calls=[
                            call(helper, weth, transfer(POOL_B, 10**18)),
                            call(helper, POOL_B, calls=[call(POOL_B, usdc, transfer(helper, 3000 * 10**6))]),
                            call(helper, usdc, transfer(executor, 3000 * 10**6)),
                            call(executor, weth, transfer(helper, 10**18 + 10**15)),
                        ],
                    ),
                ],
            ),
        ],
    )
    logs = [
        transfer_log(weth, USER_CONTRACT, executor, 10**18),
        transfer_log(weth, executor, helper, 10**18),
        transfer_log(weth, helper, POOL_B, 10**18),
        transfer_log(usdc, POOL_B, helper, 3000 * 10**6),
        transfer_log(usdc, helper, executor, 3000 * 10**6),
        transfer_log(weth, executor, helper, 10**18 + 10**15),
    ]
    primary = trace_verdict(trace, weth, EOA, USER_CONTRACT, logs=logs)
    from mevlab.trace.embedded import embedded_cycles

    assert embedded_cycles(trace, weth, logs, primary, {POOL_B}) == []
    # declared as a decoded venue, the same contract is a market and the helper's cycle stands
    assert len(embedded_cycles(trace, weth, logs, primary, {POOL_B, executor})) == 1


# ---------------------------------------------------------------- 2026-10-09 (second session): Arbitrum random-block sheet, seed 20261011


def test_arbitrum_order_execution_contracts_are_declared_settlements():
    """GMX V2 keeper executions (MultichainVault → OrderVault), a Glide bridge-deposit fill and the Keeper order-batch
    vault had closed as cycles (7 of 50 random-block rows); a transaction that calls one is a settlement."""
    settlements = ARB.recipients_of_kind("settlement")
    for address in (
        "0x31ef83a530fde1b38ee9a18093a333d8bbbc40d5",
        "0xceaadfaf6a8c489b250e407987877c5fdfcdbe6e",
        "0x442be06bb5d13711a22ccd3fa382b72a874c5135",
        "0xc6dd6c021cfbb284f0c0d788547b51fc90fcf663",
    ):
        assert address in settlements
    assert "0x442be06bb5d13711a22ccd3fa382b72a874c5135" in load_chain("ethereum").recipients_of_kind(
        "settlement"
    )
    weth = ARB.weth
    usdc = "0xaf88d065e77c8cc2239327c5edb3a432268e5831"
    factory, glide = (
        "0x442be06bb5d13711a22ccd3fa382b72a874c5135",
        "0xbeba44b980b9d3f186451fb583894134772d56ad",
    )
    trace = call(
        EOA,
        BOT,
        calls=[
            call(BOT, usdc, transfer(glide, 8_680 * 10**6)),
            call(
                BOT, factory, calls=[call(factory, glide, calls=[call(glide, weth, transfer(BOT, 10**18))])]
            ),
            call(BOT, weth, transfer(POOL_B, 10**18)),
            call(BOT, POOL_B, calls=[call(POOL_B, usdc, transfer(BOT, 8_693 * 10**6))]),
        ],
    )
    row = run(ARB, trace)
    assert row["arb_class"] == "settlement_like" and row["assessment"] == "not_arb" and not row["is_arb"]
    assert any(r.startswith("settlement_contract_called:Glide") for r in row["reasons"])


def test_a_liquidation_event_makes_the_row_a_position_operation():
    """Arbitrum 513093052 #2: a Marginly ReceivePosition (WBTC debt repaid, WETH collateral received 30 % under the pools)
    closed as a $337 WETH cycle through Balancer, the Marginly pool and Uniswap V3."""
    from arbkit.scan.families import decode_liquidation
    from mevlab.decode.signatures import event_topic

    topic = event_topic("ReceivePosition(address,address,uint8,uint256,uint256)")
    liquidator, position = "0x" + "e3" * 20, "0x" + "9c" * 20
    log = {
        "address": "0x99cc2a68e121f2434db5c5d63670212f07f89ee8",
        "topics": [topic, "0x" + f"{int(liquidator, 16):064x}", "0x" + f"{int(position, 16):064x}"],
        "data": "0x" + f"{1:064x}" + f"{590014444938425972:064x}" + f"{315479:064x}",
    }
    decoded = decode_liquidation(log)
    assert (
        decoded["protocol"] == "marginly"
        and decoded["liquidator"] == liquidator
        and decoded["borrower"] == position
    )
    assert decoded["position_base_discounted_raw"] == "315479"
    weth = ARB.weth
    wbtc = "0x2f2a2543b76a4166549f7aab2e75bef0aefc5b0f"
    trace = cycle_trace(weth, wbtc, weth, 10**15)
    transfers = [
        transfer_log(wbtc, BOT, POOL_A, 10**18),
        transfer_log(weth, POOL_A, BOT, 3000 * 10**6),
        transfer_log(weth, BOT, POOL_B, 3000 * 10**6),
        transfer_log(wbtc, POOL_B, BOT, 10**18 + 10**15),
    ]
    row = row_template()
    tx = {"from": EOA, "to": BOT, "hash": row["tx_hash"]}
    apply_trace(ARB, row, trace, tx, {"status": "0x1", "logs": [*transfers, log]})
    assert row["arb_class"] == "debt_operation" and row["assessment"] == "not_arb" and not row["is_arb"]
    assert "liquidation_event:marginly" in row["reasons"]
    plain = row_template()
    apply_trace(ARB, plain, trace, tx, {"status": "0x1", "logs": transfers})
    assert plain["arb_class"] == "atomic_cycle"


def test_bsc_builder_payment_eoas_are_declared():
    """Four EOAs paid native BNB from cycle proceeds by 3-9 unrelated operators (BSC random-block sheets, seeds
    20261011/20261012); the vault 0x60b97709 paying one of them after a one-pool buy is operator-side again."""
    builders = BSC.recipients_of_kind("builder")
    for address in (
        "0xe89c42bc188c993273ab34231e12ae60c73042e4",
        "0x956e324dd26941c5626e560a74c71a9d856088f0",
        "0x83d4d453617ae14ffa220911730ad8eb38b05c08",
        "0x3199a46a1ab7821e226fcf9b336684c47faa1062",
    ):
        assert address in builders
    weth = BSC.weth
    cake = "0x0e09fabb73bd3ade0a17ecc321fd13a19e81ce82"
    usdt = "0x55d398326f99059ff775485246999027b3197955"
    payee = "0xe89c42bc188c993273ab34231e12ae60c73042e4"
    executor = "0x982385d2000000000000000000000000000982385"[:42]
    trace = call(
        EOA,
        FORWARDER,
        calls=[
            call(
                FORWARDER,
                VAULT,
                calls=[
                    call(VAULT, usdt, transfer(POOL_A, 343_823_902_172_614_133_074)),
                    call(VAULT, POOL_A, calls=[call(POOL_A, cake, transfer(executor, 160 * 10**18))]),
                    call(executor, cake, transfer(VAULT, 160 * 10**18)),
                    call(VAULT, payee, "0x", value=1_275_765_272_897),
                ],
            )
        ],
    )
    assert (
        trace_verdict(trace, weth, EOA, FORWARDER).verdict == "cycle"
    )  # the old reading: vault as venue, tip as gain
    fixed = trace_verdict(trace, weth, EOA, FORWARDER, known_recipients=BSC.recipients_of_kind("builder"))
    assert VAULT not in fixed.venues and fixed.verdict != "cycle"


# ---------------------------------------------------------------- 2026-10-09 (second session): Base random-block sheet, seed 20261011


def test_a_payout_burned_to_the_dead_address_is_not_proceeds():
    """Base 52327874 #30: inside a user's swap through the 0x4f6f9159 router, the router's fee module swaps its 1 % WETH
    cut for 83,082 SHIB on Uniswap V4 and burns it to 0x…dEaD; the module had closed as a SHIB cycle."""
    weth = BASE.weth
    shib = "0xfca95aeb5bf44ae355806a5ad14659c940dc6bf7"
    dead = "0x000000000000000000000000000000000000dead"
    trace = call(
        EOA,
        BOT,
        calls=[
            call(BOT, weth, transfer(POOL_A, 10**15)),
            call(BOT, POOL_A, calls=[call(POOL_A, shib, transfer(BOT, 100 * 10**9))]),
            call(BOT, shib, transfer(POOL_B, 100 * 10**9)),
            call(BOT, POOL_B, calls=[call(POOL_B, weth, transfer(BOT, 10**15 + 10**12))]),
            call(BOT, weth, transfer(dead, 10**12)),
        ],
    )
    logs = [
        transfer_log(weth, BOT, POOL_A, 10**15),
        transfer_log(shib, POOL_A, BOT, 100 * 10**9),
        transfer_log(shib, BOT, POOL_B, 100 * 10**9),
        transfer_log(weth, POOL_B, BOT, 10**15 + 10**12),
        transfer_log(weth, BOT, dead, 10**12),
    ]
    burned = trace_verdict(trace, weth, EOA, BOT, logs=logs)
    assert burned.verdict != "cycle" and "proceeds_consumed_by_protocol_payments" in burned.notes
    kept = trace_verdict(trace, weth, EOA, BOT, logs=logs[:4])
    assert kept.verdict == "cycle" and kept.gross == 10**12


def test_the_entry_token_is_the_profit_token_when_it_ends_positive_with_receipts():
    """Arbitrum 512984150 #15: a USDC loop whose +0.0121 USDC result lost to 1.2e-9 ETHFI of pass-through dust under the
    relative-gain fallback (ETHFI had no flow), priced from a leg at $2.33."""
    from mevlab.trace.operator import closed_gain

    usdc, ethfi = "0xaf88d065e77c8cc2239327c5edb3a432268e5831", "0x7189fb5b6504bbff6a852b13b7b82a3c118fdc27"
    net = {usdc: 12133, ethfi: 1226694652}
    flow = {usdc: 2_328_957, ethfi: 1226694652}  # ETHFI was only ever received: its gain is its whole flow
    assert closed_gain(net, flow, usdc, exact=True)[0] == usdc
    assert (
        closed_gain({ethfi: 1226694652}, {ethfi: 1226694652}, None, exact=True)[0] == ethfi
    )  # alone it still closes


def test_a_delegator_account_is_operator_side_never_a_venue():
    """Arbitrum 513260119 #3: a MetaMask smart account paid 1 USDC for exactly 22.9 tokens that its Spender bought for
    0.928 USDC; the account's exchange had closed as a 0.0716 USDC cycle. Arbitrum 512990139 #1 and 512995745 #1 are
    the same shape with the improvement paid to the executor's fee wallet: delegated order fills, never cycles. A
    bot's own account executing a two-leg cycle through the manager stays a cycle."""
    from mevlab.trace.calls import DELEGATION_MANAGER, delegator_accounts

    account, spender, token, payee = "0x" + "ce" * 20, "0x" + "0a" * 20, "0x" + "77" * 20, "0x" + "e3" * 20
    usdc = "0x" + "a0" * 20
    trace = call(
        EOA,
        DELEGATION_MANAGER,
        "0xcef6d209",
        calls=[
            call(
                DELEGATION_MANAGER,
                account,
                "0xd691c964",
                calls=[call(account, spender, "0x5f575529", calls=[call(spender, POOL_A, "0x128acb08")])],
            )
        ],
    )
    assert delegator_accounts(trace) == {account}
    logs = [
        transfer_log(usdc, account, spender, 1_000_000),
        transfer_log(usdc, spender, POOL_A, 928_393),
        transfer_log(token, POOL_A, spender, 22_912_306_847_787_969_194),
        transfer_log(token, spender, account, 22_912_306_847_787_969_194),
        transfer_log(usdc, spender, payee, 71_607),
    ]
    tv = trace_verdict(trace, ARB.weth, EOA, DELEGATION_MANAGER, logs=logs)
    assert tv.verdict != "cycle"
    assert any(n.startswith("delegator_accounts:") for n in tv.notes)
    row = row_template()
    apply_trace(
        ARB,
        row,
        trace,
        {"from": EOA, "to": DELEGATION_MANAGER, "hash": row["tx_hash"]},
        {"status": "0x1", "logs": logs},
    )
    assert row["arb_class"] == "settlement_like" and not row["is_arb"]
    assert any(r.startswith("delegated_order_execution:") for r in row["reasons"])
    # the same manager executing a bot account's two-leg cycle stays a cycle
    bot_trace = call(
        EOA,
        DELEGATION_MANAGER,
        "0xcef6d209",
        calls=[
            call(
                DELEGATION_MANAGER,
                BOT,
                "0xd691c964",
                calls=[call(BOT, POOL_A, "0x128acb08"), call(BOT, POOL_B, "0x128acb08")],
            )
        ],
    )
    bot_logs = [
        transfer_log(ARB.weth, BOT, POOL_A, 10**18),
        transfer_log(usdc, POOL_A, BOT, 3000 * 10**6),
        transfer_log(usdc, BOT, POOL_B, 3000 * 10**6),
        transfer_log(ARB.weth, POOL_B, BOT, 10**18 + 10**15),
    ]
    bot = trace_verdict(bot_trace, ARB.weth, EOA, DELEGATION_MANAGER, logs=bot_logs)
    assert bot.verdict == "cycle" and int(bot.gross) == 10**15


def test_a_dust_residue_does_not_hide_the_output_redirected_to_a_vault():
    """Optimism 157975774 #44: the group kept 6,701 wei WETH while the 0.000046 USDC.e output was claim-minted to the
    operator's vault; with the singleton manager out of the group the dust had become 'the' gain and the row dust."""
    vault = "0x" + "d2" * 20
    usdc = "0x" + "a0" * 20
    trace = call(EOA, BOT, calls=[call(BOT, POOL_A, "0x128acb08"), call(BOT, POOL_B, "0x128acb08")])
    logs = [
        transfer_log(usdc, BOT, POOL_A, 120_102),
        transfer_log(ARB.weth, POOL_A, BOT, 48_145_206_843_855),
        transfer_log(ARB.weth, BOT, POOL_B, 48_145_206_837_154),
        transfer_log(usdc, POOL_B, vault, 120_148),
    ]
    tv = trace_verdict(trace, ARB.weth, EOA, BOT, logs=logs)
    assert tv.verdict == "cycle" and tv.profit_token == usdc and int(tv.gross) == 46
    assert vault in tv.group and f"output_redirected_to:{vault}" in tv.notes


def test_the_order_executors_of_the_second_random_pass_are_declared():
    adapter = "0x0000000000bbf5c5fd284e657f01bd000933c96d"
    for chain in ("ethereum", "arbitrum", "base", "optimism", "bsc", "robinhood"):
        spec = load_chain(chain)
        assert adapter in spec.recipients_of_kind("settlement")
        assert "0xdb9b1e94b5b69df7e401ddbede43491141047db3" not in spec.recipients_of_kind(
            "settlement"
        )  # bots execute through it too
    assert "0x5d1d6fa733bfba0ec7b143de0d7e8a4afa9f9ba9" in load_chain("ethereum").recipients_of_kind(
        "settlement"
    )
    for chain in ("ethereum", "bsc"):
        assert "0xbeb0009aca35087ce7ccf11637e24dd1aad3bf2a" in load_chain(chain).quote_venues
    assert "0xf9e6d0d6f3a4ce89384a1387a32ac124e5c3cf84" in load_chain("arbitrum").recipients_of_kind(
        "settlement"
    )  # Sweeper
    assert "0x3cb658cc8f58dcc2adffed1e8f074a4a659b0222" in load_chain("bsc").recipients_of_kind(
        "settlement"
    )  # launchpad purchases


def test_a_debt_repayment_makes_the_row_a_position_operation():
    """Arbitrum 512966161 #4: a USDT0 flash swap repaid a position's Aave V3 debt, the 1.77 WETH collateral came out and
    was sold, and the 0.33 USDC left over had closed as a cycle."""
    from mevlab.decode.signatures import event_topic

    topic = event_topic("Repay(address,address,address,uint256,bool)")
    weth = ARB.weth
    usdt0 = "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9"
    position = "0x" + "e5" * 20
    repay = {
        "address": "0x794a61358d6845594f94dc1db02a252b5b4814ad",
        "topics": [
            topic,
            "0x" + f"{int(usdt0, 16):064x}",
            "0x" + f"{int(position, 16):064x}",
            "0x" + f"{int(position, 16):064x}",
        ],
        "data": "0x" + f"{4312318886:064x}" + f"{0:064x}",
    }
    trace = cycle_trace(usdt0, weth, usdt0, 10**6)
    transfers = [
        transfer_log(usdt0, BOT, POOL_A, 4312 * 10**6),
        transfer_log(weth, POOL_A, BOT, 10**18),
        transfer_log(weth, BOT, POOL_B, 10**18),
        transfer_log(usdt0, POOL_B, BOT, 4313 * 10**6),
    ]
    row = row_template()
    tx = {"from": EOA, "to": BOT, "hash": row["tx_hash"]}
    apply_trace(ARB, row, trace, tx, {"status": "0x1", "logs": [*transfers, repay]})
    assert row["arb_class"] == "debt_operation" and row["assessment"] == "not_arb" and not row["is_arb"]
    assert "debt_repayment:aave_v3_repay" in row["reasons"]
    plain = row_template()
    apply_trace(ARB, plain, trace, tx, {"status": "0x1", "logs": transfers})
    assert plain["arb_class"] == "atomic_cycle"


def test_the_registry_singleton_manager_is_never_absorbed_into_the_group():
    """Robinhood 84263927 #1: a WETH flash take from the Uniswap V4 manager, sold through two pools and settled back in
    ETH, emits no Swap event of the manager's; undeclared by the event layer it had joined the operator's group."""
    manager = next(iter(ARB.pool_managers))
    weth, usdc = ARB.weth, "0xaf88d065e77c8cc2239327c5edb3a432268e5831"
    payee = "0x" + "6a" * 20
    trace = call(
        EOA,
        BOT,
        calls=[
            call(
                BOT,
                manager,
                "0x48c89491",
                calls=[
                    call(
                        manager,
                        BOT,
                        "0x91dd7346",
                        calls=[
                            call(BOT, manager, "0x0b0d9c09"),
                            call(BOT, POOL_A, "0x128acb08"),
                            call(BOT, manager, "0x11da60b4"),
                            call(BOT, manager, "0x0b0d9c09"),
                            call(BOT, POOL_B, "0x128acb08"),
                            call(BOT, manager, "0x11da60b4"),
                            call(BOT, manager, "0x0b0d9c09"),
                        ],
                    )
                ],
            )
        ],
    )
    logs = [
        transfer_log(weth, manager, BOT, 10**18),
        transfer_log(weth, BOT, POOL_A, 10**18),
        transfer_log(usdc, POOL_A, manager, 3000 * 10**6),
        transfer_log(usdc, manager, BOT, 3000 * 10**6),
        transfer_log(usdc, BOT, POOL_B, 3000 * 10**6),
        transfer_log(weth, POOL_B, BOT, 101 * 10**16),
        transfer_log(weth, BOT, manager, 10**18),
        transfer_log(weth, manager, payee, 10**16),
    ]
    row = row_template()
    row["venues"] = [POOL_A, POOL_B]
    apply_trace(
        ARB, row, trace, {"from": EOA, "to": BOT, "hash": row["tx_hash"]}, {"status": "0x1", "logs": logs}
    )
    assert row["trace"]["verdict"] == "cycle"
    assert manager not in row["trace"]["group"]
    assert int(row["trace"]["gross"]) == 10**16


def test_a_route_venue_that_changed_liquidity_in_the_transaction_is_an_lp_action():
    """Base 52350210 #125: a position manager burned its Uniswap V3 range, collected, swapped through LI.FI and minted
    again; the pool's collected tokens had read as a $200 WETH cycle. Arbitrum 513003645 #3 / 513247838 #1: a Gamma
    Hypervisor rebalance whose fee split to the manager's fee contract had read as the result."""
    from mevlab.decode.signatures import SIG_V3_BURN, event_topic

    weth, usdc = ARB.weth, "0xaf88d065e77c8cc2239327c5edb3a432268e5831"
    trace = cycle_trace(weth, usdc, weth, 10**15)
    transfers = [
        transfer_log(weth, BOT, POOL_A, 10**18),
        transfer_log(usdc, POOL_A, BOT, 3000 * 10**6),
        transfer_log(usdc, BOT, POOL_B, 3000 * 10**6),
        transfer_log(weth, POOL_B, BOT, 10**18 + 10**15),
    ]
    burn = {
        "address": POOL_B,
        "topics": [
            event_topic(SIG_V3_BURN),
            "0x" + f"{int(BOT, 16):064x}",
            "0x" + f"{0:064x}",
            "0x" + f"{100:064x}",
        ],
        "data": "0x" + f"{5 * 10**17:064x}" + f"{10**18:064x}" + f"{3000 * 10**6:064x}",
    }
    row = row_template()
    tx = {"from": EOA, "to": BOT, "hash": row["tx_hash"]}
    apply_trace(ARB, row, trace, tx, {"status": "0x1", "logs": [*transfers, burn]})
    assert row["arb_class"] == "lp_action" and not row["is_arb"]
    assert f"liquidity_event_at_route_venue:{POOL_B}" in row["reasons"]
    plain = row_template()
    apply_trace(ARB, plain, trace, tx, {"status": "0x1", "logs": transfers})
    assert plain["arb_class"] == "atomic_cycle"
    # a liquidity event of the singleton manager (any pool of it) does not flip a cycle through the manager
    manager = next(iter(ARB.pool_managers))
    modify = {
        "address": manager,
        "topics": [
            event_topic("ModifyLiquidity(bytes32,address,int24,int24,int256,bytes32)"),
            "0x" + "ab" * 32,
            "0x" + f"{int(BOT, 16):064x}",
        ],
        "data": "0x" + "00" * 128,
    }
    singleton = row_template()
    singleton["venues"] = [POOL_A, POOL_B]
    apply_trace(ARB, singleton, trace, tx, {"status": "0x1", "logs": [*transfers, modify]})
    assert singleton["arb_class"] == "atomic_cycle"


def test_a_v4_sandwich_result_does_not_fold_the_wrap_on_top_of_the_native_flow():
    """Ethereum 26143358 #11: ETH taken from the Uniswap V4 pool behind the victim and wrapped; the native take and the
    WETH Deposit had both counted, doubling the result (0.001327 WETH read as 0.002654)."""
    from arbkit.scan.sandwich import operator_result
    from mevlab.decode.signatures import WETH_DEPOSIT_TOPIC, WETH_WITHDRAWAL_TOPIC

    weth = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
    manager = "0x000000000004444c5dc75cb358380d2e3de08a90"
    front, back = "0x" + "f1" * 32, "0x" + "b2" * 32
    token = "0x" + "e9" * 20

    def ev(topic, who, value):
        return {
            "address": weth,
            "topics": [topic, "0x" + f"{int(who, 16):064x}"],
            "data": "0x" + f"{value:064x}",
        }

    receipts = {
        front: {
            "logs": [
                ev(WETH_WITHDRAWAL_TOPIC, BOT, 29 * 10**15),
                transfer_log(token, manager, BOT, 1_811_615 * 10**18),
            ]
        },
        back: {
            "logs": [
                transfer_log(token, BOT, manager, 1_811_613 * 10**18),
                ev(WETH_DEPOSIT_TOPIC, BOT, 30_642 * 10**12),
            ]
        },
    }
    traces = {
        front: call(
            EOA,
            BOT,
            calls=[
                call(weth, BOT, "0x", value=29 * 10**15),
                call(BOT, manager, "0x11da60b4", value=29 * 10**15),
            ],
        ),
        back: call(
            EOA,
            BOT,
            calls=[
                call(manager, BOT, "0x", value=30_642 * 10**12),
                call(BOT, weth, "0xd0e30db0", value=30_642 * 10**12),
            ],
        ),
    }
    bundle = {"operator": BOT, "front": front, "back": back, "pool": manager, "victims": []}
    result = operator_result(bundle, receipts, weth, traces, EOA)
    assert int(result["net_raw"][weth]) == 30_642 * 10**12 - 29 * 10**15
    # without traces the events are the only sight of the native side and still count
    blind = operator_result(bundle, receipts, weth, None, EOA)
    assert int(blind["net_raw"][weth]) == 30_642 * 10**12 - 29 * 10**15


def test_liquidity_changes_inside_a_hook_callback_are_the_pools_own():
    """Robinhood 84259655 #4 / 70067057 #13 / 70067123 #13 / 54346064 #7, BSC 83600063 #2 / 83600097 #1: the hook of the
    Uniswap V4 pool (or its fee locker, through the position manager) collected, compounded or rebalanced its own
    liquidity inside beforeSwap/afterSwap; the operator's cycle had been held as an lp_action. A modifyLiquidity the
    operator reaches itself, or through a position manager it calls, is still its own liquidity action."""
    from arbkit.scan.block import singleton_liquidity_only
    from mevlab.decode.signatures import SIG_V3_SWAP, SIG_V4_MODIFY, event_topic
    from mevlab.trace.calls import hook_liquidity_only

    manager = next(iter(BASE.pool_managers))
    hook, locker, position_manager = "0x" + "40" * 20, "0x" + "63" * 20, "0x" + "7c" * 20
    swap, modify, after_swap, unlock_cb = "0xf3cd914c", "0x5a6bcfda", "0xb47b2fb1", "0x91dd7346"
    hooked = call(
        EOA,
        BOT,
        calls=[
            call(
                BOT,
                manager,
                "0x48c89491",
                calls=[
                    call(
                        manager,
                        BOT,
                        unlock_cb,
                        calls=[
                            call(
                                BOT,
                                manager,
                                swap,
                                calls=[
                                    call(
                                        manager,
                                        hook,
                                        after_swap,
                                        calls=[
                                            call(
                                                hook,
                                                locker,
                                                "0x86b0c83f",
                                                calls=[
                                                    call(
                                                        locker,
                                                        position_manager,
                                                        "0x4afe393c",
                                                        calls=[call(position_manager, manager, modify)],
                                                    )
                                                ],
                                            ),
                                            call(hook, manager, modify),
                                        ],
                                    )
                                ],
                            )
                        ],
                    )
                ],
            )
        ],
    )
    assert hook_liquidity_only(hooked, {manager})
    own = call(
        EOA,
        BOT,
        calls=[
            call(
                BOT,
                manager,
                "0x48c89491",
                calls=[
                    call(
                        manager,
                        BOT,
                        unlock_cb,
                        calls=[
                            call(BOT, manager, swap),
                            call(
                                BOT,
                                position_manager,
                                "0x4afe393c",
                                calls=[call(position_manager, manager, modify)],
                            ),
                        ],
                    )
                ],
            )
        ],
    )
    assert not hook_liquidity_only(own, {manager})
    mixed = call(
        EOA,
        BOT,
        calls=[
            call(
                BOT,
                manager,
                "0x48c89491",
                calls=[
                    call(
                        manager,
                        BOT,
                        unlock_cb,
                        calls=[
                            call(
                                BOT,
                                manager,
                                swap,
                                calls=[call(manager, hook, after_swap, calls=[call(hook, manager, modify)])],
                            ),
                            call(BOT, manager, modify),
                        ],
                    )
                ],
            )
        ],
    )
    assert not hook_liquidity_only(mixed, {manager})
    assert not hook_liquidity_only(call(EOA, BOT, calls=[call(BOT, manager, swap)]), {manager})

    # the engine reads the trace first only for receipts whose liquidity changes are all the singleton's, next to a swap
    def modify_log(address, delta):
        return {
            "address": address,
            "topics": [event_topic(SIG_V4_MODIFY), "0x" + "ab" * 32, "0x" + f"{int(hook, 16):064x}"],
            "data": "0x"
            + f"{(-887220) % 2**256:064x}"
            + f"{887220:064x}"
            + f"{delta % 2**256:064x}"
            + "00" * 32,
        }

    v3_swap = {
        "address": POOL_A,
        "topics": [event_topic(SIG_V3_SWAP), "0x" + f"{int(BOT, 16):064x}", "0x" + f"{int(BOT, 16):064x}"],
        "data": "0x"
        + f"{10**18:064x}"
        + f"{(-3000 * 10**6) % 2**256:064x}"
        + f"{2**96:064x}"
        + f"{10**20:064x}"
        + "00" * 32,
    }
    assert singleton_liquidity_only({"logs": [v3_swap, modify_log(manager, 10**12)]}, BASE)
    assert not singleton_liquidity_only(
        {"logs": [modify_log(manager, 10**12)]}, BASE
    )  # no swap: never a cycle
    assert singleton_liquidity_only(
        {"logs": [v3_swap, modify_log(manager, 0)]}, BASE
    )  # a collect: the trace says whose
    assert not singleton_liquidity_only(
        {"logs": [v3_swap, modify_log(POOL_B, 10**12)]}, BASE
    )  # not the singleton's


def test_a_bare_payment_to_a_code_less_address_is_not_a_venue_call():
    """Ethereum 26137380 #168: the bot pulled its funding from the operator's wallet by allowance, ran the cycle, paid the
    wallet back with the gain and returned the change of a pre-funded builder tip as plain ETH. The bare value call had
    made the wallet a "called" counterparty, so it read as the closing venue and the returned change as a 0.0203 ETH
    loss. A bonding curve bought by sending it ETH still counts: its frame runs code."""
    from mevlab.trace.calls import callees

    weth, usdc = BASE.weth, USDC_BASE
    funder, curve = "0x" + "fa" * 20, "0x" + "cc" * 20
    legs = [
        call(BOT, weth, transfer(POOL_A, 10**18)),
        call(BOT, POOL_A, calls=[call(POOL_A, usdc, transfer(BOT, 3000 * 10**6))]),
        call(BOT, usdc, transfer(POOL_B, 3000 * 10**6)),
        call(BOT, POOL_B, calls=[call(POOL_B, weth, transfer(BOT, 10**18 + 10**15))]),
    ]
    trace = call(
        EOA,
        BOT,
        calls=[
            call(BOT, weth, transfer_from(funder, BOT, 10**17)),
            *legs,
            call(BOT, weth, transfer(funder, 10**17 + 10**15)),
            call(BOT, funder, "0x", value=2 * 10**15),
        ],
    )
    assert funder not in callees(trace)
    tv = trace_verdict(trace, weth, EOA, BOT)
    assert tv.verdict == "cycle" and tv.venues == [POOL_A, POOL_B] and tv.gross == 10**15
    # the change of the pre-funded tip is inventory moving, not proceeds: the payout is capped at what the legs made
    assert tv.payouts == {funder: 10**15} and f"inventory_payout:{weth}:{2 * 10**15}" in tv.notes
    token = "0x" + "7e" * 20
    bought = call(
        EOA,
        BOT,
        calls=[
            call(BOT, curve, "0x", value=10**17, calls=[call(curve, token, transfer(BOT, 500 * 10**18))]),
            call(BOT, token, transfer(POOL_A, 500 * 10**18)),
            call(BOT, POOL_A, calls=[call(POOL_A, weth, transfer(BOT, 10**17 + 10**15))]),
        ],
    )
    assert curve in callees(bought)
    ran = trace_verdict(bought, weth, EOA, BOT)
    assert ran.verdict == "cycle" and curve in ran.venues and ran.gross == 10**15


def test_addresses_reached_inside_a_hook_callback_are_never_venues():
    """Robinhood 70067057 #21: five Uniswap V4 pools; the hook's rebalancer exchanged tokens with the manager inside
    afterSwap and read as a second venue, so the trace searched a returning route through it, found none, and the
    closed +0.0875 USDG gain stayed under review. Addresses reached inside a hook callback act for the pool: they are
    dropped from the venues (and never folded in as collectors); the route is then singleton-only and the events
    establish the pools."""
    from mevlab.trace.calls import hook_callees

    manager = next(iter(ARB.pool_managers))
    weth, usdc, tok_a, tok_b = (
        ARB.weth,
        "0xaf88d065e77c8cc2239327c5edb3a432268e5831",
        "0x" + "7a" * 20,
        "0x" + "7b" * 20,
    )
    hook, rebalancer = "0x" + "40" * 20, "0x" + "6f" * 20
    trace = call(
        EOA,
        BOT,
        calls=[
            call(
                BOT,
                manager,
                "0x48c89491",
                calls=[
                    call(
                        manager,
                        BOT,
                        "0x91dd7346",
                        calls=[
                            call(BOT, manager, "0x0b0d9c09"),
                            call(
                                BOT,
                                manager,
                                "0xf3cd914c",
                                calls=[
                                    call(
                                        manager,
                                        hook,
                                        "0xb47b2fb1",
                                        calls=[
                                            call(
                                                hook,
                                                rebalancer,
                                                "0x167d1604",
                                                calls=[
                                                    call(rebalancer, manager, "0x0b0d9c09"),
                                                    call(rebalancer, manager, "0xf3cd914c"),
                                                    call(rebalancer, manager, "0x11da60b4"),
                                                ],
                                            )
                                        ],
                                    )
                                ],
                            ),
                            call(BOT, manager, "0xf3cd914c"),
                            call(BOT, manager, "0x11da60b4"),
                        ],
                    )
                ],
            )
        ],
    )
    assert hook_callees(trace, {manager}) == {hook, rebalancer}
    logs = [
        transfer_log(weth, manager, BOT, 101 * 10**16),
        transfer_log(weth, BOT, manager, 10**18),
        transfer_log(tok_a, manager, rebalancer, 16 * 10**18),
        transfer_log(tok_a, rebalancer, manager, 11 * 10**18),
        transfer_log(tok_b, manager, rebalancer, 35 * 10**13),
        transfer_log(tok_b, rebalancer, manager, 7 * 10**13),
        transfer_log(usdc, manager, BOT, 3000 * 10**6),
        transfer_log(usdc, BOT, manager, 3000 * 10**6),
    ]
    tx = {"from": EOA, "to": BOT, "hash": row_template()["tx_hash"]}
    tv = trace_verdict(trace, weth, EOA, BOT, logs=logs, venue_hints={manager}, singletons={manager})
    assert rebalancer not in tv.venues and tv.venues == [manager] and tv.verdict == "single_venue_loop"
    assert f"hook_side_venues_dropped:{rebalancer}" in tv.notes
    assert tv.gross == 10**16 and tv.profit_token == weth
    pools = [f"{manager}:0x" + "11" * 32, f"{manager}:0x" + "22" * 32]
    row = {
        **row_template(),
        "is_arb": True,
        "assessment": "event_candidate",
        "arb_class": "atomic_cycle",
        "n_legs": 2,
        "venues": pools,
        "event_label": {"venues": pools, "gross_profit_raw": 10**16, "profit_token": weth},
        "gross_profit_raw": str(10**16),
        "gross_source": "events",
        "profit_token": weth,
    }
    apply_trace(ARB, row, trace, tx, {"status": "0x1", "logs": logs})
    assert row["assessment"] == "trace_supported" and row["arb_class"] == "atomic_cycle"
    assert "singleton_multi_pool_route_receipt_closed" in row["reasons"]
    # the hook's own swaps had made the event layer name another profit token: with a hook-side venue dropped the
    # receipts decide alone
    polluted = {
        **row,
        "reasons": [],
        "assessment": "event_candidate",
        "arb_class": "atomic_cycle",
        "is_arb": True,
        "event_label": {"venues": pools, "gross_profit_raw": 5, "profit_token": tok_b},
        "profit_token": tok_b,
        "gross_profit_raw": "5",
    }
    apply_trace(ARB, polluted, trace, tx, {"status": "0x1", "logs": logs})
    assert (
        polluted["assessment"] == "trace_supported"
        and polluted["profit_token"] == weth
        and int(polluted["gross_profit_raw"]) == 10**16
    )


def test_a_cycle_executed_by_the_pools_hook_inside_a_users_swap_is_the_pools_mechanism():
    """Robinhood 84319662 #1: an EOA buys 1.08 tokens for 0.00001 ETH; inside the swap the pool's hook runs its own
    round trip through its executor (0.086 ETH between two pools, +0.000315 ETH kept as claims). The event layer saw a
    cycle; it was the hook's, never the sender's, and had been counted as the sender's 0.000305 WETH cycle."""
    from mevlab.decode.signatures import SIG_V4_SWAP, event_topic

    manager = next(iter(ARB.pool_managers))
    weth, token = ARB.weth, "0x" + "7e" * 20
    hook, executor = "0x" + "40" * 20, "0x" + "4b" * 20
    trace = call(
        EOA,
        BOT,
        calls=[
            call(
                BOT,
                manager,
                "0x48c89491",
                calls=[
                    call(
                        manager,
                        BOT,
                        "0x91dd7346",
                        calls=[
                            call(
                                BOT,
                                manager,
                                "0xf3cd914c",
                                value=10**13,
                                calls=[
                                    call(
                                        manager,
                                        hook,
                                        "0x575e24b4",
                                        calls=[
                                            call(
                                                hook,
                                                executor,
                                                "0x76bbe04a",
                                                calls=[
                                                    call(executor, manager, "0xf3cd914c"),
                                                    call(executor, manager, "0xf3cd914c"),
                                                ],
                                            )
                                        ],
                                    )
                                ],
                            ),
                            call(BOT, manager, "0x0b0d9c09"),
                        ],
                    )
                ],
            )
        ],
    )

    def v4_swap(pool_id, swapper):
        return {
            "address": manager,
            "topics": [event_topic(SIG_V4_SWAP), pool_id, "0x" + f"{int(swapper, 16):064x}"],
            "data": "0x"
            + f"{(-86 * 10**15) % 2**256:064x}"
            + f"{9337 * 10**18:064x}"
            + f"{2**96:064x}"
            + f"{10**20:064x}"
            + f"{0:064x}"
            + f"{3000:064x}",
        }

    logs = [
        v4_swap("0x" + "a1" * 32, executor),
        v4_swap("0x" + "a2" * 32, executor),
        v4_swap("0x" + "a2" * 32, BOT),
        transfer_log(token, manager, EOA, 108 * 10**16),
    ]  # the user's own single swap sits on one of the hook's pools
    pools = [f"{manager}:0x" + "a1" * 32, f"{manager}:0x" + "a2" * 32]
    row = {
        **row_template(),
        "is_arb": True,
        "assessment": "event_candidate",
        "arb_class": "atomic_cycle",
        "n_legs": 2,
        "venues": pools,
        "event_label": {"venues": pools, "gross_profit_raw": 3 * 10**14, "profit_token": weth},
        "gross_profit_raw": str(3 * 10**14),
        "gross_source": "events",
        "profit_token": weth,
    }
    apply_trace(
        ARB, row, trace, {"from": EOA, "to": BOT, "hash": row["tx_hash"]}, {"status": "0x1", "logs": logs}
    )
    assert row["assessment"] == "not_arb" and row["arb_class"] == "hook_mechanism" and not row["is_arb"]
    assert f"cycle_legs_executed_by_hook:{executor}" in row["reasons"]


def test_a_fluid_vault_operation_makes_the_row_a_position_operation():
    """Ethereum 26141146 #245: a Safe repays its 1.0078 WBTC Fluid vault debt with a flash loan, withdraws 130,681 USDC of
    collateral and buys the WBTC back for 83,942 USDC; the collateral withdrawal had closed as a 130,681 USDC "cycle"."""
    from mevlab.decode.signatures import event_topic

    weth, usdc = ARB.weth, "0xaf88d065e77c8cc2239327c5edb3a432268e5831"
    vault = "0x" + "f1" * 20
    operate = {
        "address": vault,
        "topics": [event_topic("LogOperate(address,uint256,int256,int256,address)")],
        "data": "0x"
        + f"{int(BOT, 16):064x}"
        + f"{19392:064x}"
        + f"{(-130681342765) % 2**256:064x}"
        + f"{0:064x}"
        + f"{0:064x}",
    }
    transfers = [
        transfer_log(weth, BOT, POOL_A, 10**18),
        transfer_log(usdc, POOL_A, BOT, 3000 * 10**6),
        transfer_log(usdc, BOT, POOL_B, 3000 * 10**6),
        transfer_log(weth, POOL_B, BOT, 10**18 + 10**15),
    ]
    row = row_template()
    apply_trace(
        ARB,
        row,
        cycle_trace(weth, weth, usdc, 10**15),
        {"from": EOA, "to": BOT, "hash": row["tx_hash"]},
        {"status": "0x1", "logs": [*transfers, operate]},
    )
    assert (
        row["arb_class"] == "debt_operation"
        and not row["is_arb"]
        and "debt_repayment:fluid_vault_operate" in row["reasons"]
    )


def test_a_declared_settlement_is_never_a_secondary_operator():
    """Base 52340002 #58: a user's 0x Settler swap through Multicall3 sells 1,028 tokens to a Bebop maker through the Bebop
    settlement for 0.002934 WETH; the Settler keeps 0.0000249 ETH. The Settler and the settlement had closed as a secondary
    operator's cycle with the maker as its venue. A declared quote venue or settlement is a venue, never an operator."""
    from mevlab.trace.embedded import embedded_cycles
    from mevlab.trace.verdict import trace_verdict

    weth, token = BASE.weth, "0x" + "75" * 20
    multicall, settler, bebop, maker = (
        "0x" + "ca" * 20,
        "0x" + "97" * 20,
        "0xbbbbbbb520d69a9775e85b458c58c648259fad5f",
        "0x" + "51" * 20,
    )
    trace = call(
        EOA,
        multicall,
        calls=[
            call(
                multicall,
                settler,
                calls=[
                    call(settler, token, transfer_from(EOA, bebop, 1028 * 10**18)),
                    call(
                        settler,
                        bebop,
                        calls=[
                            call(bebop, token, transfer(maker, 1028 * 10**18)),
                            call(bebop, weth, transfer_from(maker, bebop, 2934 * 10**12)),
                            call(bebop, weth, "0x2e1a7d4d"),
                            call(bebop, settler, "0x", value=2934 * 10**12),
                        ],
                    ),
                    call(settler, multicall, "0x", value=2909 * 10**12),
                ],
            ),
            call(multicall, EOA, "0x", value=2909 * 10**12),
        ],
    )
    primary = trace_verdict(trace, weth, EOA, multicall, quote_venues={bebop})
    assert primary.verdict != "cycle"
    assert embedded_cycles(trace, weth, [], primary, blocked={bebop}) == []
    assert (
        bebop in BASE.quote_venues
        and bebop in load_chain("optimism").quote_venues
        and bebop in ARB.quote_venues
    )


def test_a_counted_cycle_through_an_undecoded_venue_is_flagged_not_dropped():
    trace = cycle_trace(BASE.weth, BASE.weth, USDC_BASE, 10**15)
    row = run(BASE, trace)
    assert row["is_arb"] and row["assessment"] == "trace_supported"
    flagged = [r for r in row["reasons"] if r.startswith("trace_only_venue:")]
    assert flagged and POOL_A in flagged[0] and POOL_B in flagged[0]
    # the same cycle with both pools established by the event layer carries no flag
    row = row_template()
    row["event_label"] = {"venues": [POOL_A, POOL_B]}
    apply_trace(BASE, row, trace, {"from": EOA, "to": BOT, "hash": row["tx_hash"]}, {"status": "0x1"})
    assert row["is_arb"] and not any(r.startswith("trace_only_venue:") for r in row["reasons"])
