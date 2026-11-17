"""Scan blocks into a report: download (or read the cache), classify, price, and summarise."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from eth_abi import decode

from arbkit.chain.rpc import SafeRpc
from arbkit.chain.settings import Settings
from arbkit.files import read_json, write_json
from arbkit.scan.block import BlockContext, classify_block
from arbkit.scan.bundle import decode_extra_data, download_bundle, validate_bundle
from arbkit.scan.pools import HistoricalPools
from arbkit.value.fees import units
from arbkit.value.pricing import PriceBook, value_rows
from arbkit.value.proceeds import enrich_proceeds, enrich_report
from mevlab.decode.signatures import selector
from mevlab.hexutil import to_int
from mevlab.rpc import RpcError
from mevlab.version import CLASSIFIER_VERSION


class Scanner:
    def __init__(self, settings: Settings | None, progress=lambda text: None, offline_prices=False):
        self.settings = settings
        self.spec = settings.chain if settings else None
        self.progress = progress
        self.offline_prices = offline_prices

    async def run(self, blocks, trace_mode="candidates", refresh=False, transaction=None, extra=None):
        settings, spec = self.settings, self.spec
        async with SafeRpc(settings.http_url) as rpc, SafeRpc(settings.trace_url) as trace_rpc:
            if await rpc.chain_id() != spec.chain_id:
                raise ValueError(f"Configured RPC is not {spec.name} (chain ID {spec.chain_id}).")
            trace_available = trace_mode != "none"
            if trace_available:
                try:
                    trace_available = await trace_rpc.chain_id() == spec.chain_id
                except RpcError:
                    trace_available = False
                if not trace_available:
                    self.progress(
                        "Trace RPC unavailable or wrong chain; event candidates will be marked unverified."
                    )
            if transaction:
                tx = await rpc.call("eth_getTransactionByHash", [transaction])
                if not tx or tx.get("blockNumber") is None:
                    raise ValueError(f"Transaction not found or not mined on {spec.name}.")
                blocks = [to_int(tx["blockNumber"])]
            elif blocks == ["latest"]:
                blocks = [await rpc.block_number()]
            elif blocks == ["sample"]:
                head = await rpc.block_number()
                blocks = list(range(max(0, head - 7), max(0, head - 2)))
            numbers = sorted(set(blocks))
            if not numbers:
                raise ValueError("No block numbers supplied.")
            resolver = HistoricalPools(
                rpc,
                settings.data / "pool_meta.json",
                spec.factories,
                spec.pool_managers,
                max_log_range=spec.max_log_range,
            )
            ctx = BlockContext(spec)
            rows, block_info, failures = [], [], []
            for index, number in enumerate(numbers, 1):
                self.progress(f"Block {number} ({index}/{len(numbers)}): downloading and classifying…")
                try:
                    found, info = await self.scan_block(
                        rpc,
                        trace_rpc if trace_available else None,
                        number,
                        resolver,
                        ctx,
                        trace_mode,
                        refresh,
                    )
                    rows.extend(found)
                    block_info.append(info)
                    resolver.save()
                    self.progress(
                        f"Block {number}: {len(found)} transactions; "
                        f"{sum(r['assessment'] == 'trace_supported' for r in found)} positive cycles; "
                        f"{sum(r['assessment'] == 'trace_supported_loss' for r in found)} losing cycles; "
                        f"{sum(r['assessment'] in ('event_candidate', 'needs_review') for r in found)} candidates/review; "
                        f"{sum(1 for r in found if r.get('sandwich', {}).get('role') == 'back')} sandwiches."
                    )
                except (RpcError, ValueError, OSError) as exc:
                    failures.append({"block": number, "error": str(exc)})
                    self.progress(f"Block {number} failed: {exc}")
                    ctx.reset()
            resolver.save()
            book = PriceBook(settings.data.parent / "prices", offline=self.offline_prices)
            return await self.finish(
                rpc,
                trace_rpc,
                rows,
                block_info,
                failures,
                numbers,
                trace_mode,
                book,
                transaction=transaction,
                extra=extra,
            )

    async def scan_block(
        self,
        rpc,
        trace_rpc,
        number,
        resolver,
        ctx,
        trace_mode="candidates",
        refresh=False,
        cache_level: int = 9,
    ):
        """Download (or read from the block cache), classify and recheck one block. `trace_rpc` None = no traces.
        Returns (rows, block info). Raises RpcError/ValueError/OSError; the caller resets `ctx` on failure."""
        settings, spec = self.settings, self.spec
        rpc.block_tag = hex(number)
        # A bounded (local-node) resolver keeps its misses: re-searching the same unknown pool every block costs a
        # log scan each time, and a pool born later is learned from its Initialize event (`learn`) or the seed.
        if getattr(resolver, "max_log_range", None) is None:
            resolver.misses.clear()
        cache = settings.data / "blocks" / f"{number}.json.gz"
        from_pruned_cache = False
        if cache.exists() and not refresh:
            bundle = read_json(cache)
            header = await rpc.get_block(number, False)
            if header and header["hash"] == bundle["block"]["hash"]:
                block, receipts = bundle["block"], bundle["receipts"]
            elif header is None:
                # The node no longer serves this block (a snapshot without old history, BSC): the cached bundle was
                # checked against a node when it was saved, and validate_bundle re-checks it below.
                block, receipts = bundle["block"], bundle["receipts"]
                from_pruned_cache = True
            else:
                block, receipts = await download_bundle(rpc, number)
        else:
            block, receipts = await download_bundle(rpc, number)
        validate_bundle(block, receipts, number)
        previous = ctx.previous
        if previous and number == previous[0] + 1 and block.get("parentHash") != previous[1]:
            raise ValueError("Adjacent block hashes disagree; rerun with --refresh.")
        if previous is None or number != previous[0] + 1:
            ctx.reset()
        write_json(cache, {"chain_id": spec.chain_id, "block": block, "receipts": receipts}, cache_level)
        if isinstance(resolver, HistoricalPools):
            resolver.learn(receipts)
        found = await classify_block(
            spec, block, receipts, resolver, ctx, trace_rpc, trace_mode, settings.data / "traces", refresh
        )
        # Recheck after tracing to catch a reorg during the scan.
        header = None if from_pruned_cache else await rpc.get_block(number, False)
        if not from_pruned_cache and (not header or header["hash"] != block["hash"]):
            raise ValueError("Block changed during classification; rerun with --refresh.")
        info = {
            "number": number,
            "hash": block["hash"],
            "parent_hash": block.get("parentHash"),
            "timestamp": to_int(block["timestamp"]),
            "transactions": len(found),
            "miner": block.get("miner"),
            "builder_tag": decode_extra_data(block.get("extraData")),
            "base_fee_per_gas": to_int(block["baseFeePerGas"])
            if block.get("baseFeePerGas") is not None
            else None,
            "gas_used": to_int(block.get("gasUsed", "0x0")),
        }
        ctx.previous = number, block["hash"]
        return found, info

    async def finish(
        self,
        rpc,
        trace_rpc,
        rows,
        block_info,
        failures,
        numbers,
        trace_mode,
        book,
        transaction=None,
        extra=None,
    ):
        """Token symbols, USD values and the report envelope for a batch of classified blocks."""
        spec = self.spec
        await self.add_token_info(rows, rpc)
        timestamps = {b["number"]: b["timestamp"] for b in block_info}
        await value_rows(spec, rows, timestamps, book)
        switched = [r for r in rows if "profit_token_by_value" in (r.get("reasons") or [])]
        if switched:  # pricing chose a more valuable gain as the profit: its symbol and decimals
            await self.add_token_info(switched, rpc)
        report = {
            "schema_version": 1,
            "classifier_version": CLASSIFIER_VERSION,
            "chain": spec.name,
            "chain_id": spec.chain_id,
            "family": spec.family,
            "native_symbol": spec.native_symbol,
            "weth": spec.weth,
            "explorer_name": spec.explorer_name,
            "explorer_tx": spec.explorer_tx,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "requested_blocks": numbers,
            "blocks": block_info,
            "failures": failures,
            "complete": not failures,
            "trace_mode": trace_mode,
            "rows": rows,
            "rpc_stats": {"blocks_and_metadata": rpc.stats, "traces": trace_rpc.stats if trace_rpc else {}},
            "summary": summarize(rows),
            "focus_transaction": transaction,
        }
        if extra:
            report.update(extra)
        return enrich_report(report)

    async def add_token_info(self, rows, rpc):
        spec = self.spec
        tokens = {r.get("profit_token") for r in rows if r["assessment"] != "not_arb"} - {None}
        tokens |= {
            r["sandwich"].get("profit_token") for r in rows if r.get("sandwich", {}).get("profit_token")
        }
        cache = self.__dict__.setdefault("token_cache", {})  # kept across batches by a long-running follower
        metadata = {**cache, **{t.address: (t.symbol, t.decimals) for t in spec.tokens}}

        async def describe(token):
            raw = await rpc.eth_call(token, selector("decimals()"))
            decimals = to_int(raw)
            if decimals > 255:
                return None
            raw = bytes.fromhex((await rpc.eth_call(token, selector("symbol()")))[2:])
            try:
                symbol = decode(["string"], raw)[0]
            except Exception:
                symbol = raw[:32].rstrip(b"\0").decode("utf-8", "replace")
            return "".join(c for c in symbol if c.isprintable())[:24], decimals

        for token in tokens - set(metadata):
            try:
                found = await describe(token)
            except (RpcError, ValueError):
                # A node without the old state (pruned/path-scheme) cannot answer at the scanned block; a token's
                # symbol and decimals do not change, so ask at the head instead.
                pinned, rpc.block_tag = rpc.block_tag, "latest"
                try:
                    found = await describe(token)
                except (RpcError, ValueError):
                    found = None
                finally:
                    rpc.block_tag = pinned
            if found:
                metadata[token] = cache[token] = found
        for row in rows:
            symbol, decimals = metadata.get(row.get("profit_token"), (None, None))
            row["profit_symbol"], row["profit_decimals"] = symbol, decimals
            row["gross_profit"] = (
                units(row["gross_profit_raw"], decimals)
                if decimals is not None and row.get("gross_profit_raw") is not None
                else None
            )
            sandwich = row.get("sandwich")
            if sandwich and sandwich.get("profit_token"):
                s_symbol, s_decimals = metadata.get(sandwich["profit_token"], (None, None))
                sandwich["profit_symbol"], sandwich["profit_decimals"] = s_symbol, s_decimals
                sandwich["profit"] = (
                    units(sandwich["profit_raw"], s_decimals) if s_decimals is not None else None
                )
            enrich_proceeds(row, spec.weth)


def summarize(rows):
    arbs = [r for r in rows if r["assessment"] != "not_arb"]
    traced = [r for r in rows if r["assessment"] == "trace_supported"]
    return {
        "transactions": len(rows),
        "assessments": dict(Counter(r["assessment"] for r in rows)),
        "classes": dict(Counter(r["arb_class"] for r in rows)),
        "mev_types": dict(Counter(r.get("mev_type", "none") for r in rows)),
        "trace_unavailable": sum(r["trace"]["status"] == "unavailable" for r in rows),
        "incomplete_fees": sum(not r["fees"]["complete"] for r in rows),
        "arb_and_candidate_fee_wei": sum(r["fees"]["total_fee_wei"] or 0 for r in arbs),
        "arb_and_candidate_priority_fee_wei": sum(r["fees"]["priority_fee_wei"] or 0 for r in arbs),
        "trace_supported_fee_wei": sum(r["fees"]["total_fee_wei"] or 0 for r in traced),
        "trace_supported_priority_fee_wei": sum(r["fees"]["priority_fee_wei"] or 0 for r in traced),
        "builder_tips_wei": sum(int(r.get("builder_tip_wei") or 0) for r in rows),
        "sandwiches": sum(1 for r in rows if r.get("sandwich", {}).get("role") == "back"),
        "gross_usd_trace_supported": round(sum(r.get("gross_usd") or 0 for r in traced), 2),
        "net_usd_trace_supported": round(
            sum(r.get("net_usd") or 0 for r in traced if r.get("net_usd") is not None), 2
        ),
        "unpriced_cycles": sum(1 for r in traced if r.get("price_source") == "unpriced"),
    }
