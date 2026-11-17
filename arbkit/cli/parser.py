"""Command-line arguments: every command, its options and their types."""

from __future__ import annotations

import argparse
import re
from datetime import datetime
from pathlib import Path

from arbkit.chain.settings import DEFAULT_CHAIN

MAX_BLOCKS = 10_000


def parse_blocks(values):
    if values in (["latest"], ["sample"]):
        return values
    if len(values) == 2 and all(re.fullmatch(r"\d+", x) for x in values):
        values = [values[0] + "-" + values[1]]
    blocks = set()
    for part in re.split(r"[,\s]+", " ".join(values).strip()):
        if not re.fullmatch(r"\d+(?:-\d+)?", part):
            raise ValueError(
                "Use a block number, START END, START-END, or a comma-separated list; ranges are inclusive."
            )
        bounds = [int(x) for x in part.split("-")]
        start, end = bounds[0], bounds[-1]
        if start > end:
            raise ValueError("Start block must be less than or equal to end block.")
        if end - start > MAX_BLOCKS or len(blocks) + end - start + 1 > MAX_BLOCKS + 1:
            raise ValueError("Scan at most 10,001 blocks per command; split larger ranges into several runs.")
        blocks.update(range(start, end + 1))
    return sorted(blocks)


def tx_hash(value):
    if not re.fullmatch(r"0x[0-9a-fA-F]{64}", value):
        raise argparse.ArgumentTypeError("Expected a full 0x-prefixed transaction hash (64 hex characters).")
    return value.lower()


def positive(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("Must be greater than zero.")
    return number


def iso_date(value):
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError("Expected a UTC date as YYYY-MM-DD.") from None
    return value


def parser():
    p = argparse.ArgumentParser(
        description="Multi-chain arbitrage classifier and terminal dashboard. Run without arguments for prompts."
    )
    p.add_argument(
        "-c",
        "--chain",
        default=None,
        help=f"Chain directory under chains/ (default: $ARB_CHAIN or {DEFAULT_CHAIN})",
    )
    sub = p.add_subparsers(dest="command")
    _scan_commands(sub)
    _site_command(sub)
    _verify_commands(sub)
    _live_commands(sub)
    return p


def _scan_commands(sub) -> None:
    """Scanning: scan / sample / tx / day / blind, review, reprice, show, daily, chains."""
    scan = sub.add_parser("scan", help="Download and classify one block, an inclusive range or a list")
    scan.add_argument("blocks", nargs="+", help="NUMBER | START END | START-END | N,N | latest")
    sample = sub.add_parser("sample", help="Classify five recent blocks")
    tx = sub.add_parser("tx", help="Download the transaction's entire block and inspect it")
    tx.add_argument("hash", type=tx_hash)
    day = sub.add_parser(
        "day", help="Classify one UTC day: every block (--full) or N evenly spaced blocks (--sample)"
    )
    day.add_argument("date", type=iso_date, help="UTC day, YYYY-MM-DD")
    group = day.add_mutually_exclusive_group()
    group.add_argument(
        "--sample", type=positive, default=None, help="Number of evenly spaced blocks (default 200)"
    )
    group.add_argument("--full", action="store_true", help="Every block of the day, in resumable chunks")
    day.add_argument("--chunk", type=positive, default=500, help="Blocks per saved report with --full")
    blind = sub.add_parser(
        "blind",
        help="Blind sample: N random blocks of the last day (seeded), scanned and written to a review sheet",
    )
    blind.add_argument("count", type=positive)
    blind.add_argument("--seed", type=int, default=20261008)
    blind.add_argument(
        "--span",
        type=positive,
        default=None,
        help="Blocks back from the head to draw from (default: one day)",
    )
    for command in (scan, sample, tx, day, blind):
        command.add_argument(
            "--traces",
            choices=["candidates", "swaps", "all", "none"],
            default="candidates",
            help="candidates: cycles/multileg/unknown; swaps: every decoded swap; all: every successful user tx",
        )
        command.add_argument("--refresh", action="store_true", help="Refetch cached blocks and traces")
        command.add_argument("--data-dir", type=Path)
        command.add_argument("--output-dir", type=Path)
        command.add_argument(
            "--offline-prices", action="store_true", help="Never fetch Binance prices (USD stays unpriced)"
        )
    review = sub.add_parser(
        "review",
        help="Write a per-row review sheet (route, funding, payouts, flags, explorer link) for a saved report",
    )
    review.add_argument("report", type=Path)
    reprice = sub.add_parser(
        "reprice", help="Re-value a saved report's USD figures with fresh Binance prices (no RPC)"
    )
    reprice.add_argument("report", type=Path)
    show = sub.add_parser("show", help="Display a saved report without any RPC requests")
    show.add_argument(
        "report", type=Path, nargs="?", default=None, help="Defaults to reports/<chain>/latest.json"
    )
    show.add_argument("--tx", type=tx_hash, help="Inspect one transaction in this report")
    for command in (scan, sample, tx, show, day):
        command.add_argument(
            "--all", action="store_true", dest="show_all", help="Display non-arbitrage transactions too"
        )
        command.add_argument(
            "--class",
            dest="classes",
            action="append",
            metavar="CLASS",
            help="Display only this class, e.g. probe, reverted, loss_cycle; repeat for multiple classes",
        )
        command.add_argument(
            "--limit", type=positive, default=30, help="Max displayed transactions; every row is exported"
        )
    daily = sub.add_parser("daily", help="Aggregate every saved report of the chain per UTC day (no RPC)")
    daily.add_argument("--output-dir", type=Path)
    daily.add_argument("--json", action="store_true", help="Print the JSON instead of the table")
    sub.add_parser("chains", help="List the configured chains and their settings")


def _site_command(sub) -> None:
    """The website / dashboard server."""
    ui = sub.add_parser(
        "ui", help="Serve the web dashboard over the saved reports of every chain (localhost)"
    )
    ui.add_argument("--port", type=positive, default=8787)
    ui.add_argument("--output-dir", type=Path, help="Reports root (default reports/)")
    ui.add_argument(
        "--stop", action="store_true", help="Stop the running dashboard server and free its memory"
    )
    ui.add_argument("--status", action="store_true", help="Show whether a dashboard server is running")
    ui.add_argument(
        "--public",
        action="store_true",
        help="Visitor view: labels and money only; no proofs, raw rows, scans or validation; rate-limited",
    )
    ui.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address (default 127.0.0.1; put a reverse proxy in front for a public site)",
    )
    ui.add_argument(
        "--live-db",
        type=Path,
        help="Directory of the followers' live stores (<chain>.sqlite); those chains are served from them",
    )


def _verify_commands(sub) -> None:
    """Verification: audits, explorer copies, builders, recall and bracket screens, hand review and labels, facts, coverage, registry."""
    aud = sub.add_parser(
        "audit",
        help="Independently recompute fees and cycle amounts of a report from raw receipts and traces",
    )
    aud.add_argument("report", type=Path, nargs="?", default=None)
    aud.add_argument("--data-dir", type=Path)
    aud.add_argument("--json", action="store_true")
    exp = sub.add_parser(
        "explorer-check",
        help="Reconcile saved explorer page copies (chains/<chain>/validation/explorer/) with the classified rows",
    )
    exp.add_argument(
        "report",
        type=Path,
        nargs="*",
        default=None,
        help="Reports holding the rows (default: every report of the chain)",
    )
    exp.add_argument(
        "--pages", type=Path, default=None, help="Directory of <hash>.txt / <hash>.json page copies"
    )
    exp.add_argument("--data-dir", type=Path)
    pull = sub.add_parser(
        "explorer-pull",
        help="Pull explorer copies (Blockscout API v2 / BSCTrace's NodeReal API) of a review sheet's rows or given hashes into chains/<chain>/validation/explorer/",
    )
    pull.add_argument("hashes", type=tx_hash, nargs="*", help="Transaction hashes to pull")
    pull.add_argument(
        "--sheet",
        type=Path,
        action="append",
        default=None,
        help="Hand-review sheet JSON whose rows to pull (repeatable)",
    )
    pull.add_argument(
        "--refetch-partial",
        action="store_true",
        help="Re-pull copies whose last explorer-check was partial (internal transfers not indexed, page cap, share tokens)",
    )
    pull.add_argument(
        "--upgrade-text",
        action="store_true",
        help="Re-pull page-text copies (<hash>.txt) as API copies; the text moves to explorer/superseded/",
    )
    pull.add_argument(
        "--access", choices=["direct", "browser"], default=None, help="Override chain.toml blockscout_access"
    )
    pull.add_argument("--limit", type=int, default=None, help="Pull at most this many hashes")
    pull.add_argument(
        "--sleep", type=float, default=0.3, help="Pause between requests in seconds (default 0.3)"
    )
    pull.add_argument(
        "--skip-existing", action="store_true", help="Skip hashes that already have a JSON copy"
    )
    bld = sub.add_parser(
        "builders",
        help="Block-producer economics of a report: priority fees, in-tx tips, proposer payments, searcher share",
    )
    bld.add_argument("report", type=Path, nargs="?", default=None)
    bld.add_argument("--data-dir", type=Path)
    bld.add_argument("--json", action="store_true")
    rec = sub.add_parser(
        "recall",
        help="Decoder-free loop screen over every successful transaction of a report's cached blocks (misses hunt)",
    )
    rec.add_argument("report", type=Path, nargs="?", default=None)
    rec.add_argument("--data-dir", type=Path)
    rec.add_argument("--json", action="store_true")
    brk = sub.add_parser(
        "brackets",
        help="Direction-agnostic bracket screen over a report's cached blocks (sandwich misses hunt)",
    )
    brk.add_argument("report", type=Path, nargs="?", default=None)
    brk.add_argument(
        "--all",
        action="store_true",
        help="Every scanned block of the chain (newest classification per block) instead of one report",
    )
    brk.add_argument("--data-dir", type=Path)
    brk.add_argument("--json", action="store_true")
    hrv = sub.add_parser(
        "handreview",
        help="Draw fresh rows for reading by hand, write the evidence sheet, lock verdicts into handlabels.json",
    )
    hrv.add_argument("count", type=int, nargs="?", default=50, help="Rows to draw (default 50)")
    hrv.add_argument("--seed", type=int, default=None, help="Draw seed (default: today's date as YYYYMMDD)")
    hrv.add_argument(
        "--kind", choices=["cycles", "losing", "sandwiches", "families", "review", "all"], default="cycles"
    )
    hrv.add_argument(
        "--lock", type=Path, default=None, help="Sheet JSON whose verdicts to append to handlabels.json"
    )
    hrv.add_argument(
        "--progress", action="store_true", help="Hand-labelled counts per class against the targets"
    )
    hrv.add_argument(
        "--random-only",
        action="store_true",
        help="Draw only from blocks that came from a seeded random (blind) sample",
    )
    hrv.add_argument("--data-dir", type=Path)
    facts = sub.add_parser(
        "facts", help="Measured block cadence, fee and ordering behaviour from a saved report"
    )
    facts.add_argument("report", type=Path, nargs="?", default=None)
    facts.add_argument("--data-dir", type=Path)
    facts.add_argument("--json", action="store_true")
    cov = sub.add_parser(
        "coverage", help="Decoder gaps among trace venues of a report, and recall vs a broader-trace report"
    )
    cov.add_argument(
        "report", type=Path, nargs="?", default=None, help="Defaults to reports/<chain>/latest.json"
    )
    cov.add_argument(
        "--against",
        type=Path,
        default=None,
        help="Broader-trace report over the same blocks (e.g. --traces all)",
    )
    cov.add_argument("--data-dir", type=Path)
    cov.add_argument("--json", action="store_true")
    hand = sub.add_parser(
        "handlabels",
        help="Scan the blocks of chains/<chain>/handlabels.json and compare every hand-labelled transaction",
    )
    hand.add_argument("--traces", choices=["candidates", "swaps", "all", "none"], default="candidates")
    hand.add_argument("--refresh", action="store_true")
    hand.add_argument("--data-dir", type=Path)
    hand.add_argument("--output-dir", type=Path)
    hand.add_argument("--offline-prices", action="store_true")
    verify = sub.add_parser(
        "verify-registry", help="Check every DEX registry entry on chain (bytecode + probe call)"
    )
    verify.add_argument("--data-dir", type=Path)


def _live_commands(sub) -> None:
    """The live store: follow, import, re-classify, maintenance, status."""
    fol = sub.add_parser(
        "follow",
        help="Classify every new block from the chain's node into the live store, forever (catches up on restart)",
    )
    fol.add_argument("--db-dir", type=Path, help="Live store directory (default: $ARB_LIVE_DB or ./live)")
    fol.add_argument("--once", type=int, help="Stop after this many blocks (testing)")
    fol.add_argument(
        "--batch", type=int, help="Blocks per stored batch (default from [live] or ~3 s of chain)"
    )
    imp = sub.add_parser(
        "live-import", help="Load saved reports (reports/<chain>/*.json) into the live store"
    )
    imp.add_argument("--db-dir", type=Path)
    imp.add_argument("--reports-dir", type=Path, help="Default: reports/<chain>")
    rcl = sub.add_parser(
        "live-reclassify",
        help="Re-run stored live blocks through the current classifier from the block/trace cache and replace them",
    )
    rcl.add_argument("--db-dir", type=Path)
    rcl.add_argument("--from", dest="first", type=int)
    rcl.add_argument("--to", dest="last", type=int)
    rcl.add_argument(
        "--all-sources",
        action="store_true",
        help="Also re-run imported history (October reports) from the cache",
    )
    rcl.add_argument(
        "--blocks-file", type=Path, help="Only these stored blocks: a JSON list of block numbers"
    )
    rcl.add_argument(
        "--lead-in",
        type=int,
        default=0,
        help="With --blocks-file: classify N blocks before each run first (not stored) for context",
    )
    dd = sub.add_parser(
        "live-audit-dedupe",
        help="Remove money audits superseded by a newer audit of the same blocks (stores written before 2026-11-01)",
    )
    dd.add_argument("--db-dir", type=Path)
    rh = sub.add_parser(
        "live-recount-held",
        help="Recount stored cycles/gross after the held families (arbkit.econ.HELD_TYPES) changed; no re-classification",
    )
    rh.add_argument("--db-dir", type=Path)
    st = sub.add_parser(
        "live-status",
        help="Follower heartbeat, lag, backlog, gaps and money-audit totals of every chain's live store",
    )
    st.add_argument("--db-dir", type=Path)
