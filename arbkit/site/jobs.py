"""On-demand scans for the explorer UI: a block, a range, a transaction or a UTC time is scanned when asked for,
never continuously. Jobs run one at a time per chain in a worker thread; progress lines are kept for the page to poll.

`block_at(rpc, ts)` finds the first block whose timestamp is >= `ts` by binary search over block headers (exact on
every chain here: timestamps are monotone). Reports are saved exactly as the CLI saves them (`reports/<chain>/`),
so the dashboard, the daily figures and the proof pages read the same files.
"""

from __future__ import annotations

import asyncio
import re
import threading
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

from arbkit.chain.rpc import SafeRpc
from arbkit.chain.settings import Settings
from arbkit.scan.scanner import Scanner
from mevlab.hexutil import to_int

MAX_BLOCKS_PER_JOB = 60
RECENT_JOBS = 50


def parse_time(text: str) -> int:
    """'2026-10-07 12:34:56', '2026-10-07T12:34:56Z', '2026-10-07' (midnight) or a unix timestamp -> unix seconds (UTC)."""
    t = text.strip()
    if re.fullmatch(r"\d{9,11}", t):
        return int(t)
    for fmt in (
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d",
    ):
        try:
            return int(datetime.strptime(t, fmt).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    raise ValueError("Give a UTC time as YYYY-MM-DD HH:MM[:SS] or a unix timestamp.")


async def block_at(rpc, ts: int, head: int | None = None) -> dict:
    """First block with timestamp >= ts: {"number", "timestamp", "head", "head_timestamp"}."""
    head = head if head is not None else await rpc.block_number()
    head_block = await rpc.get_block(head, False)
    head_ts = to_int(head_block["timestamp"])
    if ts >= head_ts:
        return {
            "number": head,
            "timestamp": head_ts,
            "head": head,
            "head_timestamp": head_ts,
            "note": "time is at or after the chain head",
        }
    lo, hi = 1, head
    while lo < hi:
        mid = (lo + hi) // 2
        block = await rpc.get_block(mid, False)
        if to_int(block["timestamp"]) >= ts:
            hi = mid
        else:
            lo = mid + 1
    block = await rpc.get_block(lo, False)
    return {"number": lo, "timestamp": to_int(block["timestamp"]), "head": head, "head_timestamp": head_ts}


def parse_blocks_text(text: str) -> list[int]:
    """'123', '100-110', '100 110', '1,2,3' -> sorted block numbers (ranges inclusive)."""
    blocks: set[int] = set()
    parts = [p for p in re.split(r"[,\s]+", text.strip()) if p]
    if len(parts) == 2 and all(re.fullmatch(r"\d+", p) for p in parts):
        parts = [parts[0] + "-" + parts[1]]
    for part in parts:
        if not re.fullmatch(r"\d+(?:-\d+)?", part):
            raise ValueError("Use a block number, START-END, or a comma-separated list.")
        bounds = [int(x) for x in part.split("-")]
        start, end = bounds[0], bounds[-1]
        if start > end:
            raise ValueError("Start block must not exceed the end block.")
        if end - start + 1 > MAX_BLOCKS_PER_JOB:
            raise ValueError(
                f"At most {MAX_BLOCKS_PER_JOB} blocks per request (that range has {end - start + 1})."
            )
        blocks.update(range(start, end + 1))
    if not blocks:
        raise ValueError("No block numbers given.")
    if len(blocks) > MAX_BLOCKS_PER_JOB:
        raise ValueError(f"At most {MAX_BLOCKS_PER_JOB} blocks per request.")
    return sorted(blocks)


class Job:
    def __init__(
        self, chain: str, blocks: list[int] | None, transaction: str | None, trace_mode: str, label: str
    ):
        self.id = uuid.uuid4().hex[:12]
        self.chain = chain
        self.blocks = blocks
        self.transaction = transaction
        self.trace_mode = trace_mode
        self.label = label
        self.status = "queued"  # queued | running | done | failed
        self.progress: list[str] = []
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.finished_at: str | None = None
        self.report_path: str | None = None
        self.scanned_blocks: list[int] = []
        self.summary: dict | None = None
        self.error: str | None = None

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "chain": self.chain,
            "blocks": self.blocks,
            "transaction": self.transaction,
            "trace_mode": self.trace_mode,
            "label": self.label,
            "status": self.status,
            "progress": self.progress[-40:],
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "report": self.report_path,
            "scanned_blocks": self.scanned_blocks,
            "summary": self.summary,
            "error": self.error,
        }


class JobManager:
    """One worker thread per chain; jobs for a chain run in order."""

    def __init__(self, reports_root: Path | None = None):
        self.reports_root = reports_root
        self.jobs: dict[str, Job] = {}
        self.queues: dict[str, list[Job]] = {}
        self.lock = threading.Lock()
        self.workers: dict[str, threading.Thread] = {}

    def submit(
        self,
        chain: str,
        blocks: list[int] | None = None,
        transaction: str | None = None,
        trace_mode: str = "candidates",
        label: str = "",
    ) -> Job:
        if not blocks and not transaction:
            raise ValueError("Nothing to scan.")
        job = Job(chain, blocks, transaction, trace_mode, label)
        with self.lock:
            self.jobs[job.id] = job
            self.queues.setdefault(chain, []).append(job)
            if len(self.jobs) > RECENT_JOBS:
                for old in sorted(self.jobs.values(), key=lambda j: j.created_at)[:-RECENT_JOBS]:
                    if old.status in {"done", "failed"}:
                        self.jobs.pop(old.id, None)
            worker = self.workers.get(chain)
            if worker is None or not worker.is_alive():
                worker = threading.Thread(target=self._work, args=(chain,), daemon=True, name=f"scan-{chain}")
                self.workers[chain] = worker
                worker.start()
        return job

    def _next(self, chain: str) -> Job | None:
        with self.lock:
            queue = self.queues.get(chain) or []
            return queue.pop(0) if queue else None

    def _work(self, chain: str) -> None:
        while True:
            job = self._next(chain)
            if job is None:
                return
            self._run(job)

    def _run(self, job: Job) -> None:
        from arbkit.cli.reports import save_report

        job.status = "running"
        try:
            settings = Settings.load(job.chain)
            if self.reports_root is not None:
                settings = Settings(
                    settings.chain,
                    settings.http_url,
                    settings.trace_url,
                    settings.data,
                    self.reports_root / job.chain,
                )
            scanner = Scanner(settings, progress=lambda text: job.progress.append(text))
            extra = {"explorer_job": job.id, "explorer_label": job.label}
            report = asyncio.run(scanner.run(job.blocks or [], job.trace_mode, False, job.transaction, extra))
            stem = f"{settings.chain.name}_ui_{job.id}"
            path = save_report(report, settings.reports, stem=stem)
            job.report_path = str(path)
            job.scanned_blocks = [b["number"] for b in report["blocks"]]
            summary = report["summary"]
            job.summary = {
                "transactions": summary["transactions"],
                "cycles": summary["assessments"].get("trace_supported", 0),
                "losing": summary["assessments"].get("trace_supported_loss", 0),
                "review": summary["assessments"].get("needs_review", 0),
                "sandwiches": summary.get("sandwiches", 0),
                "gross_usd": summary.get("gross_usd_trace_supported"),
                "net_usd": summary.get("net_usd_trace_supported"),
                "failures": report["failures"],
                "focus_transaction": report.get("focus_transaction"),
            }
            job.status = "done" if report["complete"] else "failed"
            if not report["complete"]:
                job.error = "; ".join(f"block {f['block']}: {f['error']}" for f in report["failures"])
        except Exception as exc:  # noqa: BLE001 - the page shows the error
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
            job.progress.append(traceback.format_exc().strip().splitlines()[-1])
        finally:
            job.finished_at = datetime.now(timezone.utc).isoformat()

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def recent(self, chain: str | None = None) -> list[dict]:
        jobs = [j for j in self.jobs.values() if chain is None or j.chain == chain]
        return [j.to_json() for j in sorted(jobs, key=lambda j: j.created_at, reverse=True)]


def locate(settings: Settings, text: str) -> dict:
    """Resolve a search: block number / range, transaction hash, or UTC time -> what to open or scan."""
    t = text.strip()
    if re.fullmatch(r"0x[0-9a-fA-F]{64}", t):
        return {"kind": "transaction", "hash": t.lower()}
    try:
        blocks = parse_blocks_text(t)
        return {"kind": "blocks", "blocks": blocks}
    except ValueError as block_error:
        try:
            ts = parse_time(t)
        except ValueError:
            raise ValueError(
                f"{block_error} Or give a UTC time (YYYY-MM-DD HH:MM[:SS]) or a transaction hash."
            ) from None

    async def find():
        async with SafeRpc(settings.http_url) as rpc:
            return await block_at(rpc, ts)

    found = asyncio.run(find())
    return {
        "kind": "time",
        "time": ts,
        "block": found["number"],
        "block_timestamp": found["timestamp"],
        "head": found["head"],
        "head_timestamp": found["head_timestamp"],
        "note": found.get("note"),
    }
