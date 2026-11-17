import asyncio
import copy
import io
import json

from rich.console import Console

from arbkit.chain.spec import load_chain
from arbkit.cli.terminal import show_report
from arbkit.scan.block import receipt_route_hint
from arbkit.scan.liquidity import liquidity_evidence
from arbkit.scan.scanner import summarize
from arbkit.scan.trace import apply_trace
from arbkit.scan.userops import annotate_operations, operation_outcomes
from arbkit.value.proceeds import enrich_proceeds
from mevlab.pools.meta import DictPoolMeta, PoolMeta
from mevlab.trace.model import Leg
from mevlab.trace.route import returning_route
from tests import FIXTURES

SPEC = load_chain("base")
WETH = SPEC.weth


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def test_failed_user_operation_is_not_probe_or_executed_cycle():
    data = fixture("failed_user_operation")
    row = data["row"]
    annotate_operations(row, data["tx"], data["receipt"])
    apply_trace(SPEC, row, data["trace"], data["tx"], data["receipt"])
    assert data["receipt"]["status"] == "0x1"
    assert row["arb_class"] == "userop_failure" and not row["is_arb"]
    assert row["user_operations"][0]["success"] is False
    assert row["fees"]["total_fee_wei"] == 2466177184252
    assert row["trace"]["verdict"] != "cycle"


def test_mixed_user_operations_are_not_labeled_all_failed():
    data = fixture("failed_user_operation")
    operation = copy.deepcopy(data["receipt"]["logs"][-1])
    operation["data"] = operation["data"][:66] + f"{1:064x}" + operation["data"][130:]
    data["receipt"]["logs"].append(operation)
    annotate_operations(data["row"], data["tx"], data["receipt"])
    assert data["row"]["arb_class"] != "userop_failure"
    assert [op["success"] for op in data["row"]["user_operations"]] == [False, True]


def test_user_operation_topic_from_unrecognized_emitter_is_ignored():
    data = fixture("failed_user_operation")
    data["receipt"]["logs"][-1]["address"] = "0x" + "12" * 20
    assert operation_outcomes(data["tx"], data["receipt"]) == []


def test_vault_redemption_is_not_mistaken_for_a_losing_cycle():
    data = fixture("eurc_redemption")
    row = data["row"]
    apply_trace(SPEC, row, data["trace"], data["tx"], data["receipt"])
    assert row["assessment"] == "not_arb"
    assert row["trace"]["verdict"] == "no_taker"


def test_embedded_hook_cycle_does_not_absorb_outer_user_trade():
    data = fixture("embedded_cycle")
    row = data["row"]
    apply_trace(SPEC, row, data["trace"], data["tx"], data["receipt"])
    enrich_proceeds(row, WETH)
    assert row["arb_class"] == "embedded_cycle"
    assert row["gross_profit_raw"] == "181070"
    assert row["outer_trace"]["asset_deltas"][WETH] == "-3000000000000000"
    assert data["tx"]["from"] not in row["trace"]["group"]
    assert data["tx"]["to"] not in row["trace"]["group"]
    assert row["fee_coverage"] == "shared_transaction_fee"
    assert row["gross_minus_network_fee_wei"] is None
    assert row["trace"]["flash_loans"][0]["principal"] == "400000000"
    assert row["trace"]["flash_loans"][0]["repaid"] == "400000000"


def test_fee_retaining_router_cut_is_not_scored_as_a_cycle():
    """A user's one-way cbBTC -> WETH -> token swap through a router that keeps 0.1 % of the output. The earlier
    audit scored the router's cut (141956900128427210 raw) as a recovered executor cycle; under the closed-chain
    rule a fee retention is not an arbitrage: the router is a pass-through helper of the route, the route never
    returns to its entry asset, and the user's trade stays a split route."""
    data = fixture("fee_retaining_router")
    row = data["row"]
    apply_trace(SPEC, row, data["trace"], data["tx"], data["receipt"])
    assert row["assessment"] != "trace_supported"
    assert row["arb_class"] == "split_route"
    assert any(n.startswith("pass_through_helpers:0x2d3524b9") for n in row["trace"]["notes"])


def test_native_quote_hint_covers_missing_native_leg():
    data = fixture("native_quote_cycle")
    assert receipt_route_hint(data["tx"], data["receipt"], WETH)
    row = data["row"]
    apply_trace(SPEC, row, data["trace"], data["tx"], data["receipt"])
    assert row["gross_profit_raw"] == "15562219960158"


def test_single_venue_or_one_way_route_is_not_a_cycle():
    assert returning_route([Leg("pool", "A", 10, "B", 20)], "A") == []
    assert returning_route([Leg("pool", "A", 10, "B", 20), Leg("pool", "B", 20, "A", 9)], "A") == []
    assert len(returning_route([Leg("p1", "A", 10, "B", 20), Leg("p2", "B", 20, "A", 9)], "A")) == 2


def test_v2_lp_mint_is_capital_deposited_not_swap_profit():
    data = fixture("v2_liquidity")
    pool = "0x7810f8f3253ced11e8c56c6c8497f3134055d44c"
    aero = "0x940181a94a35a4569e4529a3cdfb74e38fd98631"
    other = "0xf544251d25f3d243a36b07e7e7962a678f952691"
    evidence = asyncio.run(
        liquidity_evidence(data["receipt"]["logs"], DictPoolMeta({pool: PoolMeta(aero, other)}))
    )
    assert evidence["kind"] == "provision"
    assert evidence["deposited_raw"] == {aero: "288742479655231285", other: "5699287989294395020"}
    assert evidence["collected_raw"] == {}
    assert not evidence["position_valuation_included"]


def test_losing_cycle_dashboard_is_resolved_with_separate_fees():
    data = fixture("no_swap_event_venue")
    row = data["row"]
    apply_trace(SPEC, row, data["trace"], data["tx"], data["receipt"])
    row.update(profit_symbol="USDC", profit_decimals=6, gross_profit="-0.060243")
    report = {
        "classifier_version": "0.4.2-base-port.6",
        "rows": [row],
        "blocks": [51285400],
        "summary": summarize([row]),
        "failures": [],
    }
    output = io.StringIO()
    show_report(report, Console(file=output, width=160, color_system=None))
    assert "1 losing cycles" in output.getvalue()
    assert "Cycle result before gas: -0.060243 USDC" in output.getvalue()
    assert report["summary"]["trace_supported_fee_wei"] == 0
    assert report["summary"]["loss_cycle_fee_wei"] == 10087597618128
    assert row["fee_coverage"] == "loss_before_gas"


def test_unknown_user_trade_resolves_without_claiming_internal_flow_is_user_profit():
    data = fixture("routed_swap_sideflows")
    row = data["row"]
    apply_trace(SPEC, row, data["trace"], data["tx"], data["receipt"])
    assert row["assessment"] == "not_arb"
    assert row["arb_class"] == "routed_swap"
    assert row["event_label"]["arb_class"] == "unknown"
    assert row["trace"]["group_asset_deltas"]["0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"] == "-150000000"
    assert (
        row["trace"]["group_asset_deltas"]["0x908b1d6b6c12bfdfcaaeb658b365ce39c21e679c"]
        == "59876907064697266390277"
    )


def test_v3_rebalance_tracks_returned_capital_and_replacement_position():
    data = fixture("v3_rebalance")
    resolver = DictPoolMeta({k: PoolMeta(*v) for k, v in data["pool_meta"].items()})
    e = asyncio.run(liquidity_evidence(data["receipt"]["logs"], resolver))
    assert e["kind"] == "rebalance"
    assert {x["kind"] for x in e["events"]} == {"mint", "burn", "collect"}
    assert len(e["position_transfers"]) == 2
    assert int(e["collected_raw"][WETH]) == 62894094150794172
    assert int(e["deposited_raw"][WETH]) == 56392197522385213
    assert e["collect_is_not_profit"]


def test_v4_liquidity_keeps_liquidity_units_separate_from_token_amounts():
    data = fixture("v4_liquidity")
    e = asyncio.run(liquidity_evidence(data["receipt"]["logs"], DictPoolMeta({})))
    assert e["kind"] == "provision"
    assert e["events"][0]["kind"] == "v4_modify"
    assert int(e["events"][0]["liquidity_delta"]) > 0
    assert e["deposited_raw"] == {}
