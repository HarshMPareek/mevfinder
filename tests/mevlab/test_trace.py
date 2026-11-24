"""T2 trace cross-check on synthetic callTracer trees: legs, venues, taker, payouts, reverted frames."""

from mevlab.trace.compare import compare
from mevlab.trace.movements import movements
from mevlab.trace.verdict import trace_verdict

WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
DAI = "0x50c5725949a6f0c72e6c4a641f24049a917db0cb"
EOA = "0x1111111111111111111111111111111111111111"
BOT = "0xb0b0000000000000000000000000000000000b0b"
POOL_A = "0xaaaa00000000000000000000000000000000aaaa"
POOL_B = "0xbbbb00000000000000000000000000000000bbbb"
COLLECTOR = "0xc01100000000000000000000000000000000c011"
ROUTER = "0x7007000000000000000000000000000000007007"


def transfer(to: str, amount: int) -> str:
    return "0xa9059cbb" + f"{int(to, 16):064x}" + f"{amount:064x}"


def transfer_from(frm: str, to: str, amount: int) -> str:
    return "0x23b872dd" + f"{int(frm, 16):064x}" + f"{int(to, 16):064x}" + f"{amount:064x}"


def call(
    frm: str, to: str, inp: str = "0x12345678", value: int = 0, calls=None, typ: str = "CALL", error=None
):
    c = {"type": typ, "from": frm, "to": to, "input": inp, "value": hex(value), "calls": calls or []}
    if error:
        c["error"] = error
    return c


def two_leg_cycle_trace():
    return call(
        EOA,
        BOT,
        calls=[
            call(BOT, WETH, transfer(POOL_A, 10**18)),
            call(BOT, POOL_A, calls=[call(POOL_A, USDC, transfer(BOT, 3000 * 10**6))]),
            call(BOT, USDC, transfer(POOL_B, 3000 * 10**6)),
            call(BOT, POOL_B, calls=[call(POOL_B, WETH, transfer(BOT, 101 * 10**16))]),
            call(BOT, WETH, transfer(COLLECTOR, 3 * 10**15)),
        ],
    )


def test_two_leg_cycle_with_payout():
    tv = trace_verdict(two_leg_cycle_trace(), WETH)
    assert tv.verdict == "cycle"
    assert tv.venues == [POOL_A, POOL_B]
    assert [(lg.venue, lg.token_in, lg.amount_in, lg.token_out, lg.amount_out) for lg in tv.legs] == [
        (POOL_A, WETH, 10**18, USDC, 3000 * 10**6),
        (POOL_B, USDC, 3000 * 10**6, WETH, 101 * 10**16),
    ]
    assert tv.taker == BOT and tv.profit_token == WETH
    assert tv.gross == 10**16 and tv.net_after == 7 * 10**15
    assert tv.payouts == {COLLECTOR: 3 * 10**15}
    assert sorted(tv.tokens) == sorted([WETH, USDC])


def test_compare_with_label_row():
    tv = trace_verdict(two_leg_cycle_trace(), WETH)
    row = {
        "is_arb": True,
        "n_legs": 2,
        "tokens": f'["{WETH}", "{USDC}"]',
        "beneficiary": BOT,
        "profit_token": WETH,
        "gross_profit_raw": str(10**16),
        "gross_trace_raw": None,
    }
    c = compare(row, tv, WETH)
    assert all(
        c[k]
        for k in (
            "class_agree",
            "legs_agree",
            "tokens_agree",
            "beneficiary_agree",
            "profit_token_agree",
            "gross_agree",
        )
    )
    row["gross_profit_raw"] = str(2 * 10**16)  # events overstated the gross
    c = compare(row, tv, WETH)
    assert not c["gross_agree"] and abs(c["gross_rel_diff"] - 0.5) < 1e-9


def test_reverted_subtree_is_ignored_and_proxy_delegatecall_not_double_counted():
    tr = two_leg_cycle_trace()
    # a failed extra leg: its transfers must not count
    tr["calls"].append(
        call(BOT, POOL_A, error="execution reverted", calls=[call(POOL_A, USDC, transfer(BOT, 10**9))])
    )
    # USDC is a proxy: the outer CALL carries the transfer, the DELEGATECALL repeats the calldata
    tr["calls"][2]["calls"] = [
        call(USDC, "0x" + "ee" * 20, transfer(POOL_B, 3000 * 10**6), typ="DELEGATECALL")
    ]
    movs, reverted = movements(tr, WETH)
    assert not reverted
    assert sum(1 for m in movs if m.token == USDC) == 2
    tv = trace_verdict(tr, WETH)
    assert tv.verdict == "cycle" and tv.gross == 10**16


def test_single_venue_round_trip_and_loss_are_not_cycles():
    loop = call(
        EOA,
        BOT,
        calls=[
            call(BOT, WETH, transfer(POOL_A, 10**18)),
            call(BOT, POOL_A, calls=[call(POOL_A, USDC, transfer(BOT, 3000 * 10**6))]),
            call(BOT, USDC, transfer(POOL_A, 3000 * 10**6)),
            call(BOT, POOL_A, calls=[call(POOL_A, WETH, transfer(BOT, 101 * 10**16))]),
        ],
    )
    assert trace_verdict(loop, WETH).verdict == "single_venue_loop"
    loss = two_leg_cycle_trace()
    loss["calls"][3] = call(BOT, POOL_B, calls=[call(POOL_B, WETH, transfer(BOT, 99 * 10**16))])
    loss["calls"].pop()  # no payout
    tv = trace_verdict(loss, WETH)
    assert tv.verdict == "no_taker" and tv.venues == [POOL_A, POOL_B]


def test_user_multihop_swap_is_not_a_cycle_and_native_value_counts():
    # user sends 1 ETH to a router, gets DAI via two pools (pool-to-pool transfer in between)
    tr = call(
        EOA,
        ROUTER,
        value=10**18,
        calls=[
            call(ROUTER, WETH, "0xd0e30db0", value=10**18),  # deposit: invisible under ETH ≡ WETH
            call(ROUTER, WETH, transfer(POOL_A, 10**18)),
            call(ROUTER, POOL_A, calls=[call(POOL_A, USDC, transfer(POOL_B, 3000 * 10**6))]),
            call(ROUTER, POOL_B, calls=[call(POOL_B, DAI, transfer(EOA, 2990 * 10**18))]),
        ],
    )
    tv = trace_verdict(tr, WETH)
    assert tv.venues == [POOL_A, POOL_B]
    assert tv.verdict == "no_taker"  # the user paid ETH and holds DAI: two open assets, no cycle


def test_transfer_from_uses_calldata_holder_and_flash_swap_order():
    # V3-style: the pool pays out first, the bot pays in from the callback via transferFrom on its behalf
    tr = call(
        EOA,
        BOT,
        calls=[
            call(
                BOT,
                POOL_A,
                calls=[
                    call(POOL_A, USDC, transfer(BOT, 3000 * 10**6)),
                    call(
                        POOL_A, BOT, "0xfa461e33", calls=[call(BOT, WETH, transfer_from(BOT, POOL_A, 10**18))]
                    ),
                ],
            ),
            call(
                BOT,
                POOL_B,
                calls=[
                    call(POOL_B, WETH, transfer(BOT, 102 * 10**16)),
                    call(POOL_B, BOT, "0xfa461e33", calls=[call(BOT, USDC, transfer(POOL_B, 3000 * 10**6))]),
                ],
            ),
        ],
    )
    tv = trace_verdict(tr, WETH)
    assert tv.verdict == "cycle" and tv.taker == BOT and tv.gross == 2 * 10**16
    assert [(lg.venue, lg.token_out, lg.token_in) for lg in tv.legs] == [
        (POOL_A, USDC, WETH),
        (POOL_B, WETH, USDC),
    ]


LENDER = "0xbbbb0000000000000000000000000000000bbbb0"
HELPER = "0xada80000000000000000000000000000000ada80"
POOLFEES = "0xfee00000000000000000000000000000000fee00"


def test_flash_loan_through_helper_gross_is_profit_not_principal():
    # bot borrows 1 WETH, hands it to a helper that swaps on pool A, gets USDC back via the helper, sells on
    # pool B, pays 30% of the profit to a collector and repays the lender
    tr = call(
        EOA,
        BOT,
        calls=[
            call(BOT, LENDER, calls=[call(LENDER, WETH, transfer(BOT, 10**18))]),
            call(BOT, WETH, transfer(HELPER, 10**18)),
            call(
                BOT,
                HELPER,
                calls=[
                    call(HELPER, WETH, transfer(POOL_A, 10**18)),
                    call(HELPER, POOL_A, calls=[call(POOL_A, USDC, transfer(HELPER, 3000 * 10**6))]),
                    call(HELPER, USDC, transfer(BOT, 3000 * 10**6)),
                ],
            ),
            call(BOT, USDC, transfer(POOL_B, 3000 * 10**6)),
            call(BOT, POOL_B, calls=[call(POOL_B, WETH, transfer(BOT, 101 * 10**16))]),
            call(BOT, WETH, transfer(COLLECTOR, 3 * 10**15)),
            call(BOT, WETH, transfer(LENDER, 10**18)),
        ],
    )
    tv = trace_verdict(tr, WETH)
    assert tv.verdict == "cycle" and tv.venues == [POOL_A, POOL_B]
    assert tv.taker == BOT and tv.profit_token == WETH and tv.gross == 10**16
    assert tv.payouts == {COLLECTOR: 3 * 10**15}
    assert HELPER in tv.group and COLLECTOR not in tv.group
    assert (
        LENDER not in tv.group
        and [x.lender for x in tv.flash_loans] == [LENDER]
        and tv.flash_loans[0].fee == 0
    )


def test_pool_fee_recipient_is_not_a_taker():
    # a plain user swap where the pool also forwards its fee to a fee contract
    tr = call(
        EOA,
        ROUTER,
        calls=[
            call(ROUTER, WETH, transfer_from(EOA, POOL_A, 10**18)),
            call(
                ROUTER,
                POOL_A,
                calls=[
                    call(POOL_A, USDC, transfer(EOA, 3000 * 10**6)),
                    call(POOL_A, WETH, transfer(POOLFEES, 3 * 10**15)),
                ],
            ),
        ],
    )
    tv = trace_verdict(tr, WETH)
    assert tv.verdict == "no_taker" and tv.venues == [POOL_A]


def test_flash_loan_lender_reported_with_fee():
    tr = call(
        EOA,
        BOT,
        calls=[
            call(BOT, LENDER, calls=[call(LENDER, WETH, transfer(BOT, 10**18))]),
            call(BOT, WETH, transfer(POOL_A, 10**18)),
            call(BOT, POOL_A, calls=[call(POOL_A, USDC, transfer(BOT, 3000 * 10**6))]),
            call(BOT, USDC, transfer(POOL_B, 3000 * 10**6)),
            call(BOT, POOL_B, calls=[call(POOL_B, WETH, transfer(BOT, 101 * 10**16))]),
            call(BOT, WETH, transfer(LENDER, 10**18 + 5 * 10**14)),  # principal + 0.05 % fee
        ],
    )
    tv = trace_verdict(tr, WETH)
    assert tv.verdict == "cycle" and tv.gross == 10**16
    assert len(tv.flash_loans) == 1
    fl = tv.flash_loans[0]
    assert fl.lender == LENDER and fl.token == WETH and fl.principal == 10**18 and fl.fee == 5 * 10**14
    # the fee left the group: reported as a payout to the lender (ownership unverified), gross stays before payouts
    assert LENDER not in tv.group and tv.payouts == {LENDER: 5 * 10**14}


def test_v4_claims_mint_counts_as_profit():
    PM = "0x498581ff718922c3f8e6a244956af099b2652b2b"
    # bot pays USDC to the singleton, gets WETH, sells WETH on pool B for the same USDC back: transfers net
    # to zero; the 3610 USDC profit stays in the singleton as an ERC-6909 claim (mint log)
    tr = call(
        EOA,
        BOT,
        calls=[
            call(BOT, USDC, transfer(PM, 14754140)),
            call(BOT, PM, calls=[call(PM, WETH, transfer(BOT, 6019693518060392))]),
            call(BOT, WETH, transfer(POOL_B, 6019693518060392)),
            call(BOT, POOL_B, calls=[call(POOL_B, USDC, transfer(BOT, 14754140))]),
        ],
    )
    assert trace_verdict(tr, WETH).verdict == "no_taker"
    logs = [
        {
            "address": PM,
            "topics": [
                "0x1b3d7edb2e9c0b0e7c525b20aaaef0f5940d2ed71663c7d39266ecafac728859",
                "0x" + "0" * 64,
                "0x" + "0" * 24 + BOT[2:],
                "0x" + "0" * 24 + USDC[2:],
            ],
            "data": "0x" + "0" * 24 + BOT[2:] + f"{3610:064x}",
        }
    ]
    # Receipts contain the successful ERC-20 movements as well as the claim mint.
    for currency, sender, receiver, amount in [
        (USDC, BOT, PM, 14754140),
        (WETH, PM, BOT, 6019693518060392),
        (WETH, BOT, POOL_B, 6019693518060392),
        (USDC, POOL_B, BOT, 14754140),
    ]:
        logs.append(
            {
                "address": currency,
                "topics": [
                    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                    "0x" + sender[2:].rjust(64, "0"),
                    "0x" + receiver[2:].rjust(64, "0"),
                ],
                "data": f"0x{amount:064x}",
            }
        )
    tv = trace_verdict(tr, WETH, logs=logs)
    assert tv.verdict == "cycle" and tv.profit_token == USDC and tv.gross == 3610


BLINK = "0x743be0db30148336a3db479f19d4e1828b293869"


def test_native_v4_settlement_and_blink_kickback():
    PM = "0x498581ff718922c3f8e6a244956af099b2652b2b"
    # bot settles 1 ETH natively into the singleton, takes USDC, sells USDC on pool B for 1.01 WETH,
    # pays 75 % of the profit to Blink by an internal ETH call
    tr = call(
        EOA,
        BOT,
        calls=[
            call(BOT, PM, "0x11da60b4", value=10**18),
            call(BOT, PM, calls=[call(PM, USDC, transfer(BOT, 3000 * 10**6))]),
            call(BOT, USDC, transfer(POOL_B, 3000 * 10**6)),
            call(BOT, POOL_B, calls=[call(POOL_B, WETH, transfer(BOT, 101 * 10**16))]),
            call(BOT, BLINK, "0x", value=75 * 10**14),
        ],
    )
    tv = trace_verdict(tr, WETH)
    assert tv.verdict == "cycle" and tv.venues == [PM, POOL_B]
    assert tv.profit_token == WETH and tv.gross == 10**16 and tv.net_after == 25 * 10**14
    assert tv.payouts_by_token == {WETH: {BLINK: 75 * 10**14}}


def test_loss_of_one_and_a_half_percent_is_open_and_dust_entry_does_not_hide_profit():
    # 4-leg cycle that returns 1.5 % less of the entry token: a loss, not a cycle with a fee-collector taker
    tr = call(
        EOA,
        BOT,
        calls=[
            call(BOT, DAI, transfer(POOL_A, 1000 * 10**18)),
            call(BOT, POOL_A, calls=[call(POOL_A, USDC, transfer(BOT, 1000 * 10**6))]),
            call(BOT, USDC, transfer(POOL_B, 999 * 10**6)),
            call(BOT, POOL_B, calls=[call(POOL_B, DAI, transfer(BOT, 985 * 10**18))]),
            call(BOT, USDC, transfer(COLLECTOR, 10**6)),  # 0.1 % router fee
        ],
    )
    assert trace_verdict(tr, WETH).verdict == "no_taker"
    # flash-swap: the pool pays USDC first, 1 unit of USDC dust stays; the real profit is in WETH
    tr = call(
        EOA,
        BOT,
        calls=[
            call(
                BOT,
                POOL_A,
                calls=[
                    call(POOL_A, USDC, transfer(BOT, 279885380)),
                    call(
                        POOL_A,
                        BOT,
                        "0xfa461e33",
                        calls=[call(BOT, WETH, transfer(POOL_A, 114192901607767058))],
                    ),
                ],
            ),
            call(BOT, USDC, transfer(POOL_B, 279885379)),
            call(BOT, POOL_B, calls=[call(POOL_B, WETH, transfer(BOT, 114198524808058538))]),
        ],
    )
    tv = trace_verdict(tr, WETH)
    assert tv.verdict == "cycle" and tv.profit_token == WETH and tv.gross == 5623200291480
    assert tv.extra_gains == {USDC: 1}


def test_pool_to_pool_legs_with_output_to_holding_address():
    HOLD = "0x4014000000000000000000000000000000004014"
    # the bot only orchestrates: pool A pays pool B directly, pool B pays the holding address more WETH than
    # the bot's EOA-funded input that pool A received from a third pool-like source is irrelevant here
    tr = call(
        EOA,
        BOT,
        calls=[
            call(BOT, POOL_A, calls=[call(POOL_A, USDC, transfer(POOL_B, 3000 * 10**6))]),
            call(
                BOT,
                POOL_B,
                calls=[
                    call(POOL_B, WETH, transfer(POOL_A, 10**18)),
                    call(POOL_B, WETH, transfer(HOLD, 10**16)),
                ],
            ),
        ],
    )
    tv = trace_verdict(tr, WETH)
    assert tv.verdict == "cycle" and tv.gross == 10**16 and tv.profit_token == WETH
    assert any(n.startswith("output_redirected_to:") for n in tv.notes)


def test_two_executors_paying_one_collector_are_one_operator():
    EXEC_B = "0x68fa000000000000000000000000000000068fa0"
    POOL_C = "0xcccc00000000000000000000000000000000cccc"
    tr = call(
        EOA,
        ROUTER,
        calls=[
            # cycle A by BOT: WETH -> USDC -> WETH, profit 1e16 to COLLECTOR
            call(BOT, WETH, transfer(POOL_A, 10**18)),
            call(BOT, POOL_A, calls=[call(POOL_A, USDC, transfer(BOT, 3000 * 10**6))]),
            call(BOT, USDC, transfer(POOL_B, 3000 * 10**6)),
            call(BOT, POOL_B, calls=[call(POOL_B, WETH, transfer(BOT, 101 * 10**16))]),
            call(BOT, WETH, transfer(COLLECTOR, 10**16)),
            # cycle B by EXEC_B: WETH -> DAI -> WETH, profit 5e15 to the same COLLECTOR
            call(EXEC_B, WETH, transfer(POOL_C, 2 * 10**18)),
            call(EXEC_B, POOL_C, calls=[call(POOL_C, DAI, transfer(EXEC_B, 6000 * 10**18))]),
            call(EXEC_B, DAI, transfer(POOL_B, 6000 * 10**18)),
            call(EXEC_B, POOL_B, calls=[call(POOL_B, WETH, transfer(EXEC_B, 2005 * 10**15))]),
            call(EXEC_B, WETH, transfer(COLLECTOR, 5 * 10**15)),
        ],
    )
    tv = trace_verdict(tr, WETH)
    assert tv.verdict == "cycle" and tv.gross == 15 * 10**15 and tv.profit_token == WETH
    assert tv.payouts == {COLLECTOR: 15 * 10**15}
    assert BOT in tv.group and EXEC_B in tv.group and tv.other_parties == []


def test_erc4337_user_swap_is_not_a_cycle():
    ENTRY = "0x5ff137d4b0fdcd49dca30c7cf57e578a026d2789"
    BUNDLER = "0x048ef1062cbb39b338ac2685da72adf104b4cef5"
    ACCOUNT = "0xacc0000000000000000000000000000000000acc"
    LIFI = "0x1231deb6f5749ef6ce6943a275a1d3e7486f4eae"
    FEE1 = "0xc06ebbefd94032b85424d51906e2a335efae264b"
    # bundler calls EntryPoint.handleOps; the account swaps 10 DAI -> USDC via a fee-taking router
    tr = call(
        BUNDLER,
        ENTRY,
        "0x1fad948c",
        calls=[
            call(ENTRY, ACCOUNT, "0x3a871cdd"),  # validateUserOp
            call(
                ENTRY,
                ACCOUNT,
                "0xb61d27f6",
                calls=[
                    call(ACCOUNT, DAI, transfer(LIFI, 10 * 10**18)),
                    call(
                        ACCOUNT,
                        LIFI,
                        calls=[
                            call(LIFI, DAI, transfer(FEE1, 10**17)),
                            call(LIFI, DAI, transfer(POOL_A, 99 * 10**17)),
                            call(LIFI, POOL_A, calls=[call(POOL_A, USDC, transfer(LIFI, 9_900_000))]),
                            call(LIFI, USDC, transfer(ACCOUNT, 9_900_000)),
                        ],
                    ),
                ],
            ),
        ],
    )
    tv = trace_verdict(tr, WETH)
    assert tv.verdict == "no_taker"
    assert any(n == f"erc4337_account:{ACCOUNT}" for n in tv.notes)


def test_order_fill_from_an_eoa_is_not_a_cycle():
    # a solver pulls a maker's tokens by allowance, sells them on a pool, pays the maker ETH, keeps a surplus:
    # the maker never receives a call, so it is not a venue and the solver's route is open (settlement, not arb)
    MAKER = "0xeb7c000000000000000000000000000000eb7c00"
    SOLVER = "0x5017e0000000000000000000000000005017e000"
    tr = call(
        EOA,
        SOLVER,
        calls=[
            call(SOLVER, DAI, transfer_from(MAKER, SOLVER, 1000 * 10**18)),
            call(SOLVER, DAI, transfer(POOL_A, 1000 * 10**18)),
            call(SOLVER, POOL_A, calls=[call(POOL_A, WETH, transfer(SOLVER, 5 * 10**17))]),
            call(SOLVER, WETH, transfer(MAKER, 49 * 10**16)),
        ],
    )
    tv = trace_verdict(tr, WETH)
    assert MAKER not in tv.venues
    assert tv.verdict in ("single_venue_loop", "no_taker", "no_venue")


def test_route_fed_from_outside_is_not_a_cycle():
    """BSC 0x640c0286…: a router sells a user's 315 WBNB for 175,654 USDT and pays it back; a 28.9 USDT side swap
    closes a "route" whose second leg takes 6,000x what the first delivered. The route's own rates produce nothing:
    the gain is the user's sale, not an arbitrage."""
    from mevlab.trace.route import route_surplus

    user = "0x5e5e00000000000000000000000000000000005e"
    trace = call(
        EOA,
        BOT,
        calls=[
            call(BOT, USDC, transfer(POOL_A, 29 * 10**6)),
            call(BOT, POOL_A, calls=[call(POOL_A, WETH, transfer(BOT, 52 * 10**15))]),
            # the user's 315 WETH reach the second pool from another contract, never from the operator
            call(BOT, ROUTER, calls=[call(ROUTER, WETH, transfer(POOL_B, 315 * 10**18))]),
            call(BOT, POOL_B, calls=[call(POOL_B, USDC, transfer(BOT, 175_654 * 10**6))]),
            call(BOT, USDC, transfer(user, 175_449 * 10**6)),
        ],
    )
    tv = trace_verdict(trace, WETH)
    assert tv.verdict != "cycle"
    assert any(n.startswith("gain_exceeds_route_surplus") for n in tv.notes)
    # a continuous route: the surplus is the last output minus the first input
    path = [
        {"amount_in": "100", "amount_out": "50"},
        {"amount_in": "50", "amount_out": "103"},
    ]
    assert route_surplus(path) == 3
    # the same rates with the second leg fed 100x from elsewhere still produce only 3
    assert route_surplus([path[0], {"amount_in": "5000", "amount_out": "10300"}]) == 3


def test_split_route_paid_by_the_operator_stays_a_cycle():
    """A route split over two pools: the second hop takes twice what one first-hop pool delivered, but the operator
    paid it from its own other leg, so the gap is not outside money."""
    pool_c = "0xcccc00000000000000000000000000000000cccc"
    trace = call(
        EOA,
        BOT,
        calls=[
            call(BOT, WETH, transfer(POOL_A, 5 * 10**17)),
            call(BOT, POOL_A, calls=[call(POOL_A, USDC, transfer(BOT, 1500 * 10**6))]),
            call(BOT, WETH, transfer(POOL_B, 5 * 10**17)),
            call(BOT, POOL_B, calls=[call(POOL_B, USDC, transfer(BOT, 1500 * 10**6))]),
            call(BOT, USDC, transfer(pool_c, 3000 * 10**6)),
            call(BOT, pool_c, calls=[call(pool_c, WETH, transfer(BOT, 101 * 10**16))]),
        ],
    )
    tv = trace_verdict(trace, WETH)
    assert tv.verdict == "cycle" and tv.gross == 10**16
