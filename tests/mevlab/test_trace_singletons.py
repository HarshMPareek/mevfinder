"""Singleton (Uniswap V4 / Ekubo / Infinity) rules of the trace verifier, from the random-block review rows of
2026-10-08: EIP-7702 searchers calling the PoolManager directly, results paid out by the manager to collectors
(through forwarders), hook fee recipients, and routes the trace order cannot pair."""

from eth_abi import encode

from mevlab.decode.signatures import ERC6909_TRANSFER_TOPIC
from mevlab.trace.verdict import trace_verdict
from tests.mevlab.test_receipt import BOT, EOA, POOL_A, USDC, WETH, t, transfer
from tests.mevlab.test_trace import call

PM = "0x" + "aa" * 20  # singleton pool manager
PM2 = "0x" + "ab" * 20  # a second singleton (Ekubo core)
VAULT = "0x" + "cc" * 20  # the operator's collector
HOOK = "0x" + "dd" * 20  # a pool's registered hook
COINBASE = "0x" + "ee" * 20
FWD1, FWD2 = "0x" + "f1" * 20, "0x" + "f2" * 20
FEE1, FEE2 = "0x" + "fa" * 20, "0x" + "fb" * 20
ZERO = "0x" + "00" * 20
SPLITTER_TOPIC = "0x382c1ddbdd6f153c26f2a003979529bb14fedbaf848e35cb8a347345298bd48b"


def claim_mint(caller, to, currency, amount, li):
    """PoolManager ERC-6909 Transfer(caller, from=0, to, id=currency, amount)."""
    return {
        "address": PM,
        "logIndex": hex(li),
        "topics": [ERC6909_TRANSFER_TOPIC, t(ZERO), t(to), t(currency)],
        "data": "0x" + encode(["address", "uint256"], [caller, amount]).hex(),
    }


def test_eip7702_searcher_calling_the_manager_directly_is_the_operator_not_the_manager():
    """Ethereum 0xba1fed63…: the EOA (with 7702 code) calls PoolManager.unlock itself. The manager pays the
    builder natively and the EOA mints its remaining result to its vault as a claim; the pool's hook mints its
    fee claim to itself. Gross = builder + vault (0.000226), the hook's claim is a fee."""
    trace = call(
        EOA,
        PM,
        calls=[
            call(PM, EOA, "0x91dd7346", calls=[call(EOA, PM, "0x0b0d9c09")]),
            call(PM, COINBASE, value=115),
        ],
    )
    logs = [claim_mint(HOOK, HOOK, ZERO, 112, 1), claim_mint(EOA, VAULT, ZERO, 111, 2)]
    tv = trace_verdict(trace, WETH, EOA, PM, logs=logs, venue_hints={PM}, fee_takers={HOOK}, singletons={PM})
    assert tv.verdict == "gain_only" and tv.profit_token == WETH and tv.gross == 226
    assert tv.redirects == [COINBASE, VAULT] and EOA in tv.group and PM not in tv.group
    assert any(n.startswith("hook_fee_recipients:") and HOOK in n for n in tv.notes)


def test_result_paid_through_forwarders_and_hook_splitter_fee_excluded():
    """Robinhood 0xd0b20d3d… / 0xab17829d…: the manager pays the result to the vault through two pass-through
    helpers, and pays the hook's fee recipients whose receipts add up to the hook's fee event."""
    trace = call(
        EOA,
        BOT,
        calls=[
            call(BOT, PM, "0x48c89491", calls=[call(PM, BOT, "0x91dd7346")]),
            call(BOT, FWD1, "0xabcdef01", calls=[call(FWD1, FWD2, "0xabcdef02")]),
        ],
    )
    logs = [
        transfer(USDC, PM, FWD1, 893, 1),
        transfer(USDC, FWD1, FWD2, 893, 2),
        transfer(USDC, FWD2, VAULT, 893, 3),
        transfer(USDC, PM, FEE1, 12, 4),
        transfer(USDC, PM, FEE2, 48, 5),
        {
            "address": HOOK,
            "logIndex": "0x6",
            "topics": [SPLITTER_TOPIC, t(USDC)],
            "data": "0x" + encode(["uint256"], [60]).hex(),
        },
    ]
    tv = trace_verdict(trace, WETH, EOA, BOT, logs=logs, venue_hints={PM}, singletons={PM})
    assert tv.verdict == "gain_only" and tv.profit_token == USDC and tv.gross == 893
    assert tv.redirects == [VAULT] and {FWD1, FWD2, VAULT} <= set(tv.group)
    assert any("hook_fee_recipients:" in n and FEE1 in n and FEE2 in n for n in tv.notes)


def test_single_payment_from_the_manager_to_a_collector_closes_the_result():
    """Base 0x44679f62…: two V4 pools, the whole result taken from the manager straight to a collector; the
    manager has one movement only and the collector must not be mistaken for the venue."""
    trace = call(EOA, BOT, calls=[call(BOT, PM, "0x48c89491", calls=[call(PM, BOT, "0x91dd7346")])])
    logs = [transfer(USDC, PM, VAULT, 11686, 1)]
    tv = trace_verdict(trace, WETH, EOA, BOT, logs=logs, venue_hints={PM}, singletons={PM})
    assert tv.verdict == "gain_only" and tv.gross == 11686 and tv.redirects == [VAULT]


def test_singleton_only_route_defers_the_route_shape_to_the_events():
    """Ethereum 0xf32f80fd…: Uniswap V4 plus Ekubo, both singletons; takes precede settles so the trace cannot
    pair the legs. The verdict is single_venue_loop (the Swap events establish the pools)."""
    trace = call(EOA, BOT, calls=[call(BOT, PM, "0x48c89491"), call(BOT, PM2, "0x12345678")])
    logs = [
        transfer(WETH, PM, BOT, 100, 1),
        transfer(USDC, BOT, PM, 2000, 2),
        transfer(USDC, PM2, BOT, 2000, 3),
        transfer(WETH, BOT, PM2, 99, 4),
    ]
    tv = trace_verdict(trace, WETH, EOA, BOT, logs=logs, venue_hints={PM, PM2}, singletons={PM, PM2})
    assert tv.verdict == "single_venue_loop" and "singleton_only_route" in tv.notes and tv.gross == 1


def test_singleton_net_flow_legs_pair_a_mixed_route():
    """Robinhood 0xf23ab353…: a V3 pool plus the manager. The manager pays a hook fee first, then receives
    the settle and pays the take: ordered pairing yields USDC→USDC and a WETH leg without input; the net flows
    (USDC in, WETH out) give the returning route."""
    trace = call(EOA, BOT, calls=[call(BOT, POOL_A, "0x128acb08"), call(BOT, PM, "0x48c89491")])
    logs = [
        transfer(USDC, PM, HOOK, 2, 1),
        transfer(WETH, BOT, POOL_A, 100, 2),
        transfer(USDC, POOL_A, BOT, 2000, 3),
        transfer(USDC, BOT, PM, 2000, 4),
        transfer(WETH, PM, BOT, 101, 5),
    ]
    tv = trace_verdict(
        trace, WETH, EOA, BOT, logs=logs, venue_hints={PM, POOL_A}, fee_takers={HOOK}, singletons={PM}
    )
    assert tv.verdict == "cycle" and tv.gross == 1 and tv.profit_token == WETH
    assert [leg["venue"] for leg in tv.cycle_path] == [POOL_A, PM]


def test_claim_minted_by_the_v4_locker_is_the_operators_result():
    """Base 0x6f23699b…: the executor that holds the manager's lock (the Swap event's sender) mints the result
    claim to itself; it is in nobody's movement graph but it is not a hook."""
    from eth_abi import encode as abi_encode

    from mevlab.decode.signatures import SIG_V4_SWAP, event_topic

    LOCKER = "0x" + "1c" * 20
    POOL_ID = "0x" + "ab" * 32
    trace = call(
        EOA,
        BOT,
        calls=[
            call(BOT, LOCKER, "0x11111111", calls=[call(LOCKER, PM, "0x48c89491")]),
            call(BOT, POOL_A, "0x128acb08"),
        ],
    )
    swap = {
        "address": PM,
        "logIndex": "0x1",
        "topics": [event_topic(SIG_V4_SWAP), POOL_ID, t(LOCKER)],
        "data": "0x"
        + abi_encode(
            ["int128", "int128", "uint160", "uint128", "int24", "uint24"],
            [2000, -100, 2**96, 10**20, 0, 3000],
        ).hex(),
    }
    logs = [
        swap,
        transfer(WETH, POOL_A, PM, 100, 2),
        transfer(USDC, PM, POOL_A, 1990, 3),
        claim_mint(LOCKER, LOCKER, USDC, 10, 4),
    ]
    tv = trace_verdict(trace, WETH, EOA, BOT, logs=logs, venue_hints={PM, POOL_A}, singletons={PM})
    assert tv.verdict == "cycle" and tv.gross == 10 and tv.profit_token == USDC and tv.redirects == [LOCKER]


def test_a_deposit_destination_is_not_a_collector():
    """Base 0x052d4542…: USDC minted into a wrapper, redeemed, deposited into a vault and supplied to a lending
    pool; the operator touches nothing. Only a singleton manager can pay out a result the operator never
    funded: folding the pool in as the "collector" closed a 1,488 USDC phantom cycle."""
    ISSUER = "0x" + "b4" * 20
    HOLDER = "0x" + "86" * 20
    POOL = "0x" + "bb" * 20
    WUSD = "0x" + "c9" * 20
    trace = call(
        EOA,
        BOT,
        calls=[
            call(
                BOT,
                HOLDER,
                "0x11111111",
                calls=[call(HOLDER, ISSUER, "0x22222222"), call(HOLDER, POOL, "0x33333333")],
            )
        ],
    )
    logs = [
        transfer(USDC, HOLDER, ISSUER, 1488, 1),
        transfer(WUSD, ZERO, HOLDER, 1488 * 10**12, 2),
        transfer(USDC, ISSUER, HOLDER, 1488, 3),
        transfer(USDC, HOLDER, POOL, 1488, 4),
        {"address": POOL, "logIndex": "0x5", "topics": ["0x" + "55" * 32], "data": "0x"},
    ]
    tv = trace_verdict(trace, WETH, EOA, BOT, logs=logs)
    assert tv.verdict != "cycle" and tv.gross is None


def test_a_venue_paying_a_third_party_from_inventory_is_not_a_collector_when_the_operator_funded_nothing():
    """Robinhood 0x1e7e2d1e…: a keeper triggers a vault that swaps a little and distributes NVDA from its own
    inventory to a beneficiary; the keeper pays nothing in. The earlier rule folded the beneficiary in and
    reported a 0.108 NVDA "cycle"."""
    VAULT = "0x" + "e5" * 20
    BENEFICIARY = "0x" + "d3" * 20
    NVDA = "0x" + "4e" * 20
    HEDGE = "0x" + "4d" * 20
    trace = call(
        EOA,
        BOT,
        calls=[
            call(
                BOT,
                VAULT,
                "0x11111111",
                calls=[call(VAULT, PM, "0x48c89491"), call(VAULT, BENEFICIARY, "0x")],
            )
        ],
    )
    logs = [
        transfer(HEDGE, VAULT, PM, 275, 1),
        transfer(NVDA, PM, VAULT, 1, 2),
        transfer(NVDA, VAULT, BENEFICIARY, 108, 3),
    ]
    tv = trace_verdict(trace, WETH, EOA, BOT, logs=logs, venue_hints={PM}, singletons={PM})
    assert tv.verdict == "gain_no_route" and any(n.startswith("gain_exceeds_route_surplus") for n in tv.notes)


def test_a_route_through_a_settlement_contract_is_a_settlement_not_a_cycle():
    """Ethereum 0x57d2f2c9…: a CoW batch whose user hooks withdraw aWBTC, sell it through the settlement and
    repay the position's USDT debt closed as an 85,025 USDT "cycle". Any call into CoW / 1inch marks the
    transaction settlement_like."""
    from arbkit.chain.spec import load_chain
    from arbkit.scan.trace import apply_trace

    spec = load_chain("ethereum")
    COW = "0x9008d19f58aabd9ed0d60971565aa8510560ab41"
    W = spec.weth
    trace = call(EOA, BOT, calls=[call(BOT, COW, "0x13d79a0b"), call(BOT, POOL_A, "0x128acb08")])
    logs = [
        transfer(W, BOT, POOL_A, 100, 1),
        transfer(USDC, POOL_A, BOT, 2000, 2),
        transfer(USDC, BOT, COW, 2000, 3),
        transfer(W, COW, BOT, 101, 4),
    ]
    row = {
        "tx_hash": "0x" + "cd" * 32,
        "is_arb": True,
        "arb_class": "atomic_cycle",
        "assessment": "event_candidate",
        "n_legs": 2,
        "venues": [POOL_A, COW],
        "event_label": {"venues": [POOL_A, COW], "gross_profit_raw": 1, "profit_token": W},
        "reasons": [],
        "gross_unreliable": False,
        "fees": {"is_system": False},
        "profit_token": W,
        "gross_profit_raw": "1",
        "gross_source": "events",
    }
    apply_trace(
        spec, row, trace, {"from": EOA, "to": BOT, "hash": row["tx_hash"]}, {"status": "0x1", "logs": logs}
    )
    assert row["assessment"] != "trace_supported" and row["arb_class"] == "settlement_like"
    assert any(r.startswith("settlement_contract_called:") for r in row["reasons"])
