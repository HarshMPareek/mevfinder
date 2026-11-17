"""Declared RFQ quote venues (chain.toml `[quote_venues]`): a market maker whose inventory a declared settlement
contract moved by allowance inside its own frame is that contract's leg, so a pool-versus-quote take closes as a
cycle. Undeclared settlements keep the maker as an uncalled counterparty (reported, not counted)."""

from mevlab.trace.verdict import trace_verdict
from tests import FIXTURES
from tests.mevlab.test_receipt import BOT, EOA, POOL_A, USDC, WETH, transfer
from tests.mevlab.test_trace import call, transfer_from

SETTLE = "0x" + "5e" * 20  # RFQ settlement contract (called by the bot)
MAKER = "0x" + "3a" * 20  # market maker wallet (never called; tokens move by allowance)
PERMIT2 = "0x000000000022d473030f116ddee9f6b43ac78ba3"


def pool_vs_quote_trace(settlement_pulls_through_permit2: bool = False):
    """Bot pays 1 WETH to POOL_A for 3000 USDC, then takes the maker's quote: 3000 USDC -> 1.01 WETH, settled by
    SETTLE pulling both sides by allowance (optionally through Permit2)."""
    pull = (
        [
            call(
                SETTLE,
                PERMIT2,
                "0x" + "ab" * 4,
                calls=[
                    call(PERMIT2, USDC, transfer_from(BOT, MAKER, 3000 * 10**6)),
                    call(PERMIT2, WETH, transfer_from(MAKER, BOT, 101 * 10**16)),
                ],
            )
        ]
        if settlement_pulls_through_permit2
        else [
            call(SETTLE, USDC, transfer_from(BOT, MAKER, 3000 * 10**6)),
            call(SETTLE, WETH, transfer_from(MAKER, BOT, 101 * 10**16)),
        ]
    )
    return call(
        EOA,
        BOT,
        calls=[
            call(BOT, WETH, "0xa9059cbb" + POOL_A[2:].rjust(64, "0") + hex(10**18)[2:].rjust(64, "0")),
            call(
                BOT,
                POOL_A,
                calls=[
                    call(
                        POOL_A,
                        USDC,
                        "0xa9059cbb" + BOT[2:].rjust(64, "0") + hex(3000 * 10**6)[2:].rjust(64, "0"),
                    )
                ],
            ),
            call(BOT, SETTLE, "0xdeadbeef", calls=pull),
        ],
    )


LOGS = [
    transfer(WETH, BOT, POOL_A, 10**18, 0),
    transfer(USDC, POOL_A, BOT, 3000 * 10**6, 1),
    transfer(USDC, BOT, MAKER, 3000 * 10**6, 2),
    transfer(WETH, MAKER, BOT, 101 * 10**16, 3),
]


def test_undeclared_settlement_keeps_the_maker_as_an_uncalled_counterparty():
    tv = trace_verdict(pool_vs_quote_trace(), WETH, EOA, BOT, logs=LOGS)
    assert tv.verdict != "cycle"
    assert any(n == f"uncalled_counterparties:{MAKER}" for n in tv.notes)
    assert not any(n.startswith("rfq_quote_leg:") for n in tv.notes)


def test_declared_settlement_turns_the_pool_vs_quote_take_into_a_cycle():
    tv = trace_verdict(pool_vs_quote_trace(), WETH, EOA, BOT, logs=LOGS, quote_venues={SETTLE})
    assert tv.verdict == "cycle" and tv.profit_token == WETH and tv.gross == 10**16
    assert tv.venues == [POOL_A, SETTLE]
    assert f"rfq_quote_leg:{SETTLE}:{MAKER}" in tv.notes
    assert not any(n.startswith("uncalled_counterparties:") for n in tv.notes)
    assert [lg["venue"] for lg in tv.cycle_path] == [POOL_A, SETTLE]


def test_permit2_pull_inside_the_settlement_frame_is_still_the_settlement_leg():
    tv = trace_verdict(pool_vs_quote_trace(True), WETH, EOA, BOT, logs=LOGS, quote_venues={SETTLE})
    assert tv.verdict == "cycle" and tv.gross == 10**16 and SETTLE in tv.venues and MAKER not in tv.group


def test_a_declared_venue_that_was_not_called_changes_nothing():
    """The declaration never promotes a maker on its own: the settlement must be the frame that moved the tokens."""
    tv = trace_verdict(pool_vs_quote_trace(), WETH, EOA, BOT, logs=LOGS, quote_venues={MAKER})
    assert tv.verdict != "cycle" and any(n.startswith("uncalled_counterparties:") for n in tv.notes)


def test_intent_fill_event_in_the_receipt_makes_the_row_a_settlement():
    """UniswapX `Fill` (and 1inch OrderFilled / CoW Trade) in the receipt: the transaction executes someone's order.
    The filler's surplus is never a cycle, even when the trace closes one (Ethereum 0x242c339b…, three Dutch-order
    fills whose 502-sat surplus was sold for WETH)."""
    import json

    from arbkit.chain.spec import load_chain
    from arbkit.scan.trace import apply_trace
    from mevlab.decode.signatures import SIG_UNISWAPX_FILL, event_topic

    data = json.loads((FIXTURES / "forwarded_cycle.json").read_text())
    spec = load_chain("base")
    row, receipt = data["row"], data["receipt"]
    apply_trace(spec, dict(row), data["trace"], data["tx"], receipt)
    reactor = "0x" + "6e" * 20
    fill = {
        "address": reactor,
        "logIndex": hex(len(receipt["logs"])),
        "data": "0x" + "00" * 32,
        "topics": [
            event_topic(SIG_UNISWAPX_FILL),
            "0x" + "11" * 32,
            "0x" + "00" * 12 + "22" * 20,
            "0x" + "00" * 12 + "33" * 20,
        ],
    }
    filled = dict(row)
    apply_trace(spec, filled, data["trace"], data["tx"], {**receipt, "logs": [*receipt["logs"], fill]})
    assert filled["assessment"] != "trace_supported" and filled["arb_class"] == "settlement_like"
    assert "intent_fill_event:Fill" in filled["reasons"]
