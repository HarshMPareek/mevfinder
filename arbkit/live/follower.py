"""Live follower: classify every new block of one chain from the chain's own node, store it in the live store
(`livedb`), audit the money of every stored batch independently (`audit`), and keep going.

Order of work, each loop:
1. Head: the blocks between the cursor and `head - confirmations`, oldest first, traced, in batches. On a restart
   the cursor is where the last run stopped, so missed blocks are classified first. A stretch older than the
   node's trace window (`[live] trace_window`: ~10,000 blocks on reth, ~128 on geth/Nitro path-scheme nodes) can
   no longer be traced; it goes to the backlog instead of stalling the head.
2. Backlog, when the head is caught up: newest range first; traced while it is still inside the trace window,
   otherwise classified from receipts only (rows stay unverified candidates, never counted) and stored with
   source `catchup_untraced`, so nothing is silently skipped.
3. Housekeeping: heartbeat in `meta`, block and trace cache pruning, slim transaction maps dropped after
   `TXMAP_DAYS`, a one-time background seed of singleton (V4-style) pools from Initialize logs.

A reorg shows up as a parent-hash mismatch against the previous block: stored blocks are compared with the
canonical chain going back, the mismatched ones are deleted (their days rebuilt) and re-classified.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from arbkit.live import store as livedb
from arbkit.verify.audit import audit
from arbkit.chain.spec import ROOT
from arbkit.chain.settings import Settings
from arbkit.scan.block import BlockContext
from arbkit.scan.pools import HistoricalPools
from arbkit.scan.scanner import Scanner
from arbkit.value.pricing import PriceBook
from arbkit.chain.rpc import SafeRpc
from mevlab.rpc import RpcError

MAX_ATTEMPTS = 3


def default_db_dir() -> Path:
    """$ARB_LIVE_DB (also read from .env), else ./live."""
    load_dotenv(ROOT / ".env", override=False)
    return Path(os.environ.get("ARB_LIVE_DB") or ROOT / "live")


def db_path(chain: str, directory: Path | None = None) -> Path:
    return (directory or default_db_dir()) / f"{chain}.sqlite"


def livedb_path(conn) -> Path:
    """The file behind an open store connection (for a second, read-only connection in a worker thread)."""
    return Path(conn.execute("PRAGMA database_list").fetchone()["file"])


def labelled_evidence(spec) -> tuple[set[int], set[str]]:
    """Blocks and transaction hashes of the chain's hand labels: their cached block and trace files are the evidence the
    regression re-reads (BSC, Arbitrum and Robinhood nodes cannot trace a block older than ~128 blocks again), so the
    cache prune skips them. Labels taken from live blocks (2026-10-31 reviews, 2026-11-01 census) had been due to vanish
    after a week."""
    path = spec.directory / "handlabels.json"
    try:
        rows = json.loads(path.read_text()).get("rows", [])
    except (OSError, ValueError):
        return set(), set()
    return (
        {int(r["block"]) for r in rows if r.get("block") is not None},
        {str(r["tx_hash"]).lower() for r in rows if r.get("tx_hash")},
    )


class Follower:
    def __init__(self, chain: str, directory: Path | None = None, log=print, max_batch: int | None = None):
        self.settings = Settings.load(chain)
        self.spec = self.settings.chain
        self.log = log
        self.path = db_path(self.spec.name, directory)
        self.conn = livedb.connect(self.path)
        self.scanner = Scanner(self.settings, lambda text: None)
        live = self.spec.live_setting
        self.confirmations = live("confirmations", 2)
        self.trace_window = live("trace_window", 100)
        self.cache_hours = live(
            "cache_hours", 168.0
        )  # a week of raw blocks and traces: tx pages and manual review
        self.max_batch = max_batch or live("max_batch", max(1, round(3000 / self.spec.block_time_ms)))
        self.poll = min(1.0, self.spec.block_time_ms / 2000)
        self.stop = asyncio.Event()
        self.ctx = BlockContext(self.spec)
        self.stats = {
            "blocks": 0,
            "rows": 0,
            "seconds": 0.0,
            "since": time.time(),
            "failures": 0,
            "reorgs": 0,
        }
        self.last_house = 0.0
        self.prune_task: asyncio.Task | None = None
        self.attempts: dict[int, int] = {}

    # ---- state ---------------------------------------------------------------------------------------------
    def backlog(self) -> list[list[int]]:
        return livedb.get_meta(self.conn, "backlog", [])

    def set_backlog(self, ranges: list[list[int]]) -> None:
        merged: list[list[int]] = []
        for lo, hi in sorted(r for r in ranges if r[0] <= r[1]):
            if merged and lo <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], hi)
            else:
                merged.append([lo, hi])
        livedb.set_meta(self.conn, "backlog", merged)

    def take_from_backlog(self, n: int) -> list[int]:
        ranges = self.backlog()
        if not ranges:
            return []
        lo, hi = ranges[-1]
        start = max(lo, hi - n + 1)
        ranges[-1] = [lo, start - 1]
        self.set_backlog(ranges)
        return list(range(start, hi + 1))

    # ---- main loop -----------------------------------------------------------------------------------------
    async def run(self, once: int | None = None) -> None:
        spec = self.spec
        async with (
            SafeRpc(self.settings.http_url) as rpc,
            SafeRpc(self.settings.trace_url) as trace_rpc,
            SafeRpc(self.settings.http_url) as seed_rpc,
        ):
            if await rpc.chain_id() != spec.chain_id:
                raise ValueError(f"Configured RPC is not {spec.name} (chain ID {spec.chain_id}).")
            self.rpc, self.trace_rpc = rpc, trace_rpc
            self.resolver = HistoricalPools(
                rpc,
                self.settings.data / "pool_meta.json",
                spec.factories,
                spec.pool_managers,
                max_log_range=spec.max_log_range,
            )
            self.book = PriceBook(self.settings.data.parent / "prices")
            head = await rpc.block_number()
            cursor = livedb.get_meta(self.conn, "cursor")
            if cursor is None:
                cursor = head - self.confirmations - 1
                livedb.set_meta(self.conn, "floor", cursor + 1)
                livedb.set_meta(self.conn, "started_at", time.time())
                livedb.set_meta(self.conn, "cursor", cursor)
            self.log(
                f"{spec.name}: head {head}, cursor {cursor}, {head - self.confirmations - cursor} blocks to catch up; "
                f"batch {self.max_batch}, trace window {self.trace_window}, store {self.path}"
            )
            seeding = (
                asyncio.create_task(self.seed_pools(seed_rpc))
                if spec.pool_managers and once is None
                else None
            )
            done = 0
            try:
                while not self.stop.is_set():
                    worked = await self.step()
                    done += worked
                    if once is not None and done >= once:
                        break
                    if time.time() - self.last_house > 10:
                        await self.housekeeping()
                    if not worked:
                        try:
                            await asyncio.wait_for(self.stop.wait(), self.poll)
                        except asyncio.TimeoutError:
                            pass
            finally:
                if seeding:
                    seeding.cancel()
                self.resolver.save()
                await self.heartbeat(final=True)

    async def step(self) -> int:
        """One unit of work; returns the number of blocks classified (0 = idle)."""
        head = await self.rpc.block_number()
        target = head - self.confirmations
        cursor = livedb.get_meta(self.conn, "cursor")
        self.head = head
        if target > cursor:
            lo = cursor + 1
            if target - cursor > self.trace_window:
                skip_to = target - self.trace_window
                self.set_backlog([*self.backlog(), [lo, skip_to]])
                self.log(
                    f"{self.spec.name}: blocks {lo}-{skip_to} are older than the trace window; queued for catch-up"
                )
                lo = skip_to + 1
                livedb.set_meta(self.conn, "cursor", skip_to)
                self.ctx.reset()
            numbers = list(range(lo, min(lo + self.max_batch - 1, target) + 1))
            await self.process(numbers, traced=True, advance=True)
            return len(numbers)
        numbers = self.take_from_backlog(self.max_batch * 4)
        if numbers:
            traced = target - numbers[0] <= self.trace_window
            await self.process(numbers, traced=traced, advance=False, ctx=BlockContext(self.spec))
            return len(numbers)
        return 0

    async def process(
        self,
        numbers: list[int],
        traced: bool,
        advance: bool,
        ctx: BlockContext | None = None,
        retry: bool = True,
        lead_in: int = 0,
    ) -> None:
        """Classify, store and audit a batch. `advance`: head work (moves the cursor, keeps the head's context).
        `lead_in` (re-classifying scattered blocks): before each block that does not follow the previous one, the
        `lead_in` blocks before it are classified but not stored, so the cross-block context (pool prices, trigger
        memory) is what the follower had; re-run alone, 44 of 1,080 Ethereum rows had flipped backrun/mid-block."""
        spec, t0 = self.spec, time.time()
        ctx = ctx or self.ctx
        rows, infos, failures = [], [], []
        previous = None
        for number in numbers:
            if lead_in and (previous is None or number != previous + 1):
                ctx.reset()
                for n in range(number - lead_in, number):
                    if not (self.settings.data / "blocks" / f"{n}.json.gz").exists():
                        ctx.reset()
                        continue
                    try:
                        await self.scanner.scan_block(
                            self.rpc,
                            self.trace_rpc if traced else None,
                            n,
                            self.resolver,
                            ctx,
                            "candidates",
                            False,
                            cache_level=3,
                        )
                    except (RpcError, ValueError, OSError):
                        ctx.reset()
            previous = number
            try:
                found, info = await self.scanner.scan_block(
                    self.rpc,
                    self.trace_rpc if traced else None,
                    number,
                    self.resolver,
                    ctx,
                    "candidates",
                    False,
                    cache_level=3,
                )
                rows.extend(found)
                infos.append(info)
            except (RpcError, ValueError, OSError) as exc:
                if advance and ("hashes disagree" in str(exc) or "changed during" in str(exc)):
                    # The chain moved under us: drop this batch, rewind to the common ancestor, classify again.
                    await self.reconcile(infos[0]["number"] if infos else number)
                    return
                failures.append({"block": number, "error": str(exc)})
                ctx.reset()
        if not infos and not failures:
            return
        self.resolver.save()
        report = await self.scanner.finish(
            self.rpc,
            self.trace_rpc,
            rows,
            infos,
            failures,
            numbers,
            "candidates" if traced else "none",
            self.book,
            extra={"live": True},
        )
        source = "live" if traced else "catchup_untraced"
        stored = livedb.store_report(self.conn, spec, report, source) if infos else {"rows": 0}
        if infos:
            try:
                livedb.store_audit(
                    self.conn,
                    infos[0]["number"],
                    infos[-1]["number"],
                    audit(spec, report, self.settings.data),
                    report,
                )
            except (OSError, KeyError, ValueError) as exc:
                self.log(f"{spec.name}: audit of {infos[0]['number']}-{infos[-1]['number']} failed: {exc}")
        if not retry:  # re-classification: a block that cannot be redone keeps its stored rows
            self.stats["blocks"] += len(infos)
            self.stats["rows"] += stored["rows"]
            self.stats["failures"] += len(failures)
            return
        retry = []
        for f in failures:
            n = f["block"]
            self.attempts[n] = self.attempts.get(n, 0) + 1
            if self.attempts[n] >= MAX_ATTEMPTS:
                livedb.note_gap(self.conn, n, n, f["error"][:200])
                self.attempts.pop(n, None)
            else:
                retry.append([n, n])
        if retry:
            self.set_backlog([*self.backlog(), *retry])
        if advance:
            classified = [i["number"] for i in infos] + [f["block"] for f in failures]
            if classified:
                livedb.set_meta(self.conn, "cursor", max(classified))
        self.stats["blocks"] += len(infos)
        self.stats["rows"] += stored["rows"]
        self.stats["seconds"] += time.time() - t0
        self.stats["failures"] += len(failures)

    async def reconcile(self, number: int, depth: int = 128) -> None:
        """The node's block `number` does not extend what we stored: walk back to the common ancestor (blocks
        below `number` whose stored hash differs from the node's are deleted) and set the cursor there."""
        self.stats["reorgs"] += 1
        bad = []
        for n in range(number - 1, max(number - 1 - depth, 0), -1):
            row = self.conn.execute("SELECT hash FROM blocks WHERE number=?", (n,)).fetchone()
            if row is None:
                break
            header = await self.rpc.get_block(n, False)
            if header and header["hash"] == row["hash"]:
                break
            bad.append(n)
        livedb.delete_blocks(self.conn, self.spec, bad)
        cursor = min(bad) - 1 if bad else number - 1
        livedb.set_meta(self.conn, "cursor", cursor)
        self.ctx.reset()
        self.log(
            f"{self.spec.name}: reorg at {number}: {len(bad)} stored blocks replaced, cursor back to {cursor}"
        )

    async def reclassify(
        self,
        first: int | None = None,
        last: int | None = None,
        batch: int = 50,
        all_sources: bool = False,
        numbers: list[int] | None = None,
        lead_in: int = 0,
    ) -> dict:
        """Re-run stored live blocks through the current classifier (cached blocks and traces first, the node for the
        rest) and replace their rows and day totals. Untraced catch-up and imported blocks are left alone, and a block
        that cannot be redone keeps what is stored."""
        spec = self.spec
        # imported history (October reports) too when asked: its rows were written by an older classifier and stay
        # on the site until re-run from the block/trace cache
        sources = "source='live' OR source LIKE 'import%'" if all_sources else "source='live'"
        wanted = (
            set(numbers) if numbers is not None else None
        )  # only these blocks (a fix that touches a known few)
        numbers = [
            r["number"]
            for r in self.conn.execute(
                f"SELECT number FROM blocks WHERE ({sources}) AND number BETWEEN ? AND ? ORDER BY number",
                (first if first is not None else 0, last if last is not None else 2**62),
            )
            if wanted is None or r["number"] in wanted
        ]
        numbers = [n for n in numbers if (self.settings.data / "blocks" / f"{n}.json.gz").exists()]
        async with SafeRpc(self.settings.http_url) as rpc, SafeRpc(self.settings.trace_url) as trace_rpc:
            if await rpc.chain_id() != spec.chain_id:
                raise ValueError(f"Configured RPC is not {spec.name} (chain ID {spec.chain_id}).")
            self.rpc, self.trace_rpc = rpc, trace_rpc
            self.resolver = HistoricalPools(
                rpc,
                self.settings.data / "pool_meta.json",
                spec.factories,
                spec.pool_managers,
                max_log_range=spec.max_log_range,
            )
            self.book = PriceBook(self.settings.data.parent / "prices")
            ctx, done, t0 = BlockContext(spec), 0, time.time()
            if lead_in:
                batch = max(
                    batch, 200
                )  # scattered blocks: store (and rebuild their days) once per 200 blocks
            for i in range(0, len(numbers), batch):
                chunk = numbers[i : i + batch]
                await self.process(chunk, traced=True, advance=False, ctx=ctx, retry=False, lead_in=lead_in)
                done += len(chunk)
                if i // batch % 20 == 0:
                    self.log(
                        f"{spec.name}: re-classified {done}/{len(numbers)} blocks ({time.time() - t0:.0f} s)"
                    )
            self.resolver.save()
        return {
            "blocks": len(numbers),
            "failures": self.stats["failures"],
            "seconds": round(time.time() - t0),
        }

    # ---- housekeeping --------------------------------------------------------------------------------------
    async def heartbeat(self, final: bool = False) -> None:
        cursor = livedb.get_meta(self.conn, "cursor")
        head = getattr(self, "head", None)
        elapsed = max(time.time() - self.stats["since"], 1e-9)
        info = {
            "at": datetime.now(timezone.utc).isoformat(),
            "pid": os.getpid(),
            "head": head,
            "cursor": cursor,
            "lag_blocks": (head - cursor) if head is not None and cursor is not None else None,
            "lag_seconds": round((head - cursor) * self.spec.block_time_ms / 1000, 1)
            if head is not None and cursor is not None
            else None,
            "blocks_per_minute": round(self.stats["blocks"] / elapsed * 60, 1),
            "chain_blocks_per_minute": round(60_000 / self.spec.block_time_ms, 1),
            "ms_per_block": round(self.stats["seconds"] / self.stats["blocks"] * 1000)
            if self.stats["blocks"]
            else None,
            "backlog_blocks": sum(hi - lo + 1 for lo, hi in self.backlog()),
            "rows_stored": self.stats["rows"],
            "failures": self.stats["failures"],
            "reorgs": self.stats["reorgs"],
            "pool_cache": len(self.resolver.table),
            "stopped": final,
        }
        livedb.set_meta(self.conn, "heartbeat", info)
        if final or not self.stats.get("logged") or time.time() - self.stats["logged"] > 300:
            self.stats["logged"] = time.time()
            self.log(
                f"{self.spec.name}: cursor {cursor} lag {info['lag_blocks']} blocks ({info['lag_seconds']} s), "
                f"{info['blocks_per_minute']}/min vs chain {info['chain_blocks_per_minute']}/min, {info['ms_per_block']} ms/block, "
                f"backlog {info['backlog_blocks']}, rows {self.stats['rows']}"
            )
        self.stats.update(blocks=0, rows=0, seconds=0.0, since=time.time())

    async def housekeeping(self) -> None:
        self.last_house = time.time()
        try:
            self.head = await self.rpc.block_number()
        except RpcError:
            pass
        await self.heartbeat()
        last_prune = livedb.get_meta(self.conn, "last_prune", 0)
        if time.time() - last_prune < 1800:
            return
        if self.prune_task is not None and not self.prune_task.done():
            return  # the previous pass is still running in the background
        livedb.set_meta(self.conn, "last_prune", time.time())
        livedb.drop_old_txmaps(self.conn)
        floor = livedb.get_meta(self.conn, "floor", 0)
        started = livedb.get_meta(self.conn, "started_at", time.time())
        keep_blocks = int(self.cache_hours * 3_600_000 / self.spec.block_time_ms)
        cutoff = livedb.get_meta(self.conn, "cursor") - keep_blocks
        pruned_to = livedb.get_meta(self.conn, "cache_pruned_to", floor - 1)
        livedb.set_meta(self.conn, "cache_pruned_to", max(pruned_to, cutoff - 1))
        labelled_blocks, keep_txs = labelled_evidence(self.spec)
        path = livedb_path(self.conn)
        # Everything below reads or deletes files and the store's block maps: it runs on a worker thread while the
        # follower keeps classifying. Awaited on the event loop it stopped the follower for minutes every 30 min (a
        # scandir + stat of ~14M cached BSC traces, 3.5M on Robinhood); those chains' nodes trace only ~128 blocks back,
        # so every pause sent the blocks behind it to untraced catch-up, which is never counted: BSC 26,083 blocks on
        # 2026-11-07 (~14 % of the day), Robinhood 61,014 (~11 %), both rising as the cache grew.
        self.prune_task = asyncio.create_task(
            asyncio.to_thread(
                self._prune_files,
                path,
                range(max(pruned_to + 1, floor), cutoff),
                labelled_blocks,
                keep_txs,
                started,
                time.time() - self.cache_hours * 3600,
            )
        )
        self.prune_task.add_done_callback(self._pruned)

    def _prune_files(self, path, numbers, labelled_blocks, keep_txs, started, limit) -> dict:
        out = {"removed": 0, "failed_attempts": None, "error": None}
        try:  # failed attempts: a full read of the stored block maps, on its own read-only connection
            out["failed_attempts"] = livedb.failed_attempts(livedb.connect(path, readonly=True))
        except Exception as exc:  # noqa: BLE001 - a figure for the overview must never stop the follower
            out["error"] = f"failed-attempts figure not refreshed: {exc}"
        blocks_dir = self.settings.data / "blocks"
        for n in numbers:  # only blocks this follower wrote: older caches stay
            if n in labelled_blocks:
                continue  # a hand-labelled transaction's block is regression evidence: never pruned
            try:
                (blocks_dir / f"{n}.json.gz").unlink()
                out["removed"] += 1
            except FileNotFoundError:
                pass
        traces_dir = self.settings.data / "traces"
        if traces_dir.exists():
            with os.scandir(traces_dir) as entries:
                for entry in entries:
                    if self.stop.is_set():
                        break
                    try:
                        mtime = entry.stat().st_mtime
                        if (
                            started <= mtime < limit
                            and entry.name.split("_")[-1].split(".")[0].lower() not in keep_txs
                        ):
                            os.unlink(entry.path)
                            out["removed"] += 1
                    except FileNotFoundError:
                        pass
        return out

    def _pruned(self, task: asyncio.Task) -> None:
        """Back on the event loop: store the failed-attempts figure and log the pass."""
        try:
            out = task.result()
        except Exception as exc:  # noqa: BLE001 - housekeeping must never stop the follower
            self.log(f"{self.spec.name}: housekeeping failed: {exc}")
            return
        if out["failed_attempts"] is not None:
            livedb.set_meta(self.conn, "failed_attempts", out["failed_attempts"])
        if out["error"]:
            self.log(f"{self.spec.name}: {out['error']}")
        self.log(
            f"{self.spec.name}: housekeeping removed {out['removed']} cached files older than {self.cache_hours} h"
        )

    async def seed_pools(self, rpc) -> None:
        """Once per manager: Initialize logs from the head back until the node refuses (pruned receipts) or block 0."""
        await asyncio.sleep(5)
        chunk = self.spec.live_setting("seed_chunk", 10_000)
        seeder = HistoricalPools(rpc, None, self.spec.factories, self.spec.pool_managers)
        seeder.table = self.resolver.table  # share the follower's table: found pools are usable at once
        seeder._pending = self.resolver._pending
        for manager in self.spec.pool_managers:
            state = livedb.get_meta(self.conn, f"pool_seed:{manager}", {})
            if state.get("done"):
                continue
            hi = state.get("low", await rpc.block_number() + 1) - 1
            found = state.get("found", 0)
            self.log(f"{self.spec.name}: seeding pools of {manager} below block {hi + 1}")
            while hi > 0 and not self.stop.is_set():
                lo = max(0, hi - chunk + 1)
                try:
                    found += await seeder.seed(manager, lo, hi, chunk)
                except RpcError:
                    state = {
                        "done": True,
                        "low": hi + 1,
                        "found": found,
                        "stopped": "node refused older logs (pruned receipts)",
                    }
                    break
                hi = lo - 1
                state = {"done": hi <= 0, "low": lo, "found": found}
                livedb.set_meta(self.conn, f"pool_seed:{manager}", state)
                await asyncio.sleep(0.2)
            livedb.set_meta(self.conn, f"pool_seed:{manager}", state)
            self.resolver.save()
            self.log(f"{self.spec.name}: pool seed of {manager}: {found} pools, {state}")


def reclassify(
    chain: str,
    directory: Path | None = None,
    first: int | None = None,
    last: int | None = None,
    all_sources: bool = False,
    numbers: list[int] | None = None,
    lead_in: int = 0,
) -> int:
    def log(text):
        print(f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} {text}", flush=True)

    result = asyncio.run(
        Follower(chain, directory, log).reclassify(
            first, last, all_sources=all_sources, numbers=numbers, lead_in=lead_in
        )
    )
    log(f"{chain}: re-classification done: {result}")
    return 0


def follow(
    chain: str, directory: Path | None = None, once: int | None = None, max_batch: int | None = None
) -> int:
    def log(text):
        print(f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} {text}", flush=True)

    follower = Follower(chain, directory, log, max_batch)

    async def main():
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, follower.stop.set)
        while not follower.stop.is_set():
            try:
                await follower.run(once)
                return
            except (RpcError, OSError) as exc:  # node restarting: wait and resume from the cursor
                log(f"{chain}: {exc}; retrying in 15 s")
                try:
                    await asyncio.wait_for(follower.stop.wait(), 15)
                except asyncio.TimeoutError:
                    pass

    asyncio.run(main())
    return 0
