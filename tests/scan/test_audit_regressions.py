import io
import json

from rich.console import Console

from arbkit.chain.spec import load_chain
from arbkit.cli.terminal import show_report
from arbkit.scan.block import receipt_route_hint, wants_trace
from arbkit.scan.scanner import summarize
from arbkit.scan.trace import apply_trace
from arbkit.value.proceeds import enrich_proceeds
from mevlab.trace.movements import movements
from mevlab.trace.verdict import trace_verdict
from tests import FIXTURES

SPEC = load_chain("base")
WETH = SPEC.weth


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def test_real_taxed_sr_selects_weth_profit_from_receipts():
    data = fixture("taxed_sr_cycle")
    verdict = trace_verdict(data["trace"], WETH, logs=data["receipt"]["logs"])
    assert verdict.verdict == "cycle"
    assert verdict.profit_token == WETH
    assert verdict.gross == 3_017_558_514_318
    assert verdict.net_after == 3_017_558_514_318
    assert verdict.extra_gains["0x10c56f005a379f8eafc88ff5c3f40d30f0031ac9"] == 1
    # Calldata alone reproduces the old bug; receipt amounts remove the phantom SR gain.
    inferred = trace_verdict(data["trace"], WETH)
    assert inferred.profit_token != WETH
    assert inferred.gross == 2_363_353_712_981_693_648


def test_real_forwarded_profit_not_lost_from_gross_minus_gas():
    data = fixture("forwarded_cycle")
    row = data["row"]
    apply_trace(SPEC, row, data["trace"], data["tx"], data["receipt"])
    enrich_proceeds(row, WETH)
    assert row["assessment"] == "trace_supported"
    assert row["gross_minus_network_fee_wei"] == -18_790_655_820_900
    assert row["fee_coverage"] == "below_network_fee"
    assert row["retained_before_gas_raw"] == "0"
    assert row["forwarded_profit_raw"] == "6659763958592"
    assert row["retained_minus_network_fee_wei"] == -25_450_419_779_492


def test_conversion_accounts_for_subscription_cost_and_direct_pool_mint():
    data = fixture("conversion_route")
    row = data["row"]
    apply_trace(SPEC, row, data["trace"], data["tx"], data["receipt"])
    assert row["assessment"] == "trace_supported"
    assert row["profit_token"] == WETH
    assert row["gross_profit_raw"] == "487621349854188"
    assert row["route_type"] == "swaps_and_token_conversions"
    assert set(row["trace"]["payouts_by_token"]) == {WETH}
    assert "0xf0de996292a195dbb5fc94ff1899781c874a9750" in row["trace"]["venues"]
    assert "0x2dad8b751ad15c4186ea955d6a47b751c66827d7" in row["trace"]["venues"]


def test_empty_receipt_cannot_prove_requested_token_transfer():
    data = fixture("forwarded_cycle")
    observed, reverted = movements(data["trace"], WETH, logs=[])
    assert observed == [] and not reverted
    assert trace_verdict(data["trace"], WETH, logs=[]).verdict == "empty"


def test_tax_receipt_flow_includes_fee_recipient_and_exact_delivered_amount():
    data = fixture("taxed_sr_cycle")
    observed, _ = movements(data["trace"], WETH, logs=data["receipt"]["logs"])
    token = "0x10c56f005a379f8eafc88ff5c3f40d30f0031ac9"
    pool = "0x93e7c445d77e6506b77c869c469e4305c15c075b"
    moved = [m for m in observed if m.token == token and m.sender == pool]
    assert {m.receiver: m.amount for m in moved} == {
        token: 2_363_353_712_981_693_647,
        data["tx"]["to"]: 233_972_017_585_187_671_064,
    }


def test_old_saved_report_displays_corrected_arithmetic_without_network():
    data = fixture("forwarded_cycle")
    row = data["row"]
    report = {
        "classifier_version": "0.4.0-base-port.1",
        "blocks": [51285394],
        "rows": [row],
        "summary": summarize([row]),
        "failures": [],
    }
    output = io.StringIO()
    show_report(report, Console(file=output, width=100, color_system=None))
    text = output.getvalue()
    assert "-0.0000187906558209" in text
    assert "Inferred group retained after gas" not in text
    assert "Forwarded:" in text
    assert "predate the current classifier" in text


def test_unpriced_token_has_no_eth_gas_coverage_guess():
    row = fixture("forwarded_cycle")["row"]
    row["profit_token"] = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    enrich_proceeds(row, WETH)
    assert row["fee_coverage"] == "unpriced_token"
    assert row["gross_minus_network_fee_wei"] is None


def test_positive_forwarded_profit_still_covers_gas_before_payouts():
    row = fixture("forwarded_cycle")["row"]
    row["fees"]["total_fee_wei"] = 1_000_000_000_000
    enrich_proceeds(row, WETH)
    assert row["gross_minus_network_fee_wei"] == 5_659_763_958_592
    assert row["fee_coverage"] == "covers_network_fee"
    assert row["retained_minus_network_fee_wei"] == -1_000_000_000_000


def test_basket_redemption_closes_every_underlying_and_repays_flash_loan():
    data = fixture("basket_redemption")
    row = data["row"]
    apply_trace(SPEC, row, data["trace"], data["tx"], data["receipt"])
    assert row["assessment"] == "trace_supported"
    assert row["profit_token"] == WETH
    assert row["gross_profit_raw"] == "133248890104805"
    assert row["trace"]["extra_gains"] == {}
    assert any(
        loan["principal"] == loan["repaid"] == "25002500000000000" for loan in row["trace"]["flash_loans"]
    )
    assert "0xebcda5b80f62dd4dd2a96357b42bb6facbf30267" in row["trace"]["venues"]


def test_user_purchase_and_burn_is_not_a_closed_conversion_cycle():
    data = fixture("open_mint_route")
    row = data["row"]
    apply_trace(SPEC, row, data["trace"], data["tx"], data["receipt"])
    assert row["assessment"] == "not_arb"
    assert row["trace"]["verdict"] == "no_taker"
    assert row["trace"]["gross"] is None


def test_issuer_self_burn_does_not_create_a_phantom_counterparty():
    data = fixture("conversion_route")
    observed, _ = movements(data["trace"], WETH, logs=data["receipt"]["logs"])
    zero = "0x" + "0" * 40
    assert all(m.sender != m.receiver and zero not in (m.sender, m.receiver) for m in observed)
    issuer = "0xf0de996292a195dbb5fc94ff1899781c874a9750"
    direct = [m for m in observed if m.token == issuer]
    assert len(direct) == 1
    assert direct[0].sender == issuer
    assert direct[0].receiver == "0x2a6120a1c605d7e3713860490de1de282e5d051a"
    assert direct[0].amount == 301842290


def test_receipt_hints_recover_both_single_event_cycles_in_default_mode():
    for name, token, gross in [
        ("claim_settled_cycle", "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", "112275"),
        ("single_event_redemption", WETH, "18169600809042"),
    ]:
        data = fixture(name)
        row = data["row"]
        assert row["n_legs"] == 1
        row["receipt_route_hint"] = receipt_route_hint(data["tx"], data["receipt"], WETH)
        assert row["receipt_route_hint"] and wants_trace(row, "candidates")
        apply_trace(SPEC, row, data["trace"], data["tx"], data["receipt"])
        assert row["assessment"] == "trace_supported"
        assert row["profit_token"] == token and row["gross_profit_raw"] == gross


def test_inventory_loss_is_not_erased_by_one_percent_closure_tolerance():
    data = fixture("no_swap_event_venue")
    row = data["row"]
    apply_trace(SPEC, row, data["trace"], data["tx"], data["receipt"])
    assert row["assessment"] == "trace_supported_loss" and not row["is_arb"]
    assert row["gross_profit_raw"] == "-60243"
    assert row["trace"]["verdict"] == "loss_cycle"
    assert len(row["trace"]["cycle_path"]) == 2
    assert row["trace"]["asset_deltas"]["0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"] == "-60243"
    possible = row["trace"]["unclosed_candidates"][0]
    assert possible["asset_deltas"]["0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf"] == "40"
    assert "receipt_loss_prevents_closure" in row["trace"]["notes"]


def test_claim_forwarding_is_displayed_as_a_claim_not_cash():
    data = fixture("claim_settled_cycle")
    row = data["row"]
    apply_trace(SPEC, row, data["trace"], data["tx"], data["receipt"])
    enrich_proceeds(row, WETH)
    assert len(row["forwarded_claims"]) == 1
    assert row["forwarded_claims"][0]["forwarded_raw"] == "112275"
    report = {
        "classifier_version": "0.4.1-base-port.3",
        "blocks": [51285383],
        "rows": [row],
        "summary": summarize([row]),
        "failures": [],
    }
    output = io.StringIO()
    show_report(report, Console(file=output, width=140, color_system=None))
    assert "Forwarded as ERC-6909 claim:" in output.getvalue()
    assert "Forwarded:" not in output.getvalue()


def test_exact_closure_never_ignores_a_small_receipt_loss():
    from mevlab.trace.operator import closed_gain

    net = {"USDC": -1, "WETH": 10}
    flow = {"USDC": 10_000, "WETH": 10_000}
    assert closed_gain(net, flow, "USDC") is not None
    assert closed_gain(net, flow, "USDC", exact=True) is None
