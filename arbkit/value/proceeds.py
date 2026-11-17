"""Keep cycle proceeds, forwarded payouts and retained net changes distinct."""

from __future__ import annotations


def enrich_proceeds(row, weth: str):
    weth = weth.lower()
    trace = row.get("trace", {})
    token = row.get("profit_token")
    supported = (
        row.get("assessment") in {"trace_supported", "trace_supported_loss"}
        and row.get("gross_source") == "trace"
        and trace.get("status") == "ok"
    )
    row["gross_minus_network_fee_wei"] = None
    row["retained_minus_network_fee_wei"] = None
    row["retained_before_gas_raw"] = trace.get("net_after") if supported else None
    row["forwarded_proceeds"] = dict(trace.get("payouts_by_token", {})) if supported else {}
    payouts = row["forwarded_proceeds"].get(token, {})
    row["forwarded_profit_raw"] = str(sum(int(v) for v in payouts.values())) if supported else None
    row["forwarded_claims"] = []
    remaining = {
        (t, a): int(v)
        for t, by_recipient in row["forwarded_proceeds"].items()
        for a, v in by_recipient.items()
    }
    # a vault the verifier absorbed as the redirected output (the singleton manager minted it the cycle's claim)
    # is a forwarded claim too: the manager itself is never a group member, so the mint is not a payout
    redirected = {str(a).lower() for a in trace.get("redirects") or []} if supported else set()
    for claim in row.get("cashflows", {}).get("erc6909_net_raw", []):
        currency = int(claim["currency_id"])
        claim_token = weth if currency == 0 else f"0x{currency:040x}"
        paid = remaining.get((claim_token, claim["address"]), 0)
        if paid > 0 and int(claim["delta_raw"]) > 0:
            row["forwarded_claims"].append(
                {**claim, "token": claim_token, "forwarded_raw": str(min(paid, int(claim["delta_raw"])))}
            )
            remaining[(claim_token, claim["address"])] -= min(paid, int(claim["delta_raw"]))
        elif (
            str(claim["address"]).lower() in redirected
            and int(claim["delta_raw"]) > 0
            and claim_token == token
        ):
            row["forwarded_claims"].append(
                {**claim, "token": claim_token, "forwarded_raw": str(claim["delta_raw"])}
            )
    row["fee_coverage"] = "unverified"
    if not supported:
        return
    if row.get("operator_scope") == "embedded":
        row["fee_coverage"] = "shared_transaction_fee"
        return
    if token != weth:
        row["fee_coverage"] = (
            "loss_before_gas" if int(row.get("gross_profit_raw") or 0) < 0 else "unpriced_token"
        )
        return
    fee = row.get("fees", {}).get("total_fee_wei")
    gross = row.get("gross_profit_raw")
    if fee is None or gross is None:
        row["fee_coverage"] = "unknown_fee"
        return
    difference = int(gross) - int(fee)
    row["gross_minus_network_fee_wei"] = difference
    row["fee_coverage"] = (
        "below_network_fee" if difference < 0 else "covers_network_fee" if difference > 0 else "break_even"
    )
    retained = row["retained_before_gas_raw"]
    if retained is not None:
        row["retained_minus_network_fee_wei"] = int(retained) - int(fee)
    # Compatibility with schema 1: this was retained net change minus sender gas,
    # never total operator P&L. New output uses explicit arithmetic names.
    row["net_after_gas_wei"] = row["retained_minus_network_fee_wei"]


def enrich_report(report):
    weth = report.get("weth") or "0x4200000000000000000000000000000000000006"
    for row in report["rows"]:
        enrich_proceeds(row, weth)
    traced = [r for r in report["rows"] if r["assessment"] == "trace_supported"]
    report["summary"]["trace_supported_fee_wei"] = sum(r["fees"]["total_fee_wei"] or 0 for r in traced)
    report["summary"]["trace_supported_priority_fee_wei"] = sum(
        r["fees"]["priority_fee_wei"] or 0 for r in traced
    )
    report["summary"]["fee_coverage"] = {
        category: sum(r["fee_coverage"] == category for r in traced)
        for category in (
            "below_network_fee",
            "covers_network_fee",
            "break_even",
            "unpriced_token",
            "unknown_fee",
            "shared_transaction_fee",
        )
    }
    losses = [r for r in report["rows"] if r["assessment"] == "trace_supported_loss"]
    report["summary"]["loss_cycle_fee_wei"] = sum(r["fees"]["total_fee_wei"] or 0 for r in losses)
    report["summary"]["loss_cycle_priority_fee_wei"] = sum(r["fees"]["priority_fee_wei"] or 0 for r in losses)
    report["summary"]["builder_tips_wei"] = sum(int(r.get("builder_tip_wei") or 0) for r in report["rows"])
    report["summary"]["mev_types"] = {}
    for r in report["rows"]:
        kind = r.get("mev_type") or "none"
        report["summary"]["mev_types"][kind] = report["summary"]["mev_types"].get(kind, 0) + 1
    report["schema_version"] = 4
    report["report_version"] = "0.4.0"
    return report
