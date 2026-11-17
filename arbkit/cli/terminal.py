from __future__ import annotations

from decimal import Decimal

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from arbkit.value.fees import units
from arbkit.value.proceeds import enrich_report
from mevlab.version import CLASSIFIER_VERSION

STYLES = {
    "trace_supported": "green",
    "trace_supported_loss": "red",
    "event_candidate": "yellow",
    "needs_review": "magenta",
    "not_arb": "dim",
}
RESULT = {
    "trace_supported": "positive",
    "trace_supported_loss": "loss",
    "event_candidate": "candidate",
    "needs_review": "review",
}


def usd(value):
    return "—" if value is None else f"{value:,.2f}"


def show_report(report, console: Console, show_all=False, limit=30, focus=None, classes=None):
    # Old reports can be viewed with corrected proceeds labels without a rescan.
    enrich_report(report)
    chain = report.get("chain", "base")
    native = report.get("native_symbol", "ETH")
    summary = report["summary"]
    counts = summary["assessments"]
    mev = summary.get("mev_types", {})
    console.print(
        Panel(
            Text(
                f"{chain.upper()} ARBITRAGE CLASSIFIER   ·   chain {report.get('chain_id', '?')}   ·   {report['classifier_version']}\n"
                f"{len(report['blocks'])} blocks  |  {summary['transactions']:,} transactions  |  "
                f"{counts.get('trace_supported', 0)} positive cycles  |  "
                f"{counts.get('trace_supported_loss', 0)} losing cycles  |  "
                f"{counts.get('event_candidate', 0)} event candidates  |  {counts.get('needs_review', 0)} need review  |  "
                f"{summary.get('sandwiches', 0)} sandwiches\n"
                f"Positions: {mev.get('atomic_backrun', 0)} backrun · {mev.get('atomic_top_of_block', 0)} top-of-block · "
                f"{mev.get('atomic_top_of_flashblock', 0)} top-of-flashblock · {mev.get('atomic_mid_block', 0)} mid-block · "
                f"{mev.get('embedded_cycle', 0)} embedded\n"
                f"Trace-supported cycle fees: {units(summary['trace_supported_fee_wei'])} {native}  |  "
                f"priority portion: {units(summary['trace_supported_priority_fee_wei'])} {native}  |  "
                f"builder tips: {units(summary.get('builder_tips_wei', 0))} {native}\n"
                f"USD (trace-supported): gross {usd(summary.get('gross_usd_trace_supported'))} · "
                f"net after fees and tips {usd(summary.get('net_usd_trace_supported'))} · {summary.get('unpriced_cycles', 0)} unpriced"
            ),
            border_style="cyan",
        )
    )
    coverage = summary["fee_coverage"]
    if report["classifier_version"] != CLASSIFIER_VERSION:
        console.print(
            "Saved classifications predate the current classifier; rescan these blocks to apply the current rules.",
            style="yellow",
        )
    console.print(
        f"Gross vs network fee: {coverage['covers_network_fee']} cover gas; "
        f"{coverage['below_network_fee']} below gas; {coverage['break_even']} break even; "
        f"{coverage['unpriced_token']} need token pricing.",
        markup=False,
    )
    if counts.get("trace_supported_loss"):
        console.print(
            f"Losing-cycle network fees: {units(summary['loss_cycle_fee_wei'])} {native} | priority: "
            f"{units(summary['loss_cycle_priority_fee_wei'])} {native}",
            markup=False,
        )
    if coverage.get("shared_transaction_fee"):
        console.print(
            f"{coverage['shared_transaction_fee']} embedded cycles have no assigned share of the transaction gas fee.",
            markup=False,
        )
    if report["failures"]:
        console.print("INCOMPLETE SCAN — some blocks failed; see the saved report.", style="red")
        for failure in report["failures"]:
            console.print(f"  Block {failure['block']}: {failure['error']}", markup=False)
    if summary["incomplete_fees"]:
        console.print(
            f"{summary['incomplete_fees']} transactions have unknown fee components; displayed fee sums are partial.",
            style="yellow",
        )
    if summary["trace_unavailable"]:
        console.print(
            f"{summary['trace_unavailable']} traces unavailable. Event candidates are not trace-verified.",
            style="yellow",
        )
    class_counts = ", ".join(
        f"{key}: {value}" for key, value in sorted(summary["classes"].items(), key=lambda x: -x[1])
    )
    console.print(class_counts, markup=False)
    rows = report["rows"]
    if focus:
        rows = [row for row in rows if row["tx_hash"].lower() == focus.lower()]
        if not rows:
            raise ValueError("Transaction is not in this report.")
    elif classes:
        rows = [row for row in rows if row["arb_class"] in classes or row.get("mev_type") in classes]
    elif not show_all:
        rows = [row for row in rows if row["assessment"] != "not_arb" or row.get("sandwich")]
    if classes and not focus:
        fees = sum(row["fees"]["total_fee_wei"] or 0 for row in rows)
        priority = sum(row["fees"]["priority_fee_wei"] or 0 for row in rows)
        incomplete = sum(not row["fees"]["complete"] for row in rows)
        console.print(
            f"Selected classes: {', '.join(classes)} | {len(rows)} transactions | "
            f"network fees {units(fees)} {native} | priority {units(priority)} {native}"
            + (" (partial: missing fee components)" if incomplete else ""),
            markup=False,
        )
    table = Table(title="Transactions", expand=True)
    for name in ["Block / index", "Result", "MEV type", "Position", "Gross", f"Fee {native} ≈", "Net USD"]:
        table.add_column(name, overflow="fold")
    for row in rows[:limit]:
        result = RESULT.get(row["assessment"], row["arb_class"])
        sandwich = row.get("sandwich")
        if sandwich and row["assessment"] == "not_arb":
            result = f"sandwich {sandwich['role']}"
        gross = ""
        if row.get("gross_profit") and row.get("profit_symbol"):
            gross = f"{Decimal(row['gross_profit']):.6g} {row['profit_symbol']}"
        elif row.get("gross_profit_raw"):
            gross = f"{row['gross_profit_raw']} raw"
        if sandwich and sandwich.get("role") == "back" and sandwich.get("profit"):
            gross = f"{Decimal(sandwich['profit']):.6g} {sandwich.get('profit_symbol') or '?'} (bundle)"
        position = row.get("trigger_type") or ""
        if row.get("trigger_distance_blocks"):
            position += f" (-{row['trigger_distance_blocks']} blk)"
        net = row.get("net_usd")
        if sandwich and sandwich.get("role") == "back":
            net = sandwich.get("net_usd")
        table.add_row(
            f"{row['block_number']} / {row['tx_index']}",
            Text(result, style=STYLES.get(row["assessment"], "white")),
            row.get("mev_type", "none"),
            position,
            gross,
            format(Decimal(row["fees"]["total_fee_eth"]), ".5g") if row["fees"]["complete"] else "unknown",
            usd(net),
        )
    if rows:
        console.print(table)
        for row in rows[:limit]:
            show_transaction(row, console, report, detailed=bool(focus))
    else:
        console.print("No matching transactions. Use --all to inspect every classification.")
    if len(rows) > limit:
        console.print(
            f"Showing {limit} of {len(rows)} matching transactions. All rows are saved; use --limit to show more."
        )
    console.print(
        "Trace support is evidence of a closed cycle; it does not prove profitability after all costs. Positions are inferred from block order.",
        style="dim",
    )


def show_transaction(row, console, report=None, detailed=False):
    report = report or {}
    native = report.get("native_symbol", "ETH")
    explorer = report.get("explorer_tx", "https://basescan.org/tx/")
    fee = row["fees"]
    console.print(
        f"\nBlock {row['block_number']} · transaction index {row['tx_index']} · {row['assessment'].replace('_', ' ')} · {row['arb_class']} · {row.get('mev_type', 'none')}",
        style="bold",
        markup=False,
    )
    # Full, copyable hashes and URLs: never abbreviate transaction identifiers.
    console.print(row["tx_hash"], style="cyan", soft_wrap=True, markup=False)
    console.print(row["explorer_url"], soft_wrap=True, markup=False)
    l1 = f"L1 {units(fee['l1_fee_wei'])}" + (
        " (included in execution)" if fee.get("l1_fee_included_in_execution") else ""
    )
    console.print(
        f"Total {fee['total_fee_eth']} {native} | execution {units(fee['execution_fee_wei'])} | "
        f"{l1} | operator {units(fee['operator_fee_wei'])}",
        markup=False,
    )
    console.print(
        f"Priority paid {units(fee['priority_fee_wei'])} {native} "
        f"({fee['priority_fee_per_gas_gwei']} gwei/gas) | "
        f"priority cap {units(fee['max_priority_fee_per_gas_wei'], 9)} gwei/gas | gas used {fee['gas_used']}",
        markup=False,
    )
    if int(row.get("builder_tip_wei") or 0):
        console.print(
            f"Builder/validator payment inside the transaction: {units(row['builder_tip_wei'])} {native}"
            + (f" (≈ ${row['builder_tip_usd']:,.2f})" if row.get("builder_tip_usd") is not None else ""),
            markup=False,
        )
    if fee.get("timeboosted"):
        console.print("Timeboost express-lane transaction (receipt flag).", style="dim")
    if row["arb_class"] == "probe":
        console.print(
            "Probe heuristic: successful low-value call without external/token logs. "
            "This does not prove simulation spam or an executed cycle.",
            style="dim",
        )
    elif row["arb_class"] == "reverted":
        console.print(
            "Transaction reverted: its state changes were rolled back; the network fee was paid. "
            "Arbitrage intent is unverified.",
            style="dim",
        )
    elif row["arb_class"] == "system_deposit":
        console.print("System/deposit transaction; excluded from bot/probe counts.", style="dim")
    if row.get("user_operations"):
        failed = sum(not op["success"] for op in row["user_operations"])
        console.print(
            f"User operations: {len(row['user_operations']) - failed} succeeded, {failed} failed. "
            "Outer transaction status is separate.",
            markup=False,
        )
        if row["arb_class"] == "userop_failure":
            console.print(
                "The reported user operations reverted. Their rolled-back swaps are not executed arbitrage.",
                style="dim",
            )
        if detailed:
            for op in row["user_operations"]:
                console.print(
                    f"UserOp {op['user_op_hash']} | sender {op['sender']} | success {op['success']} | "
                    f"EntryPoint charge {units(op['actual_gas_cost_wei'])} {native}",
                    markup=False,
                    soft_wrap=True,
                )
            console.print(
                "EntryPoint operation charges are separate accounting; do not add them to the transaction network fee.",
                style="dim",
            )
    if row.get("profit_token") and row.get("gross_profit_raw") is not None:
        amount = row.get("gross_profit") or row["gross_profit_raw"] + " raw units"
        label = (
            "Cycle result before gas"
            if row["assessment"] == "trace_supported_loss"
            else "Event flow difference"
            if row["assessment"] == "not_arb"
            else f"Gross ({row['gross_source']})"
        )
        valued = ""
        if row.get("gross_usd") is not None:
            valued = f" ≈ ${row['gross_usd']:,.2f} [{row.get('price_source')}]"
        elif row.get("price_source") == "unpriced" and row["assessment"] in {
            "trace_supported",
            "trace_supported_loss",
        }:
            valued = " [unpriced]"
        console.print(
            f"{label}: {amount} {row.get('profit_symbol') or row['profit_token']}{valued}", markup=False
        )
        if row["gross_source"] == "events" and (
            row["gross_unreliable"] or any("unverified" in x for x in row["reasons"])
        ):
            console.print("Gross is uncertain: this hook needs trace accounting.", style="yellow")
    for address, info in (row.get("known_recipient_payments") or {}).items():
        console.print(
            f"Known recipient {info['name']} ({info['kind']}) received {units(info['eth_weth_received_wei'])} {native}: {address}",
            markup=False,
            soft_wrap=True,
        )
    if int(row.get("kickback_wei") or 0):
        console.print(
            f"Order-flow kickbacks to known recipients: {units(row['kickback_wei'])} {native}"
            + (f" (≈ ${row['kickback_usd']:,.2f})" if row.get("kickback_usd") is not None else "")
            + " — counted as a cost.",
            markup=False,
        )
    if row.get("net_usd") is not None:
        console.print(
            f"Net after network fee, builder tip and kickbacks: ${row['net_usd']:,.2f}",
            style="green" if row["net_usd"] > 0 else "red",
            markup=False,
        )
    sandwich = row.get("sandwich")
    if sandwich:
        console.print(
            f"Sandwich bundle {sandwich['bundle_id']}: role {sandwich['role']} on pool {sandwich['pool']} | operator {sandwich['operator']} ({sandwich['operator_kind']})",
            markup=False,
            soft_wrap=True,
        )
        if sandwich["role"] == "back":
            profit = sandwich.get("profit") or sandwich.get("profit_raw")
            valued = f" ≈ ${sandwich['profit_usd']:,.2f}" if sandwich.get("profit_usd") is not None else ""
            console.print(
                f"Operator result over front + back: {profit} {sandwich.get('profit_symbol') or sandwich.get('profit_token')}{valued} | "
                f"bundle fees {units(sandwich.get('bundle_fee_wei'))} {native} | tips {units(sandwich.get('bundle_builder_tip_wei'))} {native}"
                + (f" | net ≈ ${sandwich['net_usd']:,.2f}" if sandwich.get("net_usd") is not None else ""),
                markup=False,
            )
            if not sandwich.get("native_from_traces"):
                console.print(
                    "Native value of the bundle legs was not traced; the operator result counts token transfers only.",
                    style="dim",
                )
            for victim in sandwich.get("victims", []):
                console.print(f"  victim {explorer}{victim}", markup=False, soft_wrap=True)
    if row["arb_class"] == "routed_swap":
        console.print(
            "Trace resolves a one-way token exchange with side flows; no closed operator gain.", style="dim"
        )
        for token, raw in row["trace"].get("group_asset_deltas", {}).items():
            console.print(f"Net token change: {raw} raw {token}", markup=False)
    if row.get("operator_scope") == "embedded":
        console.print(
            f"Embedded operator cycle: {row['trace']['taker']}. The outer user trade is separate.",
            markup=False,
        )
        console.print(
            "Displayed network fee covers the entire transaction; this operator's gas share is unknown.",
            style="dim",
        )
    if row.get("route_type") == "swaps_and_token_conversions":
        console.print(
            "Route includes token issuance/redemption; collateral and minted/burned shares are included.",
            style="dim",
        )
    if row.get("gross_minus_network_fee_wei") is not None:
        delta = row["gross_minus_network_fee_wei"]
        console.print(
            f"Gross minus network fee (before payouts): {units(delta)} {native}",
            style="red" if delta < 0 else "green",
            markup=False,
        )
        if delta < 0:
            console.print("Cycle proceeds did not cover this transaction's network fee.", style="red")
    elif row.get("fee_coverage") == "unpriced_token":
        console.print(
            f"Gas coverage in {native}: profit token is not the native coin; see the USD valuation above.",
            style="dim",
        )
    if row["assessment"] == "trace_supported_loss":
        console.print(
            "Executed cycle lost inventory before gas; external recipient credits are separate.", style="red"
        )
        for leg in row["trace"].get("cycle_path", []):
            console.print(
                f"  {leg['venue']}: {leg['amount_in']} raw {leg['token_in']} → {leg['amount_out']} raw {leg['token_out']}",
                markup=False,
            )
        for payment in row["trace"].get("venue_terminal_receipts", []):
            console.print(
                f"External venue payment: {payment['amount_raw']} raw {payment['token']} → {payment['recipient']} (ownership unverified)",
                markup=False,
            )
    elif "receipt_loss_prevents_closure" in row["trace"].get("notes", []):
        console.print(
            "Open inventory loss: receipt balances do not establish positive closed arbitrage.",
            style="yellow",
        )
        for token, raw in row["trace"].get("asset_deltas", {}).items():
            console.print(f"Venue-flow change: {raw} raw units of {token}", markup=False)
        for candidate in row["trace"].get("unclosed_candidates", []):
            console.print(
                f"Possible output recipient: {candidate['possible_recipient']} (ownership unverified)",
                markup=False,
            )
            for token, raw in candidate["asset_deltas"].items():
                console.print(f"  Including that recipient: {raw} raw units of {token}", markup=False)
    if row["assessment"] == "trace_supported" and row["trace"].get("cycle_path"):
        for leg in row["trace"]["cycle_path"]:
            console.print(
                f"  {leg['venue']}: {leg['amount_in']} raw {leg['token_in']} → {leg['amount_out']} raw {leg['token_out']}",
                markup=False,
            )
    payouts = row.get("forwarded_proceeds", {})
    if payouts or row.get("forwarded_claims"):
        retained = row.get("retained_before_gas_raw")
        decimals = row.get("profit_decimals")
        amount = units(retained, decimals) if decimals is not None else f"{retained} raw"
        console.print(
            f"Trace group net change after payouts, before gas: {amount} {row.get('profit_symbol') or row.get('profit_token')}",
            markup=False,
        )
        for token, by_recipient in payouts.items():
            for recipient, raw in by_recipient.items():
                amount = (
                    units(raw, decimals)
                    if token == row.get("profit_token") and decimals is not None
                    else f"{raw} raw"
                )
                symbol = row.get("profit_symbol") if token == row.get("profit_token") else token
                claims = [
                    claim
                    for claim in row.get("forwarded_claims", [])
                    if claim["token"] == token and claim["address"] == recipient
                ]
                claimed = sum(int(claim["forwarded_raw"]) for claim in claims)
                if int(raw) > claimed:
                    cash = str(int(raw) - claimed)
                    amount = (
                        units(cash, decimals)
                        if token == row.get("profit_token") and decimals is not None
                        else f"{cash} raw"
                    )
                    console.print(
                        f"Forwarded: {amount} {symbol or token} → {recipient}", markup=False, soft_wrap=True
                    )
                for claim in claims:
                    amount = (
                        units(claim["forwarded_raw"], decimals)
                        if token == row.get("profit_token") and decimals is not None
                        else claim["forwarded_raw"] + " raw"
                    )
                    console.print(
                        f"Forwarded as ERC-6909 claim: {amount} {symbol or token} → {recipient} (manager {claim['manager']})",
                        markup=False,
                        soft_wrap=True,
                    )
        paid_to = {
            (token, recipient) for token, by_recipient in payouts.items() for recipient in by_recipient
        }
        for claim in row.get("forwarded_claims", []):
            if (claim["token"], claim["address"]) in paid_to:
                continue
            # the manager minted the cycle's claim straight to the operator's vault (folded in as the redirected output)
            amount = (
                units(claim["forwarded_raw"], decimals)
                if claim["token"] == row.get("profit_token") and decimals is not None
                else claim["forwarded_raw"] + " raw"
            )
            symbol = row.get("profit_symbol") if claim["token"] == row.get("profit_token") else claim["token"]
            console.print(
                f"Forwarded as ERC-6909 claim: {amount} {symbol} → {claim['address']} (manager {claim['manager']}, vault folded into the group)",
                markup=False,
                soft_wrap=True,
            )
        console.print(
            "Recipient ownership is unverified; forwarded amounts may be collector proceeds or payments.",
            style="dim",
        )
    if row.get("liquidity"):
        liquidity = row["liquidity"]
        console.print(
            f"Liquidity activity: {liquidity['kind']}; {len(liquidity['events'])} pool events, "
            f"{len(liquidity['position_transfers'])} position transfers.",
            markup=False,
        )
        for title, key in (("LP cash collected", "collected_raw"), ("LP cash deposited", "deposited_raw")):
            for token, value in liquidity[key].items():
                console.print(f"{title}: {value} raw {token}", markup=False)
        console.print(
            "LP collections include returned capital; changed position value is not counted as swap profit.",
            style="dim",
        )
    if row.get("known_recipient_received_wei"):
        console.print(
            f"Known recipient gross {native}/wrapped receipts: {units(row['known_recipient_received_wei'])} {native} (separate from network fees)",
            markup=False,
        )
    console.print(
        f"Position: {row['trigger_type']} | Trace: {row['trace'].get('verdict', row['trace']['status'])}",
        markup=False,
    )
    if row["trace"].get("venues"):
        console.print(
            f"Decoded swap-event legs: {row.get('event_label', {}).get('n_legs', row['n_legs'])} | "
            f"Distinct trace venues: {len(row['trace']['venues'])}",
            markup=False,
        )
    if row.get("trigger_tx_hash"):
        distance = row.get("trigger_distance_blocks")
        where = (
            "same block"
            if not distance
            else f"{distance} block(s) earlier (block {row.get('trigger_block_number')})"
        )
        console.print(
            f"Inferred trigger ({where}): {explorer}{row['trigger_tx_hash']}", soft_wrap=True, markup=False
        )
    if detailed:
        console.print(f"From: {row['tx_from']}\nTo: {row['tx_to']}", markup=False)
        console.print(
            f"Profit token: {row.get('profit_token') or 'not established'}\nDecoded swap-event venues: "
            + ", ".join(row["event_label"]["venues"] if row.get("event_label") else row["venues"]),
            markup=False,
        )
        console.print("Trace venues: " + ", ".join(row["trace"].get("venues", [])), markup=False)
        console.print("Reasons: " + ", ".join(row["reasons"]), markup=False)
        console.print("Fees (exact wei):", fee, markup=False)
        if row["trace"].get("notes"):
            console.print("Trace notes: " + ", ".join(row["trace"]["notes"]), markup=False)
