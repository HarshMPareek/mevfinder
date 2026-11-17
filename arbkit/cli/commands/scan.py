"""Scanning commands: scan / sample / tx, day, blind, show, reprice, daily, chains."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from arbkit.chain.rpc import SafeRpc
from arbkit.chain.settings import Settings
from arbkit.chain.spec import ROOT, list_chains, load_chain
from arbkit.cli.parser import parse_blocks
from arbkit.cli.reports import chain_name, days_cache, run_scan, save_report
from arbkit.cli.terminal import show_report
from arbkit.files import read_json, write_json
from arbkit.value.daily import aggregate_daily, block_range_for_day, load_reports, render_daily, sample_blocks


def execute_day(args, console, settings):
    spec = settings.chain

    async def resolve():
        async with SafeRpc(settings.http_url) as rpc:
            if await rpc.chain_id() != spec.chain_id:
                raise ValueError(f"Configured RPC is not {spec.name} (chain ID {spec.chain_id}).")
            return await block_range_for_day(rpc, spec, args.date, settings.data / "days.json")

    first, last = asyncio.run(resolve())
    total = last - first + 1
    console.print(f"{spec.name} {args.date}: blocks {first}-{last} ({total:,} blocks).", markup=False)
    if args.full:
        done = {
            b["number"]
            for r in load_reports(settings.reports)
            if r.get("day") == args.date
            for b in r["blocks"]
        }
        pending = [n for n in range(first, last + 1) if n not in done]
        console.print(
            f"{len(done):,} blocks already saved for this day; {len(pending):,} to scan in chunks of {args.chunk}.",
            markup=False,
        )
        code = 0
        for start in range(0, len(pending), args.chunk):
            chunk = pending[start : start + args.chunk]
            extra = {"day": args.date, "day_blocks": [first, last], "day_mode": "full"}
            report = run_scan(settings, console, chunk, args, extra=extra)
            path = save_report(
                report, settings.reports, stem=f"{spec.name}_day{args.date}_{chunk[0]}-{chunk[-1]}"
            )
            console.print(f"Saved {path}", markup=False, soft_wrap=True)
            code = code or (0 if report["complete"] else 1)
        result = aggregate_daily(spec, load_reports(settings.reports), days_cache(settings))
        write_json(settings.reports / "daily.json", result)
        render_daily(console, result)
        return code
    n = args.sample or 200
    blocks = sample_blocks(first, last, n)
    extra = {"day": args.date, "day_blocks": [first, last], "day_mode": f"sample:{n}"}
    report = run_scan(settings, console, blocks, args, extra=extra)
    path = save_report(report, settings.reports, stem=f"{spec.name}_day{args.date}_sample{n}")
    show_report(report, console, args.show_all, args.limit, None, args.classes)
    console.print(
        f"\nSaved JSON: {path}\nSaved CSV:  {path.with_suffix('.csv')}", markup=False, soft_wrap=True
    )
    result = aggregate_daily(spec, load_reports(settings.reports), days_cache(settings))
    write_json(settings.reports / "daily.json", result)
    render_daily(console, result)
    return 0 if report["complete"] else 1


def cmd_chains(args, console):
    """chains"""
    for name in list_chains():
        spec = load_chain(name)
        console.print(f"{name:10s} {spec.describe()}  explorer {spec.explorer_tx}", markup=False)
    return 0


def cmd_blind(args, console):
    """blind"""
    name = chain_name(args)
    import random

    from arbkit.verify.review import write_review

    settings = Settings.load(name)
    spec = settings.chain

    async def head():
        async with SafeRpc(settings.http_url) as rpc:
            return await rpc.block_number()

    top = asyncio.run(head())
    span = args.span or spec.blocks_per_day
    rng = random.Random(args.seed)
    blocks = sorted(rng.sample(range(max(1, top - span), top - 5), args.count))
    console.print(
        f"{spec.name}: head {top}, {args.count} blind blocks from the last {span:,} (seed {args.seed}): {blocks[:6]}…",
        markup=False,
    )
    report = run_scan(settings, console, blocks, args, extra={"blind_seed": args.seed, "blind_span": span})
    path = save_report(report, settings.reports, stem=f"{spec.name}_blind{args.seed}")
    out = write_review(
        spec,
        report,
        spec.directory
        / "validation"
        / f"blind_{args.seed}_{datetime.now(timezone.utc).strftime('%Y%m%d')}.md",
    )
    console.print(f"Saved {path}\nReview sheet: {out}", markup=False)
    return 0


def cmd_reprice(args, console):
    """reprice"""
    name = chain_name(args)
    from arbkit.scan.scanner import summarize
    from arbkit.value.pricing import PriceBook, value_rows

    spec = load_chain(name)
    report = read_json(args.report)
    timestamps = {b["number"]: b["timestamp"] for b in report["blocks"]}
    asyncio.run(value_rows(spec, report["rows"], timestamps, PriceBook(ROOT / "data" / "prices")))
    report["summary"] = summarize(report["rows"])
    report["repriced_at"] = datetime.now(timezone.utc).isoformat()
    write_json(args.report, report)
    unpriced = sum(
        1 for r in report["rows"] if r["assessment"] == "trace_supported" and r.get("gross_usd") is None
    )
    console.print(
        f"{spec.name}: repriced {len(report['rows'])} rows; gross USD {report['summary'].get('gross_usd_trace_supported')}, "
        f"net USD {report['summary'].get('net_usd_trace_supported')}, unpriced cycles {unpriced}.",
        markup=False,
    )
    return 0


def cmd_show(args, console):
    """show"""
    name = chain_name(args)
    report_path = args.report or ROOT / "reports" / name / "latest.json"
    report = read_json(report_path)
    show_report(report, console, args.show_all, args.limit, args.tx, args.classes)
    return 0


def cmd_daily(args, console):
    """daily"""
    name = chain_name(args)
    spec = load_chain(name)
    reports_dir = args.output_dir or ROOT / "reports" / name
    result = aggregate_daily(
        spec,
        load_reports(reports_dir),
        days_cache(Settings.load(spec)) if (ROOT / ".env").exists() else {},
    )
    write_json(reports_dir / "daily.json", result)
    if args.json:
        console.print_json(json.dumps(result))
    else:
        render_daily(console, result)
    return 0


def cmd_day(args, console):
    """day"""
    name = chain_name(args)
    settings = Settings.load(name, args.data_dir, args.output_dir)
    return execute_day(args, console, settings)


def cmd_scan(args, console):
    """scan / sample / tx"""
    name = chain_name(args)
    settings = Settings.load(name, args.data_dir, args.output_dir)
    blocks = (
        ["sample"]
        if args.command == "sample"
        else parse_blocks(args.blocks)
        if args.command == "scan"
        else []
    )
    focus = args.hash if args.command == "tx" else None
    report = run_scan(settings, console, blocks, args, focus)
    path = save_report(report, settings.reports)
    show_report(report, console, args.show_all, args.limit, focus, args.classes)
    console.print(
        f"\nSaved JSON: {path}\nSaved CSV:  {path.with_suffix('.csv')}", markup=False, soft_wrap=True
    )
    console.print(
        f"Inspect later: ./arb -c {name} show --tx 0x… | Re-run: ./arb -c {name} scan START END", style="dim"
    )
    return 0 if report["complete"] else 1
