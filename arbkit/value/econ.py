"""Where a counted cycle's gross went (cost split), and a per-day accumulator of those splits that the live store
keeps up to date block by block, so the dashboard never has to re-read every row to draw the economics page."""

from __future__ import annotations

KEYS = ("gross", "chain_fee", "l1_fee", "priority", "tip", "kickback", "net")
SIZE_BUCKETS = (
    (0, 1, "< $1"),
    (1, 10, "$1–10"),
    (10, 100, "$10–100"),
    (100, 1000, "$100–1k"),
    (1000, float("inf"), "≥ $1k"),
)
CHAIN_FEE_LABEL = {
    "ethereum": "Base fee (burned)",
    "op-stack": "Base fee (chain)",
    "arbitrum": "Gas (chain)",
    "bsc": "Gas (validator)",
}
SHARE_BIN = 1000  # keep-share histogram resolution: 0.001

# Families held out of every total until they are verified. Their rows stay stored and labelled (the private site
# lists them), but they never add to cycles, gross, kept, bots, economics or a block's figures, on any chain.
# embedded_cycle (2026-11-03): ~99 % of BSC's is one protocol's own treasury loop (helper 0x36accdfa, funded by and
# paying to 0x144d395b, triggered inside users' trades of hooked tokens) that nets negative over a day; ~0 elsewhere.
HELD_TYPES = frozenset({"embedded_cycle"})


def counted_sql(table: str = "") -> str:
    """The SQL test for a counted cycle (see counted), optionally on a table alias: counted_sql("r")."""
    p = f"{table}." if table else ""
    return f"({p}assessment='trace_supported' AND {p}mev_type NOT IN ({','.join(repr(t) for t in sorted(HELD_TYPES))}))"


COUNTED_SQL = counted_sql()


def counted(row: dict) -> bool:
    """A cycle that counts in the totals: trace-supported and not of a held family (same rule as COUNTED_SQL)."""
    return row.get("assessment") == "trace_supported" and (row.get("mev_type") or "none") not in HELD_TYPES


def cost_split(family: str, row: dict) -> dict | None:
    """Where a counted cycle's gross went, in USD: chain fee (base fee, burned on Ethereum), L1 data fee (OP stack),
    priority fee and in-transaction tips to the block producer, order-flow kickbacks, and what the searcher kept.
    Arbitrum's L1 share is already inside its gas price, so it is not split out there."""
    native_usd, gross, net = row.get("native_usd"), row.get("gross_usd"), row.get("net_usd")
    if native_usd is None or gross is None or net is None:
        return None
    econ = row.get("economics") or {}
    wei = lambda key: int(econ.get(key) or 0)  # noqa: E731
    total, priority = wei("total_fee_wei"), wei("priority_fee_wei")
    l1 = wei("l1_fee_wei") if family == "op-stack" else 0
    chain_fee = max(total - priority - l1, 0)
    usd = lambda value: value / 1e18 * native_usd  # noqa: E731
    return {
        "gross": gross,
        "chain_fee": usd(chain_fee),
        "l1_fee": usd(l1),
        "priority": usd(priority),
        "tip": row.get("builder_tip_usd") or 0.0,
        "kickback": row.get("kickback_usd") or 0.0,
        "net": net,
    }


def new_econ() -> dict:
    return {
        "totals": dict.fromkeys(KEYS, 0.0),
        "cycles": 0,
        "skipped": 0,
        "largest": None,
        "bots": set(),
        "buckets": [dict(dict.fromkeys(KEYS, 0.0), label=b[2], cycles=0) for b in SIZE_BUCKETS],
        "shares": {},
    }


def econ_add(e: dict, family: str, row: dict) -> None:
    """Add one row; only counted cycles (`trace_supported`, not a held family) contribute."""
    if not counted(row):
        return
    split = cost_split(family, row)
    if split is None:
        e["skipped"] += 1
        return
    e["cycles"] += 1
    e["bots"].add(row.get("tx_to") or row.get("tx_from"))
    if e["largest"] is None or split["gross"] > e["largest"]["gross"]:
        e["largest"] = split
    if split["gross"] > 0:
        b = str(round(max(-5.0, min(1.5, split["net"] / split["gross"])) * SHARE_BIN))
        e["shares"][b] = e["shares"].get(b, 0) + 1
    bucket = next(b for b, (lo, hi, _) in zip(e["buckets"], SIZE_BUCKETS) if lo <= split["gross"] < hi)
    bucket["cycles"] += 1
    for target in (e["totals"], bucket):
        for k in KEYS:
            target[k] += split[k]


def econ_to_json(e: dict) -> dict:
    return {**e, "bots": sorted(b for b in e["bots"] if b)}


def econ_from_json(raw: dict) -> dict:
    return {**raw, "bots": set(raw.get("bots") or [])}


def economics_result(chain: str, family: str, per_day: dict[str, dict]) -> dict:
    """The dashboard's economics payload from per-day accumulators (same shape as the in-memory computation)."""
    totals, buckets = (
        dict.fromkeys(KEYS, 0.0),
        [dict(dict.fromkeys(KEYS, 0.0), label=b[2], cycles=0) for b in SIZE_BUCKETS],
    )
    cycles = skipped = 0
    largest, bots, shares, days = None, set(), {}, []
    for day in sorted(per_day):
        e = per_day[day]
        cycles += e["cycles"]
        skipped += e["skipped"]
        bots |= e["bots"]
        if e["largest"] and (largest is None or e["largest"]["gross"] > largest["gross"]):
            largest = e["largest"]
        for b, n in e["shares"].items():
            shares[int(b)] = shares.get(int(b), 0) + n
        for target, source in zip(buckets, e["buckets"]):
            target["cycles"] += source["cycles"]
            for k in KEYS:
                target[k] += source[k]
        for k in KEYS:
            totals[k] += e["totals"][k]
        if e["cycles"]:
            days.append(dict(e["totals"], day=day, cycles=e["cycles"]))
    rnd = lambda d: {k: (round(v, 2) if isinstance(v, float) else v) for k, v in d.items()}  # noqa: E731
    median = None
    n = sum(shares.values())
    if n:
        seen = 0
        for b in sorted(shares):
            seen += shares[b]
            if seen > n // 2:
                median = b / SHARE_BIN
                break
    top = largest or dict.fromkeys(KEYS, 0.0)
    rest_gross, rest_net = totals["gross"] - top["gross"], totals["net"] - top["net"]
    return {
        "chain": chain,
        "family": family,
        "chain_fee_label": CHAIN_FEE_LABEL.get(family, "Chain fee"),
        "cycles": cycles,
        "cycles_without_net": skipped,
        "totals": rnd(totals),
        "keep_share": round(totals["net"] / totals["gross"], 4) if totals["gross"] else None,
        "median_keep_share": round(median, 4) if median is not None else None,
        "largest_cycle_usd": round(top["gross"], 2),
        "largest_cycle_share": round(top["gross"] / totals["gross"], 4) if totals["gross"] else None,
        "keep_share_excluding_largest": round(rest_net / rest_gross, 4) if rest_gross > 0 else None,
        "bots": len(bots),
        "by_size": [rnd(b) for b in buckets if b["cycles"]],
        "days": [rnd(d) for d in days],
    }
