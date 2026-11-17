"""Per-UTC-day accounting from saved reports, and block-range resolution for whole days.

A report row belongs to the day of its block timestamp. When several reports cover the same block, the most
recently created report wins. Observed totals are exact for the blocks covered; when coverage of a day is
partial, an extrapolated figure is shown separately and labelled as an estimate (observed × blocks_in_day /
blocks_covered). Nothing here calls the chain except `block_range_for_day`.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from arbkit.chain.spec import ChainSpec
from arbkit.value.econ import counted
from mevlab.hexutil import to_int

ATOMIC_TYPES = ("atomic_backrun", "atomic_top_of_block", "atomic_top_of_flashblock", "atomic_mid_block")


def day_bounds(date: str) -> tuple[int, int]:
    start = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(start.timestamp()), int((start + timedelta(days=1)).timestamp())


async def _timestamp(rpc, number: int) -> int:
    block = await rpc.get_block(number, False)
    if not block:
        raise ValueError(f"Block {number} not found while resolving a day range.")
    return to_int(block["timestamp"])


async def first_block_at_or_after(rpc, ts: int, lo: int, hi: int) -> int:
    """Smallest block number whose timestamp >= ts, within [lo, hi]."""
    while lo < hi:
        mid = (lo + hi) // 2
        if await _timestamp(rpc, mid) >= ts:
            hi = mid
        else:
            lo = mid + 1
    return lo


async def block_range_for_day(
    rpc, spec: ChainSpec, date: str, cache_path: Path | None = None
) -> tuple[int, int]:
    """Inclusive block range whose timestamps fall in the UTC day; cached per chain and day."""
    cache = {}
    if cache_path and cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text())
        except ValueError:
            cache = {}
    if date in cache:
        return tuple(cache[date])
    start_ts, end_ts = day_bounds(date)
    head = await rpc.block_number()
    head_ts = await _timestamp(rpc, head)
    if start_ts > head_ts:
        raise ValueError(f"{date} starts after the chain head.")
    guess = max(0, head - int((head_ts - start_ts) / (spec.block_time_ms / 1000)) - spec.blocks_per_day // 24)
    lo = guess
    while lo > 0 and await _timestamp(rpc, lo) >= start_ts:
        lo = max(0, lo - spec.blocks_per_day // 4)
    first = await first_block_at_or_after(rpc, start_ts, lo, head)
    if end_ts > head_ts:
        last = head
    else:
        hi_guess = min(head, first + spec.blocks_per_day + spec.blocks_per_day // 8)
        while hi_guess < head and await _timestamp(rpc, hi_guess) < end_ts:
            hi_guess = min(head, hi_guess + spec.blocks_per_day // 4)
        last = await first_block_at_or_after(rpc, end_ts, first, hi_guess) - 1
    if end_ts <= head_ts:
        cache[date] = [first, last]
        if cache_path:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(cache, indent=2) + "\n")
    return first, last


def sample_blocks(first: int, last: int, n: int) -> list[int]:
    total = last - first + 1
    if n >= total:
        return list(range(first, last + 1))
    return sorted({first + (i * total) // n for i in range(n)})


def is_report_file(path: Path) -> bool:
    return (
        path.suffix == ".json"
        and path.name not in {"latest.json", "daily.json"}
        and not path.name.startswith("ui_")
    )


def load_report(path: Path) -> dict | None:
    """One saved report, or None when the file is not a report (or is being written)."""
    try:
        report = json.loads(path.read_text())
    except (ValueError, OSError):
        return None
    if isinstance(report, dict) and "rows" in report and "blocks" in report:
        report["_path"] = str(path)
        return report
    return None


def load_reports(reports_dir: Path) -> list[dict]:
    out = []
    for path in sorted(reports_dir.glob("*.json")):
        if not is_report_file(path):
            continue
        report = load_report(path)
        if report is not None:
            out.append(report)
    return out


def new_day(day: str, number: int) -> dict:
    return {
        "day": day,
        "blocks_covered": 0,
        "first_block": number,
        "last_block": number,
        "transactions": 0,
        "counts": defaultdict(int),
        "gross_usd": defaultdict(float),
        "net_usd": defaultdict(float),
        "fees_usd": 0.0,
        "fees_wei": 0,
        "builder_tips_usd": 0.0,
        "builder_tips_wei": 0,
        "kickbacks_usd": 0.0,
        "kickbacks_wei": 0,
        "unpriced": defaultdict(int),
        "operators": defaultdict(lambda: {"cycles": 0, "gross_usd": 0.0, "net_usd": 0.0}),
        "profit_tokens": defaultdict(lambda: {"cycles": 0, "gross_usd": 0.0}),
        "positions": defaultdict(int),
        "sandwiches": 0,
        "sandwich_profit_usd": 0.0,
        "sandwich_net_usd": 0.0,
        "loss_cycles": 0,
        "loss_usd": 0.0,
        "losers_beaten": 0,
        "candidates_unverified": 0,
        "blocks_with_arbs": 0,
        "uniform": True,
        "quote_leg_cycles": 0,
        "families": defaultdict(int),
        "cex_dex_edge_usd": 0.0,
        "cex_dex_notional_usd": 0.0,
        "liquidation_debt_usd": 0.0,
        "liquidation_collateral_usd": 0.0,
        "jit_bundles": set(),
        # results of the other measured families (2026-11-01): JIT bundles' net over add + remove, liquidation bonuses,
        # inventory-arbitrage gains at market; together with cycles and sandwiches they make the searcher-MEV total
        "jit_profit_usd": 0.0,
        "jit_net_usd": 0.0,
        "liquidation_bonus_usd": 0.0,
        "inventory_gain_usd": 0.0,
        # trace-supported cycles of a held family (econ.HELD_TYPES): stored and labelled, never in any total
        "held_cycles": 0,
        "held_gross_usd": 0.0,
    }


def add_block(d: dict, number: int, rows: list[dict], uniform: bool, transactions: int | None = None) -> None:
    """Add one classified block to a day accumulator. `transactions` overrides len(rows) when only the MEV rows of
    the block are at hand (the live store keeps every row with a type, and a count of the rest)."""
    d["blocks_covered"] += 1
    d["uniform"] = d["uniform"] and uniform
    d["first_block"] = min(d["first_block"], number)
    d["last_block"] = max(d["last_block"], number)
    d["transactions"] += len(rows) if transactions is None else transactions
    if transactions is not None:
        d["counts"]["none"] += max(0, transactions - len(rows))
    has_arb = False
    for row in rows:
        kind = row.get("mev_type", "none")
        d["counts"][kind] += 1
        if row["assessment"] == "trace_supported" and not counted(row):
            d["held_cycles"] += 1
            d["held_gross_usd"] += row.get("gross_usd") or 0.0
        elif row["assessment"] == "trace_supported":
            has_arb = True
            position = row.get("trigger_type") or "unknown"
            if position == "backrun" and row.get("trigger_distance_blocks"):
                position = "backrun_next_block"
            d["positions"][position] += 1
            if row.get("quote_venues"):
                d["quote_leg_cycles"] += 1
            gross, net = row.get("gross_usd"), row.get("net_usd")
            if gross is None:
                d["unpriced"][kind] += 1
            else:
                d["gross_usd"][kind] += gross
                if net is not None:
                    d["net_usd"][kind] += net
                fee, tip = row.get("fee_usd") or 0.0, row.get("builder_tip_usd") or 0.0
                d["fees_usd"] += fee
                d["builder_tips_usd"] += tip
                d["fees_wei"] += row["fees"]["total_fee_wei"] or 0
                d["builder_tips_wei"] += int(row.get("builder_tip_wei") or 0)
                d["kickbacks_usd"] += row.get("kickback_usd") or 0.0
                d["kickbacks_wei"] += int(row.get("kickback_wei") or 0)
                op = d["operators"][row.get("tx_to") or row.get("tx_from") or "?"]
                op["cycles"] += 1
                op["gross_usd"] += gross
                op["net_usd"] += net or 0.0
                tok = d["profit_tokens"][row.get("profit_symbol") or row.get("profit_token") or "?"]
                tok["cycles"] += 1
                tok["gross_usd"] += gross
        elif row["assessment"] == "trace_supported_loss":
            # a losing bot: shown in its own section, never netted against what the winners extracted
            d["loss_cycles"] += 1
            d["loss_usd"] += max(-(row.get("gross_usd") or 0.0), 0.0)
            d["losers_beaten"] += bool(row.get("beaten_by"))
        elif row["assessment"] in {"event_candidate", "needs_review"}:
            d["candidates_unverified"] += 1
        if kind in {
            "liquidation",
            "launch_snipe",
            "jit_liquidity",
            "cex_dex_candidate",
            "pool_exploit",
            "inventory_arbitrage",
        }:
            d["families"][kind] += 1
            if kind == "cex_dex_candidate" and row.get("cex_dex"):
                d["cex_dex_edge_usd"] += row["cex_dex"].get("edge_usd") or 0.0
                d["cex_dex_notional_usd"] += row["cex_dex"].get("notional_usd") or 0.0
            if kind == "liquidation":
                for liq in row.get("liquidations") or []:
                    d["liquidation_debt_usd"] += liq.get("debt_usd") or 0.0
                    d["liquidation_collateral_usd"] += liq.get("collateral_usd") or 0.0
                    d["liquidation_bonus_usd"] += liq.get("bonus_usd") or 0.0
            if kind == "jit_liquidity" and row.get("jit"):
                d["jit_bundles"].add(row["jit"]["bundle_id"])
                if row["jit"].get("role") == "remove" and row["jit"].get("profit_usd") is not None:
                    d["jit_profit_usd"] += row["jit"]["profit_usd"]
                    d["jit_net_usd"] += row["jit"].get("net_usd") or 0.0
            if kind == "inventory_arbitrage" and row.get("inventory_arb"):
                d["inventory_gain_usd"] += row["inventory_arb"].get("gain_usd") or 0.0
        sandwich = row.get("sandwich")
        if sandwich and sandwich.get("role") == "back":
            d["sandwiches"] += 1
            if sandwich.get("profit_usd") is not None:
                d["sandwich_profit_usd"] += sandwich["profit_usd"]
                d["sandwich_net_usd"] += sandwich.get("net_usd") or 0.0
    if has_arb:
        d["blocks_with_arbs"] += 1


def merge_day(into: dict, other: dict) -> dict:
    """Add one accumulator (an hour's or a day's) into another: counts and dollars add up, sets join, block ranges
    widen. Rolling windows are built this way from hourly accumulators."""
    for k, v in other.items():
        if k == "day":
            continue
        cur = into.get(k)
        if k == "first_block":
            into[k] = min(cur, v) if cur is not None else v
        elif k == "last_block":
            into[k] = max(cur, v) if cur is not None else v
        elif k == "uniform":
            into[k] = bool(cur) and bool(v)
        elif isinstance(cur, set):
            cur |= set(v)
        elif isinstance(cur, dict):
            for kk, vv in v.items():
                if isinstance(vv, dict):
                    target = cur[kk]
                    for f, x in vv.items():
                        target[f] = target.get(f, 0) + x
                else:
                    cur[kk] += vv
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            into[k] = (cur or 0) + v
    return into


def day_to_json(d: dict) -> dict:
    """A day accumulator as plain JSON (sets -> sorted lists), for the live store."""
    return {
        k: (sorted(v) if isinstance(v, set) else dict(v) if isinstance(v, defaultdict) else v)
        for k, v in d.items()
    }


def day_from_json(raw: dict) -> dict:
    d = new_day(raw["day"], raw["first_block"])
    for k, v in raw.items():
        if isinstance(d.get(k), set):
            d[k] = set(v)
        elif isinstance(d.get(k), defaultdict):
            d[k].update(v)
        else:
            d[k] = v
    return d


def finalize_day(spec: ChainSpec, day: str, d: dict, days_cache: dict | None = None) -> dict:
    blocks_in_day = None
    if days_cache and day in days_cache:
        first, last = days_cache[day]
        blocks_in_day = last - first + 1
    elif day != datetime.now(timezone.utc).strftime("%Y-%m-%d"):
        blocks_in_day = spec.blocks_per_day
    coverage = d["blocks_covered"] / blocks_in_day if blocks_in_day else None
    factor = (1 / coverage) if coverage and coverage < 1 and d["uniform"] else 1.0
    atomic_gross = sum(d["gross_usd"][k] for k in ATOMIC_TYPES)
    atomic_net = sum(d["net_usd"][k] for k in ATOMIC_TYPES)
    embedded_gross = d["gross_usd"]["embedded_cycle"]
    total_gross = atomic_gross + embedded_gross + d["sandwich_profit_usd"]
    total_net = atomic_net + d["sandwich_net_usd"]
    operators = sorted(d["operators"].items(), key=lambda kv: -kv[1]["gross_usd"])[:10]
    tokens = sorted(d["profit_tokens"].items(), key=lambda kv: -kv[1]["gross_usd"])[:10]
    return {
        "day": day,
        "blocks_covered": d["blocks_covered"],
        "blocks_in_day": blocks_in_day,
        "coverage": coverage,
        "first_block": d["first_block"],
        "last_block": d["last_block"],
        "transactions": d["transactions"],
        "blocks_with_arbs": d["blocks_with_arbs"],
        "cycles": sum(d["counts"][k] for k in ATOMIC_TYPES),
        "embedded_cycles": d["counts"]["embedded_cycle"],
        "loss_cycles": d["loss_cycles"],
        "loss_usd": round(d["loss_usd"], 2),
        "losers_beaten": d["losers_beaten"],
        "candidates_unverified": d["candidates_unverified"],
        "sandwiches": d["sandwiches"],
        "positions": dict(d["positions"]),
        "mev_types": dict(d["counts"]),
        "quote_leg_cycles": d["quote_leg_cycles"],
        "families": {
            **dict(d["families"]),
            "jit_bundles": len(d["jit_bundles"]),
            "cex_dex_edge_usd": round(d["cex_dex_edge_usd"], 2),
            "cex_dex_notional_usd": round(d["cex_dex_notional_usd"], 2),
            "liquidation_debt_usd": round(d["liquidation_debt_usd"], 2),
            "liquidation_collateral_usd": round(d["liquidation_collateral_usd"], 2),
            "liquidation_bonus_usd": round(d.get("liquidation_bonus_usd", 0.0), 2),
            "jit_profit_usd": round(d.get("jit_profit_usd", 0.0), 2),
            "jit_net_usd": round(d.get("jit_net_usd", 0.0), 2),
            "inventory_gain_usd": round(d.get("inventory_gain_usd", 0.0), 2),
        },
        "observed": {
            "atomic_gross_usd": round(atomic_gross, 2),
            "atomic_net_usd": round(atomic_net, 2),
            "embedded_gross_usd": round(embedded_gross, 2),
            "held_cycles": d.get("held_cycles", 0),
            "held_gross_usd": round(d.get("held_gross_usd", 0.0), 2),
            "sandwich_profit_usd": round(d["sandwich_profit_usd"], 2),
            "sandwich_net_usd": round(d["sandwich_net_usd"], 2),
            "total_gross_usd": round(total_gross, 2),
            "total_net_usd": round(total_net, 2),
            # every family with a measured result (CEX-DEX candidates are estimates and stay out)
            "searcher_mev_usd": round(
                total_gross
                + d.get("jit_profit_usd", 0.0)
                + d.get("liquidation_bonus_usd", 0.0)
                + d.get("inventory_gain_usd", 0.0),
                2,
            ),
            "fees_usd": round(d["fees_usd"], 2),
            "builder_tips_usd": round(d["builder_tips_usd"], 2),
            "fees_wei": d["fees_wei"],
            "builder_tips_wei": d["builder_tips_wei"],
            "kickbacks_usd": round(d["kickbacks_usd"], 2),
            "kickbacks_wei": d["kickbacks_wei"],
            "unpriced_cycles": sum(d["unpriced"].values()),
        },
        "sampling": "uniform (day command)"
        if d["uniform"]
        else "selected blocks: no extrapolation (use `day --sample N` for an unbiased estimate)",
        "extrapolated_per_day": None
        if factor == 1.0
        else {
            "total_gross_usd": round(total_gross * factor, 2),
            "total_net_usd": round(total_net * factor, 2),
            "cycles": round(sum(d["counts"][k] for k in ATOMIC_TYPES) * factor, 1),
            "note": "observed x blocks_in_day / blocks_covered; sampled blocks, not a census",
        },
        "top_operators": [
            {"address": a, **{k: (round(v, 2) if isinstance(v, float) else v) for k, v in s.items()}}
            for a, s in operators
        ],
        "top_profit_tokens": [
            {"token": t, **{k: (round(v, 2) if isinstance(v, float) else v) for k, v in s.items()}}
            for t, s in tokens
        ],
    }


DAILY_METHOD = (
    "exact per covered block; USD from Binance 1m opens (final at the minute start) and stable/leg-implied pricing; "
    "net = gross - network fee - builder tip - known order-flow kickbacks, trace-supported cycles only"
)


def aggregate_daily(spec: ChainSpec, reports: list[dict], days_cache: dict | None = None) -> dict:
    """Group the newest classification of every scanned block by UTC day."""
    by_block: dict[int, tuple[str, dict, dict]] = {}
    for report in sorted(reports, key=lambda r: r.get("created_at", "")):
        rows_by_block: dict[int, list[dict]] = defaultdict(list)
        for row in report["rows"]:
            rows_by_block[row["block_number"]].append(row)
        uniform = str(report.get("day_mode", "")).startswith(("sample", "full"))
        for info in report["blocks"]:
            by_block[info["number"]] = (
                report.get("created_at", ""),
                {**info, "_uniform": uniform},
                rows_by_block.get(info["number"], []),
            )
    days: dict[str, dict] = {}
    for number, (_, info, rows) in sorted(by_block.items()):
        day = datetime.fromtimestamp(info["timestamp"], tz=timezone.utc).strftime("%Y-%m-%d")
        d = days.setdefault(day, new_day(day, number))
        add_block(d, number, rows, info.get("_uniform", False))
    out_days = [finalize_day(spec, day, d, days_cache) for day, d in sorted(days.items())]
    return {
        "chain": spec.name,
        "chain_id": spec.chain_id,
        "native_symbol": spec.native_symbol,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "reports": len(reports),
        "days": out_days,
        "method": DAILY_METHOD,
    }


def render_daily(console, result: dict) -> None:
    from rich.table import Table

    table = Table(
        title=f"{result['chain']} · arbitrage per UTC day ({result['reports']} reports)", expand=True
    )
    for name in (
        "Day",
        "Blocks",
        "Coverage",
        "Cycles",
        "Sandw.",
        "Gross USD",
        "Fees USD",
        "Tips USD",
        "Kickbacks USD",
        "Net USD",
        "Est. day gross",
        "Est. day net",
        "Unpriced",
    ):
        table.add_column(name, overflow="fold")
    for d in result["days"]:
        obs, est = d["observed"], d.get("extrapolated_per_day")
        coverage = f"{100 * d['coverage']:.2f}%" if d["coverage"] is not None else "?"
        table.add_row(
            d["day"],
            f"{d['blocks_covered']:,}",
            coverage,
            f"{d['cycles']} (+{d['embedded_cycles']} emb, {d['loss_cycles']} loss)",
            str(d["sandwiches"]),
            f"{obs['total_gross_usd']:,.2f}",
            f"{obs['fees_usd']:,.2f}",
            f"{obs['builder_tips_usd']:,.2f}",
            f"{obs['kickbacks_usd']:,.2f}",
            f"{obs['total_net_usd']:,.2f}",
            f"{est['total_gross_usd']:,.0f}"
            if est
            else ("exact" if (d["coverage"] or 0) >= 1 else "n/a (selected blocks)"),
            f"{est['total_net_usd']:,.0f}"
            if est
            else ("exact" if (d["coverage"] or 0) >= 1 else "n/a (selected blocks)"),
            str(obs["unpriced_cycles"]),
        )
    console.print(table)
    console.print(result["method"], style="dim")
