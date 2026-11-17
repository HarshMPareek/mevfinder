"""What a classified row looks like on the site: the compact list form, and the public form (only the fields and
figures the public site shows)."""

from __future__ import annotations

from arbkit.value.econ import cost_split

POSITIVE = {"trace_supported", "trace_supported_loss"}
COMPACT_KEYS = (
    "block_number",
    "tx_index",
    "tx_hash",
    "tx_from",
    "tx_to",
    "assessment",
    "arb_class",
    "mev_type",
    "trigger_type",
    "trigger_tx_hash",
    "trigger_distance_blocks",
    "trigger_block_number",
    "n_legs",
    "profit_token",
    "profit_symbol",
    "gross_profit",
    "gross_profit_raw",
    "gross_usd",
    "gross_native",
    "price_source",
    "fee_usd",
    "builder_tip_wei",
    "builder_tip_usd",
    "kickback_wei",
    "kickback_usd",
    "net_usd",
    "explorer_url",
    "operator_scope",
    "gross_source",
    "fee_coverage",
    "forwarded_profit_raw",
    "block_producer",
    "block_builder_tag",
    "refund_to_trigger_wei",
    "slot_index_est",
    "quote_venues",
    "cex_dex",
    "snipe",
    "jit",
    "user_index_in_block",
    "dexes",
)


def compact(row: dict, timestamp: int | None = None) -> dict:
    out = {k: row.get(k) for k in COMPACT_KEYS}
    out["fee_native"] = row["fees"].get("total_fee_eth")
    out["priority_gwei"] = row["fees"].get("priority_fee_per_gas_gwei")
    trace = row.get("trace") or {}
    out["venues"] = (
        trace["venues_n"] if "venues_n" in trace else len(trace.get("venues") or row.get("venues") or [])
    )
    out["timestamp"] = timestamp
    sandwich = row.get("sandwich")
    if sandwich:
        out["sandwich"] = {
            k: sandwich.get(k)
            for k in (
                "bundle_id",
                "role",
                "pool",
                "operator",
                "operator_kind",
                "front",
                "back",
                "victims",
                "profit",
                "profit_symbol",
                "profit_usd",
                "net_usd",
                "price_source",
            )
        }
    if row.get("liquidations"):
        out["liquidations"] = [
            {
                k: liq.get(k)
                for k in (
                    "protocol",
                    "liquidator",
                    "borrower",
                    "debt_asset",
                    "debt_usd",
                    "collateral_asset",
                    "collateral_usd",
                    "bonus_usd",
                )
            }
            for liq in row["liquidations"]
        ]
    for key in ("inventory_arb", "exploited_pools", "beaten_by", "beat"):
        if row.get(key):
            out[key] = row[key]
    return out


# ---- public mode -----------------------------------------------------------------------------------------------
# What an outside visitor sees (`./arb ui --public`): labels and money only. No reasons, trace notes, verdicts,
# rejected or unverified candidates, proofs, raw rows or on-demand scans, so the site cannot be used as an oracle
# to reverse-engineer the rules. The private mode (default, localhost) keeps everything for diligence.
PUBLIC_MEV_TYPES = {
    "atomic_backrun",
    "atomic_top_of_block",
    "atomic_mid_block",
    "atomic_top_of_flashblock",
    "sandwich_front",
    "sandwich_back",
    "sandwich_victim",
    "loss_cycle",
    "liquidation",
    "jit_liquidity",
    "launch_snipe",
    "pool_exploit",
}  # inventory_arbitrage: private until its valuation is verified (2026-11-02: $433.9k on 17 Ethereum rows)
# embedded_cycle: private and out of every total (econ.HELD_TYPES, 2026-11-03: BSC's is a protocol's own treasury loop)
PUBLIC_KEYS = (
    "block_number",
    "tx_index",
    "tx_hash",
    "tx_from",
    "tx_to",
    "trigger_type",
    "trigger_tx_hash",
    "trigger_distance_blocks",
    "profit_symbol",
    "gross_profit",
    "gross_usd",
    "fee_usd",
    "builder_tip_usd",
    "kickback_usd",
    "net_usd",
    "explorer_url",
    "block_builder_tag",
    "block_producer",
    "slot_index_est",
    "user_index_in_block",
    "dexes",
    "n_legs",
)


def public_kind(row: dict) -> str:
    """One coarse role per transaction for the block map: arbitrage, losing, sandwich, victim, other_mev or other."""
    mev = row.get("mev_type") or "none"
    if mev not in PUBLIC_MEV_TYPES:
        return "other"
    if mev == "loss_cycle" or row.get("assessment") == "trace_supported_loss":
        return "losing"
    if mev in ("sandwich_front", "sandwich_back"):
        return "sandwich"
    if mev == "sandwich_victim":
        return "victim"
    if mev in ("liquidation", "jit_liquidity", "launch_snipe", "pool_exploit", "inventory_arbitrage"):
        return "other_mev"
    return "arbitrage" if row.get("assessment") == "trace_supported" else "other"


def public_view(row: dict, timestamp: int | None = None, family: str | None = None) -> dict:
    kind = public_kind(row)
    out = {
        "block_number": row["block_number"],
        "tx_index": row["tx_index"],
        "tx_hash": row["tx_hash"],
        "tx_from": row.get("tx_from"),
        "tx_to": row.get("tx_to"),
        "kind": kind,
        "timestamp": timestamp,
        "explorer_url": row.get("explorer_url"),
    }
    if kind == "other":
        return out
    out.update({k: row.get(k) for k in PUBLIC_KEYS})
    if row.get("assessment") not in POSITIVE:
        # a JIT add, a sandwich leg or a liquidation's "gross_profit" is a deposit, a leg's proceeds or a repayment, not
        # a result (Base 0xb94446e0 showed 51,666 VIRTUAL deposited as profit): their results live in their own sections
        for k in ("gross_profit", "gross_usd", "profit_symbol", "net_usd"):
            out[k] = None
    out["mev_type"] = row.get("mev_type")
    out["fee_native"] = (row.get("fees") or {}).get("total_fee_eth", row.get("fee_native"))
    out["priority_gwei"] = (row.get("fees") or {}).get("priority_fee_per_gas_gwei", row.get("priority_gwei"))
    if family and row.get("economics") is not None:
        out["costs"] = cost_split(family, row)
    sandwich = row.get("sandwich")
    if sandwich:
        out["sandwich"] = {
            k: sandwich.get(k)
            for k in ("bundle_id", "role", "pool", "profit", "profit_symbol", "profit_usd", "net_usd")
        }
        out["sandwich"]["victims"] = len(sandwich.get("victims") or [])
    if row.get("liquidations"):
        out["liquidations"] = [
            {k: liq.get(k) for k in ("protocol", "debt_usd", "collateral_usd", "bonus_usd")}
            for liq in row["liquidations"]
        ]
    if row.get("jit"):
        out["jit"] = {"role": row["jit"].get("role"), "bundle_id": row["jit"].get("bundle_id")}
    if row.get("inventory_arb"):
        out["inventory_arb"] = {
            k: row["inventory_arb"].get(k) for k in ("gain_usd", "paid_usd", "received_usd", "stable_pair")
        }
    if row.get("exploited_pools"):
        out["exploited_pools"] = row["exploited_pools"]
    if row.get("beaten_by"):  # the winner of the opportunity this losing cycle executed too late
        out["beaten_by"] = {k: row["beaten_by"].get(k) for k in ("tx_hash", "tx_index", "operator")}
    if row.get("beat"):
        out["beat"] = list(row["beat"])
    return out


# Dollar figures of families whose valuation is not verified yet: kept out of every public payload (the counts stay).
# 2026-11-04: an Optimism "JIT bundle" of two unrelated wallets read -$194,430 on the public overview, and Base JIT
# results counted a USDC gain while leaving out the unpriced token sold for it.
PUBLIC_HIDDEN_FIGURES = frozenset({"jit_profit_usd", "jit_net_usd", "inventory_gain_usd", "searcher_mev_usd"})


def public_figures(value):
    """A copy of an API payload without PUBLIC_HIDDEN_FIGURES, at any depth."""
    if isinstance(value, dict):
        return {k: public_figures(v) for k, v in value.items() if k not in PUBLIC_HIDDEN_FIGURES}
    if isinstance(value, list):
        return [public_figures(v) for v in value]
    return value
