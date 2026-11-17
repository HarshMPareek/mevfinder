"""Per-row review sheets: everything a person needs to decide, row by row, whether a classified cycle is a real
arbitrage — the route (venue, DEX, tokens, amounts), who funded it, where the result went, what the builder got,
and the structural flags the rules raised — with the explorer link next to it.

`./arb -c <chain> review REPORT` writes chains/<chain>/validation/review_<stem>.md.
`./arb -c <chain> blind N --seed S` picks N blocks at random from the last day (seeded, reproducible), scans them
and writes the sheet: the blind-sample protocol.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from arbkit.chain.spec import ROOT, ChainSpec
from arbkit.value.fees import units

FLAG_NOTES = (
    "singleton_only_route",
    "gain_exceeds_route_surplus",
    "proceeds_consumed_by_protocol_payments",
    "closed_gain_without_returning_route",
    "token_mint_redemption_flows",
    "pass_through_helpers",
    "uncalled_counterparties",
    "hook_fee_recipients",
    "amount_discontinuity",
    "positive_residual",
    "merged_same_collector",
    "receipt_loss_prevents_closure",
    "cycle_token",
)


def _short(a: str | None, n: int = 10) -> str:
    return (a or "")[:n] + ("…" if a and len(a) > n else "")


def _sym(spec: ChainSpec, token: str | None, symbols: dict[str, str]) -> str:
    if not token:
        return "?"
    return symbols.get(token.lower()) or spec.token_symbol(token) or _short(token, 8)


def _amount(spec: ChainSpec, token: str | None, raw, decimals: dict[str, int]) -> str:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return str(raw)
    dec = decimals.get((token or "").lower())
    if dec is None:
        dec = spec.token_decimals(token) if token else None
    return units(value, dec) if dec is not None else str(value)


def route_lines(
    spec: ChainSpec, row: dict, decimals: dict[str, int], symbols: dict[str, str], dex_of: dict[str, str]
) -> list[str]:
    tv = row.get("trace") or {}
    path = tv.get("cycle_path") or []
    legs = (
        path if path else [lg for lg in (tv.get("legs") or []) if lg.get("token_in") and lg.get("token_out")]
    )
    out = []
    for lg in legs:
        venue = str(lg.get("venue") or "").lower()
        address = venue.split(":")[0]  # singleton legs may carry manager:pool_id
        dex = dex_of.get(address) or ("singleton" if address in spec.pool_managers else "")
        out.append(
            f"{_amount(spec, lg.get('token_in'), lg.get('amount_in'), decimals)} {_sym(spec, lg.get('token_in'), symbols)} → "
            f"{_amount(spec, lg.get('token_out'), lg.get('amount_out'), decimals)} {_sym(spec, lg.get('token_out'), symbols)} "
            f"@ {_short(venue)}{(' ' + dex) if dex else ''}"
        )
    if not path and tv.get("verdict") in {"gain_only", "single_venue_loop"}:
        out.append(
            "route from the Swap events (singleton flash accounting): "
            + ", ".join(_short(v, 14) for v in (row.get("venues") or [])[:6])
        )
    return out


def route_legs(
    spec: ChainSpec, row: dict, decimals: dict[str, int], symbols: dict[str, str], dex_of: dict[str, str]
) -> list[dict]:
    """The cycle's legs in order, for the route diagram: token and amount in, DEX, token and amount out."""
    tv = row.get("trace") or {}
    legs = tv.get("cycle_path") or [
        lg for lg in (tv.get("legs") or []) if lg.get("token_in") and lg.get("token_out")
    ]
    out = []
    for lg in legs:
        address = str(lg.get("venue") or "").lower().split(":")[0]
        out.append(
            leg_view(
                spec,
                dex_of.get(address) or ("singleton" if address in spec.pool_managers else "pool"),
                address,
                lg.get("token_in"),
                lg.get("amount_in"),
                lg.get("token_out"),
                lg.get("amount_out"),
                decimals,
                symbols,
            )
        )
    return out


def leg_view(
    spec: ChainSpec,
    dex: str,
    venue: str,
    token_in,
    amount_in,
    token_out,
    amount_out,
    decimals: dict[str, int],
    symbols: dict[str, str],
) -> dict:
    """One swap for display; an amount whose token decimals are unknown is left out rather than shown raw."""

    def amount(token, raw):
        known = decimals.get(str(token or "").lower()) is not None or (
            token and spec.token_decimals(token) is not None
        )
        return _amount(spec, token, raw, decimals) if known else None

    return {
        "dex": dex,
        "venue": venue,
        "symbol_in": _sym(spec, token_in, symbols),
        "amount_in": amount(token_in, amount_in),
        "symbol_out": _sym(spec, token_out, symbols),
        "amount_out": amount(token_out, amount_out),
    }


def venue_dexes(spec: ChainSpec, data_dir: Path | None = None) -> dict[str, str]:
    """venue address (lowercase) -> DEX name, from the singleton registry and the cached pool metadata.

    A row's `dexes` list is sorted and de-duplicated, so it cannot be zipped with `venues` (that labelled a V4
    pool "uniswap_v3" and the V3 pool "uniswap_v4" on Ethereum 26137380 #199)."""
    out = {address: entry["name"] for address, entry in spec.pool_managers.items()}
    path = (data_dir or ROOT / "data" / spec.name) / "pool_meta.jsonl"
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                key, meta = json.loads(line)
            except ValueError:
                continue
            address = str(key).split(":")[0].lower()
            if isinstance(meta, list) and len(meta) > 2 and meta[2] and address not in out:
                out[address] = meta[2]
    return out


def review_rows(spec: ChainSpec, report: dict, data_dir: Path | None = None) -> list[dict]:
    decimals: dict[str, int] = {t.address.lower(): t.decimals for t in spec.tokens}
    symbols: dict[str, str] = {t.address.lower(): t.symbol for t in spec.tokens}
    for r in report["rows"]:
        if r.get("profit_token") and r.get("profit_decimals") is not None:
            decimals[r["profit_token"].lower()] = r["profit_decimals"]
            if r.get("profit_symbol"):
                symbols[r["profit_token"].lower()] = r["profit_symbol"]
    dex_of = venue_dexes(spec, data_dir)
    blocks = {b["number"]: b for b in report["blocks"]}
    out = []
    for r in sorted(report["rows"], key=lambda x: (x["block_number"], x["tx_index"])):
        if (
            r["assessment"] not in {"trace_supported", "trace_supported_loss", "needs_review"}
            and not (r.get("sandwich") or {}).get("role") == "back"
        ):
            continue
        tv = r.get("trace") or {}
        flags = [n for n in tv.get("notes") or [] if any(n.startswith(f) for f in FLAG_NOTES)]
        flags += [
            x
            for x in r.get("reasons") or []
            if x.startswith(
                (
                    "settlement_contract_called",
                    "closed_gain_without_called_route",
                    "redirected_collectors_exceed_event_gain",
                    "singleton_multi_pool",
                )
            )
        ]
        payouts = {}
        for tok, by in (tv.get("payouts_by_token") or {}).items():
            for who, v in by.items():
                payouts[f"{_short(who)}"] = f"{_amount(spec, tok, v, decimals)} {_sym(spec, tok, symbols)}"
        block = blocks.get(r["block_number"], {})
        out.append(
            {
                "tx_hash": r["tx_hash"],
                "block": r["block_number"],
                "index": r["tx_index"],
                "assessment": r["assessment"],
                "mev_type": r.get("mev_type"),
                "position": r.get("trigger_type"),
                "builder": block.get("builder_tag") or block.get("miner", "")[:10],
                "gross": f"{r.get('gross_profit') or ''} {r.get('profit_symbol') or ''}".strip(),
                "gross_usd": r.get("gross_usd"),
                "fee_usd": r.get("fee_usd"),
                "tip_usd": r.get("builder_tip_usd"),
                "kickback_usd": r.get("kickback_usd"),
                "net_usd": r.get("net_usd"),
                "route": route_lines(spec, r, decimals, symbols, dex_of),
                "funded": "operator paid in " + _sym(spec, tv.get("entry_token"), symbols)
                if tv.get("entry_token")
                else "operator paid nothing in (singleton take / pool-to-pool)",
                "group": [_short(g) for g in tv.get("group") or []],
                "collectors": [_short(x) for x in tv.get("redirects") or []],
                "payouts": payouts,
                "protocol_payments": tv.get("protocol_payments") or {},
                "flags": flags,
                "explorer": f"{spec.explorer_tx}{r['tx_hash']}",
                "callee": r.get("tx_to"),
                "sender": r.get("tx_from"),
                "sandwich": (r.get("sandwich") or {}).get("role"),
            }
        )
    return out


def write_review(spec: ChainSpec, report: dict, path: Path) -> Path:
    rows = review_rows(spec, report)
    counts = {
        k: sum(1 for x in rows if x["assessment"] == k)
        for k in ("trace_supported", "trace_supported_loss", "needs_review")
    }
    lines = [
        f"# {spec.name}: review sheet — {len(report['blocks'])} blocks, {len(report['rows'])} transactions",
        "",
        f"Generated {datetime.now(timezone.utc).isoformat()} from `{report.get('created_at')}`. "
        f"{counts['trace_supported']} positive cycles, {counts['trace_supported_loss']} losing cycles, {counts['needs_review']} under review. "
        "For each row: is the route a closed chain in one asset through called venues, did the operator (or a singleton take) fund it, "
        "and did the result reach the operator or its collector? Open the explorer link to confirm the transfers.",
        "",
    ]
    for x in rows:
        head = f"## {x['assessment']} · {x['mev_type'] or x['sandwich'] or ''} · block {x['block']} #{x['index']} · [{x['tx_hash'][:14]}…]({x['explorer']})"
        lines += [
            head,
            "",
            f"- gross **{x['gross']}**"
            + (f" (${x['gross_usd']:,.2f})" if x.get("gross_usd") is not None else " (unpriced)")
            + f"; fee ${x['fee_usd'] or 0:,.2f}, builder tip ${x['tip_usd'] or 0:,.2f}, kickback ${x['kickback_usd'] or 0:,.2f}"
            + (f", net ${x['net_usd']:,.2f}" if x.get("net_usd") is not None else "")
            + f"; position {x['position']}; block by {x['builder']}",
            f"- sender {x['sender']} → callee {x['callee']}; group {', '.join(x['group'])}"
            + (f"; collectors folded in: {', '.join(x['collectors'])}" if x["collectors"] else ""),
            f"- {x['funded']}",
        ]
        lines += [f"- route: {leg}" for leg in x["route"]] or ["- route: (none recorded)"]
        if x["payouts"]:
            lines.append("- payouts: " + "; ".join(f"{k} ← {v}" for k, v in x["payouts"].items()))
        if x["protocol_payments"]:
            lines.append(f"- protocol payments (not proceeds): {x['protocol_payments']}")
        if x["flags"]:
            lines.append("- flags: " + "; ".join(x["flags"]))
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return path
