"""Regression cases for lost payouts and cash/claim attribution on Robinhood."""

import pytest

from mevlab.trace.cashflows import CLAIM_TRANSFER, TRANSFER, cashflow_evidence, native_flows
from mevlab.trace.verdict import trace_verdict

ZERO, BOT, WETH, COLLECTOR, POOL, TOKEN = ("0x" + f"{i:040x}" for i in range(6))


def frame(sender, receiver, value=0, children=None, kind="CALL", **extra):
    return {
        "type": kind,
        "from": sender,
        "to": receiver,
        "value": hex(value),
        "calls": children or [],
        **extra,
    }


def transfer_log(token, sender, receiver, value):
    return {
        "address": token,
        "topics": [TRANSFER, f"0x{int(sender, 16):064x}", f"0x{int(receiver, 16):064x}"],
        "data": f"0x{value:064x}",
    }


def test_direct_weth_native_payout_survives_failed_route_classification():
    trace = frame(BOT, WETH, children=[frame(WETH, COLLECTOR, 100)])
    assert trace_verdict(trace, WETH).gross is None
    evidence = cashflow_evidence(trace, [], WETH, {COLLECTOR: "known"})
    assert evidence["known_recipient_receipts"][COLLECTOR]["eth_weth_received_wei"] == "100"
    assert evidence["native_net_wei"][COLLECTOR] == "100"
    assert evidence["owner_attribution_verified"] is False


def test_successful_receipt_with_failed_trace_is_unknown_not_zero():
    trace = frame(BOT, WETH, error="execution reverted")
    with pytest.raises(ValueError, match="cashflows are unknown"):
        cashflow_evidence(trace, [], WETH, {COLLECTOR: "known"}, receipt_status="0x1")


def test_reverted_subtrees_and_delegatecall_value_are_not_payments():
    trace = frame(
        BOT,
        POOL,
        children=[
            frame(POOL, COLLECTOR, 7),
            frame(POOL, COLLECTOR, 11, error="execution reverted", children=[frame(POOL, COLLECTOR, 13)]),
            frame(POOL, COLLECTOR, 17, kind="DELEGATECALL"),
            frame(POOL, COLLECTOR, 19, kind="STATICCALL"),
        ],
    )
    assert native_flows(trace) == [(POOL, COLLECTOR, 7)]
    trace["error"] = "execution reverted"
    assert native_flows(trace) == []
    result = cashflow_evidence(trace, [transfer_log(WETH, BOT, COLLECTOR, 50)], WETH, {COLLECTOR: "known"})
    assert result["known_recipient_receipts"][COLLECTOR]["eth_weth_received_wei"] == "0"


def test_logs_not_requested_calldata_establish_token_movements():
    # A token may return false without reverting; transfer calldata alone is insufficient.
    call_input = "0xa9059cbb" + f"{int(COLLECTOR, 16):064x}{100:064x}"
    trace = frame(BOT, WETH, input=call_input, output="0x" + "0" * 64)
    result = cashflow_evidence(trace, [], WETH, {COLLECTOR: "known"})
    assert result["erc20_net_raw"] == {}
    assert result["known_recipient_receipts"][COLLECTOR]["weth_raw"] == "0"


def test_claims_and_wrapping_are_separate_from_cash_and_not_double_counted():
    claim = {
        "address": POOL,
        "topics": [
            CLAIM_TRANSFER,
            f"0x{int(ZERO, 16):064x}",
            f"0x{int(COLLECTOR, 16):064x}",
            f"0x{int(WETH, 16):064x}",
        ],
        "data": f"0x{int(BOT, 16):064x}{200:064x}",
    }
    logs = [transfer_log(WETH, BOT, ZERO, 100), claim]
    trace = frame(BOT, WETH, children=[frame(WETH, COLLECTOR, 100)])
    result = cashflow_evidence(trace, logs, WETH, {COLLECTOR: "known"})
    assert result["known_recipient_receipts"][COLLECTOR]["eth_weth_received_wei"] == "100"
    assert result["erc20_net_raw"][BOT][WETH] == "-100"
    assert next(r["delta_raw"] for r in result["erc6909_net_raw"] if r["address"] == COLLECTOR) == "200"


def test_receipts_and_net_distinguish_refunds():
    trace = frame(BOT, POOL, children=[frame(POOL, COLLECTOR, 100), frame(COLLECTOR, POOL, 30)])
    result = cashflow_evidence(trace, [transfer_log(WETH, BOT, COLLECTOR, 20)], WETH, {COLLECTOR: "known"})
    record = result["known_recipient_receipts"][COLLECTOR]
    assert record["eth_weth_received_wei"] == "120"
    assert record["native_net_wei"] == "70"
    assert record["weth_net_raw"] == "20"


def test_unclosed_gain_retains_inferred_group_and_payment_evidence():
    def send(currency, sender, receiver, value):
        return frame(sender, currency, input="0xa9059cbb" + f"{int(receiver, 16):064x}{value:064x}")

    trace = frame(
        ZERO,
        BOT,
        children=[
            send(TOKEN, BOT, POOL, 100),
            frame(BOT, POOL, children=[send(WETH, POOL, BOT, 80)]),
            send(WETH, BOT, COLLECTOR, 20),
        ],
    )
    verdict = trace_verdict(trace, WETH)
    assert verdict.gross is None
    assert BOT in verdict.group
    assert verdict.payouts_by_token[WETH][COLLECTOR] == 20
