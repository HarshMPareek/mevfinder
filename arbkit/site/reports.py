"""One chain's data read from saved report files (reports/<chain>/*.json): rows, blocks, bots, families,
builders, daily figures and per-transaction evidence. The newest report that covers a block wins, as in `daily`."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from arbkit.chain.spec import ROOT, load_chain
from arbkit.files import read_json
from arbkit.site.poolindex import LazyMap, PoolIndex
from arbkit.site.views import PUBLIC_MEV_TYPES, compact, public_kind, public_view
from arbkit.value.builders import measure_builders
from arbkit.value.daily import aggregate_daily, is_report_file, load_report
from arbkit.value.econ import CHAIN_FEE_LABEL, SIZE_BUCKETS, cost_split
from arbkit.verify.evidence import block_proof, singleton_swaps, transaction_evidence
from arbkit.verify.review import leg_view, route_legs, route_lines
from mevlab.decode.signatures import event_topic

RAW_EVENT_NAMES = {
    event_topic(sig): name
    for sig, name in (
        ("Transfer(address,address,uint256)", "Transfer"),
        ("Approval(address,address,uint256)", "Approval"),
        ("Deposit(address,uint256)", "WETH Deposit"),
        ("Withdrawal(address,uint256)", "WETH Withdrawal"),
        ("Swap(address,uint256,uint256,uint256,uint256,address)", "Swap (V2)"),
        ("Sync(uint112,uint112)", "Sync (V2)"),
        ("Swap(address,address,int256,int256,uint160,uint128,int24)", "Swap (V3)"),
        ("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)", "Swap (V4)"),
        ("ModifyLiquidity(bytes32,address,int24,int24,int256,bytes32)", "ModifyLiquidity (V4)"),
        ("Transfer(address,address,address,uint256,uint256)", "Claim transfer (ERC-6909)"),
        ("Mint(address,address,int24,int24,uint128,uint256,uint256)", "Mint (V3)"),
        ("Burn(address,int24,int24,uint128,uint256,uint256)", "Burn (V3)"),
    )
}


ON_ACCESS_INDEX_BYTES = 50_000_000  # a page indexes at most this much of pool_meta.jsonl itself


class ChainData:
    def __init__(self, name: str, reports_dir: Path):
        self.name = name
        self.spec = load_chain(name)
        self.reports_dir = reports_dir
        self.lock = threading.Lock()
        self.signature = None
        self.reports: list[dict] = []
        self.rows_by_block: dict[int, list[dict]] = {}
        self.block_ts: dict[int, int] = {}
        self.block_info: dict[int, dict] = {}
        self.rows_by_hash: dict[str, dict] = {}
        self.daily: dict = {}
        self._builders: dict | None = None
        self._files: dict[
            str, tuple[int, dict]
        ] = {}  # file name -> (mtime_ns, parsed report): only changed files are re-read

    def _signature(self):
        files = sorted(self.reports_dir.glob("*.json")) if self.reports_dir.exists() else []
        return tuple((f.name, f.stat().st_mtime_ns) for f in files if is_report_file(f))

    def refresh(self) -> None:
        with self.lock:
            signature = self._signature()
            if signature == self.signature:
                return
            current = dict(signature)
            for name in [n for n, (mtime, _) in self._files.items() if current.get(n) != mtime]:
                del self._files[name]
            for name, mtime in signature:
                if name not in self._files:
                    report = load_report(self.reports_dir / name)
                    if report is not None:
                        self._files[name] = (mtime, report)
            reports = [report for _, report in self._files.values()]
            rows_by_block: dict[int, list[dict]] = {}
            block_ts: dict[int, int] = {}
            block_info: dict[int, dict] = {}
            for report in sorted(reports, key=lambda r: r.get("created_at", "")):
                grouped: dict[int, list[dict]] = {}
                for row in report["rows"]:
                    grouped.setdefault(row["block_number"], []).append(row)
                for info in report["blocks"]:
                    rows_by_block[info["number"]] = sorted(
                        grouped.get(info["number"], []), key=lambda r: r["tx_index"]
                    )
                    block_ts[info["number"]] = info["timestamp"]
                    block_info[info["number"]] = info
            days_cache = {}
            cache_path = ROOT / "data" / self.name / "days.json"
            if cache_path.exists():
                try:
                    days_cache = json.loads(cache_path.read_text())
                except ValueError:
                    days_cache = {}
            self.reports, self.rows_by_block, self.block_ts, self.block_info = (
                reports,
                rows_by_block,
                block_ts,
                block_info,
            )
            self.rows_by_hash = {r["tx_hash"].lower(): r for rows in rows_by_block.values() for r in rows}
            self.daily = aggregate_daily(self.spec, reports, days_cache)
            self._builders = None
            self.signature = signature

    # ---- lookups the live store overrides (`LiveChainData`) ----
    def get_row(self, tx_hash: str) -> dict | None:
        self.refresh()
        return self.rows_by_hash.get(tx_hash.lower())

    def get_block_rows(self, number: int) -> list[dict] | None:
        self.refresh()
        return self.rows_by_block.get(number)

    def get_block_info(self, number: int) -> dict:
        return self.block_info.get(number) or {}

    def get_block_ts(self, number: int) -> int | None:
        return self.block_ts.get(number)

    def has_block(self, number: int) -> bool:
        self.refresh()
        return number in self.rows_by_block

    def blocks_list(self, limit: int = 500) -> list[dict]:
        self.refresh()
        numbers = sorted(self.rows_by_block, reverse=True)[:limit]
        return [
            {
                "number": n,
                "timestamp": self.block_ts.get(n),
                "transactions": len(self.rows_by_block[n]),
                "cycles": sum(r["assessment"] == "trace_supported" for r in self.rows_by_block[n]),
                "sandwiches": sum(r.get("sandwich", {}).get("role") == "back" for r in self.rows_by_block[n]),
                "gross_usd": round(
                    sum(
                        r.get("gross_usd") or 0
                        for r in self.rows_by_block[n]
                        if r["assessment"] == "trace_supported"
                    ),
                    2,
                ),
            }
            for n in numbers
        ]

    def builders(self) -> dict:
        self.refresh()
        if self._builders is None:
            newest = sorted(self.block_info, reverse=True)[
                :500
            ]  # the latest 500 scanned blocks: bounded block-body reads
            report = {
                "rows": [r for n in newest for r in self.rows_by_block.get(n, [])],
                "blocks": [self.block_info[n] for n in newest],
                "created_at": max((r.get("created_at", "") for r in self.reports), default=None),
            }
            self._builders = measure_builders(self.spec, report, ROOT / "data" / self.name)
            self._builders["per_block"] = sorted(self._builders["per_block"], key=lambda b: -b["number"])[
                :300
            ]
        return self._builders

    def families(self, public: bool = False) -> dict:
        self.refresh()
        totals: dict[str, float] = {}
        for d in self.daily.get("days", []):
            for k, v in (d.get("families") or {}).items():
                if public and k.startswith("cex_dex"):
                    continue  # parked family: never published
                totals[k] = totals.get(k, 0) + v
        totals = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in totals.items()}
        rows = [r for rows in self.rows_by_block.values() for r in rows]
        quote = [r for r in rows if r.get("quote_venues")]
        next_block = [
            r for r in rows if r["assessment"] == "trace_supported" and r.get("trigger_distance_blocks")
        ]
        return {
            "totals": totals,
            "quote_leg_cycles": len(quote),
            "next_block_backruns": len(next_block),
            "quote_leg_gross_usd": round(sum(r.get("gross_usd") or 0 for r in quote), 2),
            "recent": {
                kind: self.transactions(25, {kind}, public=public)
                for kind in (
                    "liquidation",
                    "launch_snipe",
                    "jit_liquidity",
                    "pool_exploit",
                    "inventory_arbitrage",
                    "cex_dex_candidate",
                )
                if not (public and kind == "cex_dex_candidate")
            },
        }

    def evidence(self, tx_hash: str) -> dict | None:
        row = self.get_row(tx_hash)
        if row is None:
            return None
        info = self.get_block_info(row["block_number"])
        return transaction_evidence(self.spec, ROOT / "data" / self.name, row, info.get("hash"))

    def proof(self, number: int) -> dict | None:
        rows = self.get_block_rows(number)
        if rows is None:
            return None
        rows = [
            self.get_row(r["tx_hash"]) or r
            if r.get("mev_type") not in (None, "none") or r["assessment"] != "not_arb"
            else r
            for r in rows
        ]
        return block_proof(self.spec, ROOT / "data" / self.name, number, self.get_block_info(number), rows)

    def validation(self) -> dict:
        directory = self.spec.directory / "validation"
        out: dict = {"files": {}, "sheets": []}
        if not directory.exists():
            return out
        for kind in ("handlabels", "explorer_check", "audit", "recall", "builders", "facts"):
            latest = sorted(directory.glob(f"{kind}_*.json"))
            if not latest:
                continue
            try:
                data = json.loads(latest[-1].read_text())
            except ValueError:
                continue
            summary = {k: v for k, v in data.items() if not isinstance(v, (list, dict))}
            for k, v in data.items():
                if isinstance(v, list):
                    summary[f"{k}_count"] = len(v)
            out["files"][kind] = {"file": latest[-1].name, "summary": summary}
        out["sheets"] = sorted(p.name for p in directory.glob("*.md"))
        return out

    def summary(self) -> dict:
        self.refresh()
        days = self.daily["days"]
        positives = [
            r for rows in self.rows_by_block.values() for r in rows if r["assessment"] == "trace_supported"
        ]
        return {
            "name": self.name,
            "chain_id": self.spec.chain_id,
            "native_symbol": self.spec.native_symbol,
            "explorer_tx": self.spec.explorer_tx,
            "explorer_block": self.spec.explorer_block,
            "explorer_address": self.spec.explorer_address,
            "explorer_name": self.spec.explorer_name,
            "family": self.spec.family,
            "ordering_policy": self.spec.ordering_policy,
            "block_time_ms": self.spec.block_time_ms,
            "fee_flow": self.spec.fee_flow or {},
            "reports": len(self.reports),
            "blocks_covered": len(self.rows_by_block),
            "first_block": min(self.rows_by_block) if self.rows_by_block else None,
            "last_block": max(self.rows_by_block) if self.rows_by_block else None,
            "cycles": len(positives),
            "gross_usd": round(sum(r.get("gross_usd") or 0 for r in positives), 2),
            "net_usd": round(
                sum(r.get("net_usd") or 0 for r in positives if r.get("net_usd") is not None), 2
            ),
            "sandwiches": sum(
                1
                for rows in self.rows_by_block.values()
                for r in rows
                if r.get("sandwich", {}).get("role") == "back"
            ),
            "days": len(days),
            "last_scanned": max((r.get("created_at", "") for r in self.reports), default=None),
        }

    def economics(self) -> dict:
        """Where the counted cycles' gross went: totals, by cycle size and per UTC day (atomic cycles with a net figure)."""
        self.refresh()
        keys = ("gross", "chain_fee", "l1_fee", "priority", "tip", "kickback", "net")
        zero = lambda: dict.fromkeys(keys, 0.0)  # noqa: E731
        totals, buckets, days = zero(), [dict(zero(), label=b[2], cycles=0) for b in SIZE_BUCKETS], {}
        shares, cycles, skipped, largest, bots = [], 0, 0, None, set()
        for number, rows in self.rows_by_block.items():
            day = (
                datetime.fromtimestamp(self.block_ts[number], timezone.utc).strftime("%Y-%m-%d")
                if self.block_ts.get(number)
                else None
            )
            for row in rows:
                if row["assessment"] != "trace_supported":
                    continue
                split = cost_split(self.spec.family, row)
                if split is None:
                    skipped += 1
                    continue
                cycles += 1
                bots.add(row.get("tx_to") or row.get("tx_from"))
                if largest is None or split["gross"] > largest["gross"]:
                    largest = split
                if split["gross"] > 0:
                    shares.append(split["net"] / split["gross"])
                bucket = next(b for b, (lo, hi, _) in zip(buckets, SIZE_BUCKETS) if lo <= split["gross"] < hi)
                bucket["cycles"] += 1
                targets = [totals, bucket] + (
                    [days.setdefault(day, dict(zero(), day=day, cycles=0))] if day else []
                )
                for target in targets:
                    for k in keys:
                        target[k] += split[k]
                if day:
                    days[day]["cycles"] += 1
        rnd = lambda d: {k: (round(v, 2) if isinstance(v, float) else v) for k, v in d.items()}  # noqa: E731
        shares.sort()
        top = largest or dict.fromkeys(keys, 0.0)
        rest_gross, rest_net = totals["gross"] - top["gross"], totals["net"] - top["net"]
        return {
            "chain": self.name,
            "family": self.spec.family,
            "chain_fee_label": CHAIN_FEE_LABEL.get(self.spec.family, "Chain fee"),
            "cycles": cycles,
            "cycles_without_net": skipped,
            "totals": rnd(totals),
            "keep_share": round(totals["net"] / totals["gross"], 4) if totals["gross"] else None,
            "median_keep_share": round(shares[len(shares) // 2], 4) if shares else None,
            "largest_cycle_usd": round(top["gross"], 2),
            "largest_cycle_share": round(top["gross"] / totals["gross"], 4) if totals["gross"] else None,
            "keep_share_excluding_largest": round(rest_net / rest_gross, 4) if rest_gross > 0 else None,
            "bots": len(bots),
            "by_size": [rnd(b) for b in buckets if b["cycles"]],
            "days": [rnd(days[d]) for d in sorted(days)],
        }

    def transactions(
        self,
        limit=200,
        kinds: set[str] | None = None,
        min_usd: float | None = None,
        block: int | None = None,
        public: bool = False,
        sort: str | None = None,
    ) -> list[dict]:
        if sort == "usd":
            rows = self.transactions(10**9, kinds, min_usd, block, public)
            return sorted(
                rows, key=lambda r: -((r.get("sandwich") or {}).get("profit_usd") or r.get("gross_usd") or 0)
            )[:limit]
        self.refresh()
        out = []
        blocks = [block] if block is not None else sorted(self.rows_by_block, reverse=True)
        for number in blocks:
            for row in reversed(self.rows_by_block.get(number, [])):
                kind = row.get("mev_type", "none")
                if public and kind not in PUBLIC_MEV_TYPES:
                    continue
                if kinds and kind not in kinds:
                    continue
                if not kinds and kind == "none":
                    continue
                usd = row.get("gross_usd")
                if row.get("sandwich", {}).get("role") == "back":
                    usd = row["sandwich"].get("profit_usd")
                if min_usd is not None and (usd is None or usd < min_usd):
                    continue
                ts = self.block_ts.get(number)
                out.append(
                    public_view(row, ts, self.spec.family)
                    if public
                    else dict(compact(row, ts), kind=public_kind(row))
                )
                if len(out) >= limit:
                    return out
        return out

    def block(self, number: int, public: bool = False) -> dict | None:
        """Every transaction of the block in execution order, each with its coarse role (`kind`); a transaction another
        one backran in this block is marked `trigger`, and backruns carry `trigger_tx_index`."""
        rows = self.get_block_rows(number)
        if rows is None:
            return None
        ts = self.get_block_ts(number)
        out = [
            public_view(r, ts, self.spec.family) if public else dict(compact(r, ts), kind=public_kind(r))
            for r in rows
        ]
        index = {r["tx_hash"]: r["tx_index"] for r in rows}
        for view, row in zip(out, rows):
            trigger = row.get("trigger_tx_hash")
            if view["kind"] == "arbitrage" and row.get("trigger_type") == "backrun" and trigger in index:
                view["trigger_tx_index"] = index[trigger]
        triggers = {v["trigger_tx_index"] for v in out if "trigger_tx_index" in v}
        for view in out:
            if view["kind"] == "other" and view["tx_index"] in triggers:
                view["kind"] = "trigger"
        first = rows[0] if rows else {}
        return {
            "number": number,
            "timestamp": ts,
            "transactions": len(rows),
            "builder": first.get("block_builder_tag"),
            "producer": first.get("block_producer"),
            "rows": out,
        }

    def raw_logs(self, tx_hash: str) -> dict | None:
        """The transaction's receipt as it is on chain (status, gas, every log's contract, topics and data), with the
        names of well-known events. Public chain data: it says nothing about how the row was classified."""
        row = self.get_row(tx_hash)
        if row is None:
            return None
        path = ROOT / "data" / self.name / "blocks" / f"{row['block_number']}.json.gz"
        if not path.exists():
            return {"tx_hash": row["tx_hash"], "available": False, "logs": []}
        bundle = read_json(path)
        receipt = next(
            (r for r in bundle["receipts"] if str(r["transactionHash"]).lower() == tx_hash.lower()), None
        )
        tx = next(
            (x for x in bundle["block"]["transactions"] if str(x["hash"]).lower() == tx_hash.lower()), {}
        )
        if receipt is None:
            return {"tx_hash": row["tx_hash"], "available": False, "logs": []}
        logs = [
            {
                "index": int(str(lg.get("logIndex", "0x0")), 16),
                "address": str(lg.get("address", "")).lower(),
                "event": RAW_EVENT_NAMES.get(str((lg.get("topics") or [""])[0]).lower(), ""),
                "topics": [str(x).lower() for x in lg.get("topics") or []],
                "data": str(lg.get("data") or "0x"),
            }
            for lg in (receipt.get("logs") or [])[:400]
        ]
        return {
            "tx_hash": row["tx_hash"],
            "available": True,
            "status": int(str(receipt.get("status", "0x0")), 16),
            "gas_used": int(str(receipt.get("gasUsed", "0x0")), 16),
            "from": tx.get("from"),
            "to": tx.get("to"),
            "value_wei": str(int(str(tx.get("value", "0x0")), 16)),
            "input": str(tx.get("input") or "0x")[:10],
            "logs": logs,
        }

    def public_transaction(self, tx_hash: str) -> dict | None:
        row = self.get_row(tx_hash)
        if row is None:
            return None
        view = public_view(row, self.get_block_ts(row["block_number"]), self.spec.family)
        view["legs"] = self.legs(row, public=True)
        trigger = row.get("trigger_tx_hash")
        if trigger and view["kind"] == "arbitrage":
            same_block = {r["tx_hash"]: r["tx_index"] for r in self.get_block_rows(row["block_number"]) or []}
            if trigger in same_block:
                view["trigger_tx_index"] = same_block[trigger]
        return view

    def pool_index(self) -> PoolIndex:
        """The chain's pool metadata, indexed on disk (poolindex.py): a page looks up the pools it shows instead of
        re-reading pool_meta.jsonl (3.1 GB on Base) whenever a follower appends to it."""
        idx = getattr(self, "_pool_index", None)
        if idx is None:
            data = ROOT / "data" / self.name
            idx = self._pool_index = PoolIndex(
                data / "pool_meta.jsonl", data / "pool_index.sqlite", self.spec.pool_managers
            )
        if idx.pending_bytes() < ON_ACCESS_INDEX_BYTES:
            idx.update()  # a few appended lines (or a small local cache); a big backlog is the keeper's job
        return idx

    def venue_dexes(self) -> LazyMap:
        """venue -> DEX name (registered singleton managers, then the pool metadata index)."""
        idx = self.pool_index()
        return LazyMap(idx.dex)

    def _token_maps(self, row: dict) -> tuple[dict, dict]:
        decimals = {t.address: t.decimals for t in self.spec.tokens}
        symbols = {t.address: t.symbol for t in self.spec.tokens}
        if row.get("profit_token") and row.get("profit_decimals") is not None:
            decimals[row["profit_token"].lower()] = row["profit_decimals"]
            if row.get("profit_symbol"):
                symbols[row["profit_token"].lower()] = row["profit_symbol"]
        return decimals, symbols

    def pool_tokens(self) -> LazyMap:
        """'manager:pool_id' or pool address -> (token0, token1, dex, hooks) from the pool metadata index."""
        return LazyMap(self.pool_index().get)

    def _singleton_legs(self, row: dict, decimals: dict, symbols: dict) -> list[dict]:
        """A route that never leaves a singleton (Uniswap V4, PancakeSwap Infinity) has no trace legs: rebuild it from the
        transaction's own Swap events (swapper deltas: negative = paid in, positive = received) and the pools' currencies."""
        path = ROOT / "data" / self.name / "blocks" / f"{row['block_number']}.json.gz"
        if not path.exists():
            return []
        receipt = next(
            (
                r
                for r in read_json(path)["receipts"]
                if str(r["transactionHash"]).lower() == row["tx_hash"].lower()
            ),
            None,
        )
        if receipt is None:
            return []
        zero = "0x" + "0" * 40
        decimals, symbols = dict(decimals), dict(symbols)
        decimals.setdefault(zero, 18)
        symbols.setdefault(zero, self.spec.native_symbol)
        pools, raw = self.pool_tokens(), []
        for swap in sorted(singleton_swaps(receipt, {}, {}), key=lambda s: s["log_index"]):
            meta = pools.get(f"{swap['manager']}:{swap['pool_id']}")
            if meta is None or swap["amount0"] is None or swap["amount1"] is None:
                return []  # an unresolved pool would break the loop: show nothing rather than half a route
            a0, a1 = int(swap["amount0"]), int(swap["amount1"])
            token_in, amount_in, token_out, amount_out = (
                (meta[0], -a0, meta[1], a1) if a0 < 0 else (meta[1], -a1, meta[0], a0)
            )
            raw.append(
                {
                    "log_index": swap["log_index"],
                    "dex": meta[2],
                    "manager": swap["manager"],
                    "hook": meta[3],
                    "token_in": token_in,
                    "amount_in": amount_in,
                    "token_out": token_out,
                    "amount_out": amount_out,
                    "hook_fee": 0,
                    "hook_fee_token": None,
                }
            )
        # A hook that takes its fee as ERC-6909 claims minted to itself (caller = the hook) right after the swap it served:
        # the swapper settles the Swap event's amount minus that cut (Ethereum 26139154 #7: 0.13597 ETH swapped, 0.13456 received).
        claim_topic = event_topic("Transfer(address,address,address,uint256,uint256)")
        for log in receipt.get("logs") or []:
            topics = [str(x).lower() for x in log.get("topics") or []]
            if len(topics) != 4 or topics[0] != claim_topic or int(topics[1], 16) != 0:
                continue
            data = str(log.get("data") or "0x")[2:]
            caller, amount = "0x" + data[24:64], int(data[64:128] or "0", 16)
            currency, index = "0x" + topics[3][-40:], int(str(log.get("logIndex", "0x0")), 16)
            served = [
                leg
                for leg in raw
                if leg["log_index"] < index
                and leg["hook"]
                and leg["hook"] == caller
                and currency in (leg["token_in"], leg["token_out"])
            ]
            if served:
                leg = served[-1]
                if currency == leg["token_out"]:
                    leg["amount_out"] -= amount
                else:
                    leg["amount_in"] += amount
                leg["hook_fee"], leg["hook_fee_token"] = leg["hook_fee"] + amount, currency
        legs = []
        for leg in raw:
            view = dict(
                leg_view(
                    self.spec,
                    leg["dex"],
                    leg["manager"],
                    leg["token_in"],
                    leg["amount_in"],
                    leg["token_out"],
                    leg["amount_out"],
                    decimals,
                    symbols,
                ),
                source="swap_events",
            )
            if leg["hook_fee"]:
                fee = leg_view(
                    self.spec,
                    "",
                    "",
                    leg["hook_fee_token"],
                    leg["hook_fee"],
                    leg["hook_fee_token"],
                    0,
                    decimals,
                    symbols,
                )
                view["hook_fee"], view["hook_fee_symbol"] = fee["amount_in"], fee["symbol_in"]
            legs.append(view)
        return legs

    def legs(self, row: dict, public: bool = False) -> list[dict]:
        if public_kind(row) not in ("arbitrage", "losing"):
            return []
        decimals, symbols = self._token_maps(row)
        legs = route_legs(self.spec, row, decimals, symbols, self.venue_dexes()) or self._singleton_legs(
            row, decimals, symbols
        )
        if public:
            for leg in legs:
                leg.pop("venue", None)
        return legs

    def transaction(self, tx_hash: str) -> dict | None:
        row = self.get_row(tx_hash)
        if row is None:
            return None
        detail = dict(row)
        detail.pop("event_label", None)
        detail["timestamp"] = self.get_block_ts(row["block_number"])
        decimals, symbols = self._token_maps(row)
        detail["route"] = route_lines(self.spec, row, decimals, symbols, self.venue_dexes())
        detail["legs"] = self.legs(row)
        detail["kind"] = public_kind(row)
        detail["costs"] = cost_split(self.spec.family, row)
        trigger = row.get("trigger_tx_hash")
        same_block = {r["tx_hash"]: r["tx_index"] for r in self.get_block_rows(row["block_number"]) or []}
        if trigger in same_block and row.get("trigger_type") == "backrun":
            detail["trigger_tx_index"] = same_block[trigger]
        return detail

    def bot(self, address: str, public: bool = False, limit: int = 300) -> dict:
        """One bot contract: every indexed transaction it sent or received (newest first) with its totals."""
        self.refresh()
        address = address.lower()
        rows = [
            r
            for r in self.transactions(limit=10**9, public=public)
            if address in {str(r.get("tx_to") or "").lower(), str(r.get("tx_from") or "").lower()}
        ]
        rows.sort(key=lambda r: (-int(r.get("block_number") or 0), -int(r.get("tx_index") or 0)))
        counted = [r for r in rows if r.get("assessment") == "trace_supported"]
        blocks = [int(r["block_number"]) for r in rows if r.get("block_number") is not None]
        return {
            "address": address,
            "transactions": len(rows),
            "cycles": len(counted),
            "gross_usd": round(sum(r.get("gross_usd") or 0.0 for r in counted), 2),
            "net_usd": round(sum(r.get("net_usd") or 0.0 for r in counted), 2),
            "backruns": sum(r.get("trigger_type") == "backrun" for r in counted),
            "top_of_block": sum(
                r.get("trigger_type") in ("top_of_block", "top_of_flashblock") for r in counted
            ),
            "wallets": len(
                {
                    r.get("tx_from")
                    for r in counted
                    if r.get("tx_from") and str(r.get("tx_to") or "").lower() == address
                }
            ),
            "first_block": min(blocks) if blocks else None,
            "last_block": max(blocks) if blocks else None,
            "rows": rows[:limit],
        }

    def operators(self, limit=25) -> list[dict]:
        self.refresh()
        agg: dict[str, dict] = {}
        for rows in self.rows_by_block.values():
            for r in rows:
                if r["assessment"] != "trace_supported":
                    continue
                key = r.get("tx_to") or r.get("tx_from") or "?"
                a = agg.setdefault(
                    key,
                    {
                        "address": key,
                        "cycles": 0,
                        "gross_usd": 0.0,
                        "net_usd": 0.0,
                        "backruns": 0,
                        "top_of_block": 0,
                        "wallets": set(),
                    },
                )
                a["cycles"] += 1
                a["gross_usd"] += r.get("gross_usd") or 0.0
                a["net_usd"] += r.get("net_usd") or 0.0
                a["backruns"] += r.get("trigger_type") == "backrun"
                a["top_of_block"] += r.get("trigger_type") == "top_of_block"
                if r.get("tx_from"):
                    a["wallets"].add(r["tx_from"])
        out = sorted(agg.values(), key=lambda a: -a["gross_usd"])[:limit]
        return [
            {
                **a,
                "gross_usd": round(a["gross_usd"], 2),
                "net_usd": round(a["net_usd"], 2),
                "wallets": len(a["wallets"]),
            }
            for a in out
        ]
