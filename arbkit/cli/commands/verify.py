"""Verification commands: hand labels and hand review, audits, recall and bracket screens, explorer copies, builder
and chain facts, registry checks."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from arbkit.chain.settings import Settings
from arbkit.chain.spec import ROOT, load_chain
from arbkit.cli.reports import chain_name, run_scan, save_report
from arbkit.files import read_json, write_json


def cmd_review(args, console):
    """review"""
    name = chain_name(args)
    from arbkit.verify.review import write_review

    spec = load_chain(name)
    report = read_json(args.report)
    out = write_review(spec, report, spec.directory / "validation" / f"review_{args.report.stem}.md")
    console.print(f"Review sheet: {out}", markup=False)
    return 0


def cmd_audit(args, console):
    """audit"""
    name = chain_name(args)
    from arbkit.verify.audit import audit

    spec = load_chain(name)
    report = read_json(args.report or ROOT / "reports" / name / "latest.json")
    result = audit(spec, report, args.data_dir or ROOT / "data" / name)
    out = spec.directory / "validation" / f"audit_{datetime.now(timezone.utc).strftime('%Y%m%d')}.json"
    write_json(out, result)
    if args.json:
        console.print_json(json.dumps(result))
    else:
        console.print(
            f"{spec.name}: {result['transactions_fee_checked']} fees recomputed, {len(result['fee_failures'])} mismatches; "
            f"{result['cycles_checked']} cycle amounts rebuilt from raw receipts, {len(result['gross_mismatches'])} mismatches; "
            f"{len(result['positive_cycles_with_open_losses'])} positives with open losses; "
            f"{len(result['missing_cached_data'])} missing cached items.",
            markup=False,
        )
        for m in result["gross_mismatches"][:10]:
            console.print(
                f"  GROSS {m['tx_hash']}: receipts {m['gross_from_receipts']} vs classifier {m['classifier_gross']}",
                markup=False,
                soft_wrap=True,
            )
        for m in result["fee_failures"][:5]:
            console.print(
                f"  FEE {m['tx_hash']}: expected {m['expected_total']} reported {m['reported']}",
                markup=False,
                soft_wrap=True,
            )
        console.print(f"Saved {out}", style="dim")
    return 1 if (result["fee_failures"] or result["gross_mismatches"]) else 0


def cmd_explorer_check(args, console):
    """explorer-check"""
    name = chain_name(args)
    from arbkit.verify.explorer import run_check, write_markdown

    spec = load_chain(name)
    pages = args.pages or spec.directory / "validation" / "explorer"
    paths = args.report or sorted(
        p for p in (ROOT / "reports" / name).glob("*.json") if p.name not in {"latest.json", "daily.json"}
    )
    merged = {"rows": [], "blocks": []}
    seen_blocks = set()
    for path in sorted(paths, key=lambda p: p.stat().st_mtime):
        rep = read_json(path)
        for b in rep["blocks"]:
            seen_blocks.add(b["number"])
        merged["blocks"] = [{"number": n} for n in seen_blocks]
        newest = {r["tx_hash"]: r for r in merged["rows"]}
        newest.update({r["tx_hash"]: r for r in rep["rows"]})
        merged["rows"] = list(newest.values())
    summary = run_check(spec, merged, pages, args.data_dir or ROOT / "data" / name)
    md = write_markdown(spec, summary)
    write_json(md.with_suffix(".json"), summary)
    console.print(
        f"{spec.name}: {summary['exact']}/{summary['pages']} explorer copies agree exactly, {summary['partial']} agree on everything "
        f"the explorer shows (internal transfers omitted by the explorer), {len(summary['mismatches'])} disagree.",
        markup=False,
    )
    for r in summary["mismatches"][:20]:
        console.print(
            f"  {r['verdict']} {r['tx_hash']} block {r['block']} checks {r['checks']} {'; '.join(r['notes'])}",
            markup=False,
            soft_wrap=True,
        )
    for r in (x for x in summary["results"] if x["verdict"] == "partial"):
        console.print(
            f"  partial {r['tx_hash']}: {'; '.join(r.get('partial', []))}",
            markup=False,
            soft_wrap=True,
            style="dim",
        )
    console.print(f"Sheet: {md}", style="dim")
    return 0 if not summary["mismatches"] else 1


def cmd_explorer_pull(args, console):
    """explorer-pull"""
    name = chain_name(args)
    from arbkit.verify.explorer_pull import partial_hashes, pull, sheet_hashes, text_copy_hashes

    spec = load_chain(name)
    explorer_dir = spec.directory / "validation" / "explorer"
    wanted: list[str] = list(args.hashes or [])
    for sheet in args.sheet or []:
        path = sheet if sheet.exists() else spec.directory / "validation" / sheet.name
        wanted += sheet_hashes(path)
    if args.refetch_partial:
        wanted += partial_hashes(spec.directory / "validation")
    if args.upgrade_text:
        wanted += text_copy_hashes(explorer_dir)
    seen: set[str] = set()
    hashes = [h for h in (w.lower() for w in wanted) if not (h in seen or seen.add(h))]
    if args.skip_existing:
        hashes = [h for h in hashes if not (explorer_dir / f"{h}.json").exists()]
    if args.limit:
        hashes = hashes[: args.limit]
    if not hashes:
        console.print(
            f"{spec.name}: nothing to pull (give hashes, --sheet, --refetch-partial or --upgrade-text).",
            markup=False,
        )
        return 0
    console.print(
        f"{spec.name}: pulling {len(hashes)} explorer copies into {explorer_dir}",
        markup=False,
        soft_wrap=True,
    )
    result = pull(
        spec,
        hashes,
        explorer_dir,
        args.access,
        args.sleep,
        log=lambda t: console.print(t, markup=False, soft_wrap=True, style="dim"),
    )
    console.print(
        f"{spec.name}: {len(result['pulled'])} pulled from {result['source']}, {len(result['failed'])} not fetched"
        f"{': ' + '; '.join(f'{h[:10]}… {r}' for h, r in list(result['failed'].items())[:8]) if result['failed'] else ''}. "
        f"Now run: ./arb -c {spec.name} explorer-check",
        markup=False,
        soft_wrap=True,
    )
    return 0 if not result["failed"] else 1


def cmd_builders(args, console):
    """builders"""
    name = chain_name(args)
    from arbkit.value.builders import measure_builders, render

    spec = load_chain(name)
    report_path = args.report or ROOT / "reports" / name / "latest.json"
    facts = measure_builders(spec, read_json(report_path), args.data_dir or ROOT / "data" / name)
    out = spec.directory / "validation" / f"builders_{datetime.now(timezone.utc).strftime('%Y%m%d')}.json"
    write_json(out, facts)
    if args.json:
        console.print_json(json.dumps(facts))
    else:
        render(console, facts)
        console.print(f"Saved {out}", style="dim")
    return 0


def cmd_handreview(args, console):
    """handreview"""
    name = chain_name(args)
    from arbkit.verify.handreview import default_dirs, dex_map, draw, entry, lock, progress, write_sheet

    spec = load_chain(name)
    reports_dir, data_dir = default_dirs(spec)
    data_dir = args.data_dir or data_dir
    if args.progress:
        console.print_json(json.dumps(progress(spec, reports_dir=reports_dir)))
        return 0
    if args.lock:
        sheet = args.lock if args.lock.exists() else spec.directory / "validation" / args.lock.name
        counts = lock(spec, sheet)
        console.print(
            f"{spec.name}: locked {sheet.name} into handlabels.json: {counts}. Now run: ./arb -c {spec.name} handlabels",
            markup=False,
        )
        return 0
    seed = args.seed if args.seed is not None else int(datetime.now(timezone.utc).strftime("%Y%m%d"))
    rows, info = draw(spec, reports_dir, args.kind, args.count, seed, random_only=args.random_only)
    dex_of = dex_map(spec, data_dir)
    entries = [entry(spec, r, data_dir, info.get(r["block_number"]), dex_of) for r in rows]
    md, js = write_sheet(spec, args.kind, seed, entries)
    console.print(
        f"{spec.name}: drew {len(entries)} fresh {args.kind} rows (seed {seed}{', random blocks only' if args.random_only else ''}); read {md.name}, fill the verdicts in {js.name}, "
        f"then ./arb -c {spec.name} handreview --lock {js.name}",
        markup=False,
        soft_wrap=True,
    )
    return 0


def cmd_brackets(args, console):
    """brackets"""
    name = chain_name(args)
    from arbkit.verify.brackets import bracket_screen, merged_report
    from arbkit.verify.brackets import write_markdown as write_brackets

    spec = load_chain(name)
    report = (
        merged_report(spec, ROOT / "reports" / name)
        if args.all
        else read_json(args.report or ROOT / "reports" / name / "latest.json")
    )
    result = bracket_screen(spec, report, args.data_dir or ROOT / "data" / name)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    out = spec.directory / "validation" / f"brackets_{stamp}.json"
    write_json(out, result)
    md = write_brackets(spec, result, out.with_suffix(".md"))
    if args.json:
        console.print_json(json.dumps(result))
    else:
        console.print(
            f"{spec.name}: {result['screened']:,} successful transactions in {result['blocks']} blocks; {result['brackets']} brackets; "
            f"screen saw {result['sandwiches_seen_by_screen']}/{result['classifier_sandwiches']} classifier sandwiches; "
            f"{len(result['suspects'])} suspects (read them: {md})",
            markup=False,
            soft_wrap=True,
        )
        for kind, n in result["kinds"].items():
            console.print(f"  {kind}: {n}", markup=False)
    return 0


def cmd_recall(args, console):
    """recall"""
    name = chain_name(args)
    from arbkit.verify.recall import loop_screen, write_markdown

    spec = load_chain(name)
    report_path = args.report or ROOT / "reports" / name / "latest.json"
    result = loop_screen(spec, read_json(report_path), args.data_dir or ROOT / "data" / name)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    out = spec.directory / "validation" / f"recall_{stamp}.json"
    write_json(out, result)
    md = write_markdown(spec, result, out.with_suffix(".md"))
    if args.json:
        console.print_json(json.dumps(result))
    else:
        console.print(
            f"{spec.name}: {result['screened']:,} successful transactions screened in {result['blocks']} blocks; "
            f"{result['flagged']} flagged; screen saw {result['positives_flagged_by_screen']}/{result['classifier_positives']} classifier positives; "
            f"{result['suspects_not_counted']} flagged rows not counted (read them: {md})",
            markup=False,
            soft_wrap=True,
        )
        for b in result["buckets"][:8]:
            console.print(
                f"  {b['count']:4d}  {b['arb_class']}{(' / sandwich ' + b['sandwich_role']) if b['sandwich_role'] else ''}  reasons {', '.join(b['reasons'])}",
                markup=False,
            )
    return 0


def cmd_facts(args, console):
    """facts"""
    name = chain_name(args)
    from arbkit.chain.facts import measure, render

    spec = load_chain(name)
    report = read_json(args.report or ROOT / "reports" / name / "latest.json")
    facts = measure(spec, report, args.data_dir or ROOT / "data" / name)
    out = spec.directory / "validation" / f"facts_{datetime.now(timezone.utc).strftime('%Y%m%d')}.json"
    write_json(out, facts)
    if args.json:
        console.print_json(json.dumps(facts))
    else:
        render(console, facts)
        console.print(f"Saved {out}", style="dim")
    return 0


def cmd_coverage(args, console):
    """coverage"""
    name = chain_name(args)
    from arbkit.verify.coverage import compare_reports, decoder_gaps

    report = read_json(args.report or ROOT / "reports" / name / "latest.json")
    data_dir = args.data_dir or ROOT / "data" / name
    result = {"decoder_gaps": decoder_gaps(report, data_dir)}
    if args.against:
        result["recall"] = compare_reports(report, read_json(args.against))
    if args.json:
        console.print_json(json.dumps(result))
        return 0
    gaps = result["decoder_gaps"]
    console.print(
        f"{gaps['traced_rows_with_venues']} traced rows, {gaps['distinct_trace_venues']} distinct trace venues, "
        f"{gaps['venues_without_decoded_swap_event']} venues emitted no decodable swap event.",
        markup=False,
    )
    for g in gaps["undecoded_topics"][:15]:
        console.print(
            f"  {g['topic0']}  {g['logs']:5d} logs  emitters {', '.join(f'{a[:10]}×{n}' for a, n in g['emitters'][:3])}  e.g. {g['sample_txs'][0]}",
            markup=False,
            soft_wrap=True,
        )
    if "recall" in result:
        r = result["recall"]
        console.print(
            f"Recall vs {args.against.name}: {r['common_blocks']} common blocks, {r['transactions_compared']} transactions, "
            f"{r['changed']} changed; positives only in the broad scan: {len(r['positives_only_in_broad'])}; "
            f"positives only in the default scan: {len(r['positives_only_in_default'])} "
            f"(traces {r['default_traces']} vs {r['broad_traces']}).",
            markup=False,
        )
        for h in r["positives_only_in_broad"][:20]:
            d = next(x for x in r["diffs"] if x["tx_hash"] == h)
            console.print(
                f"  MISSED {h} block {d['block_number']} idx {d['tx_index']}: default {d['default']} -> broad {d['broad']}",
                markup=False,
                soft_wrap=True,
            )
    return 0


def cmd_verify_registry(args, console):
    """verify-registry"""
    name = chain_name(args)
    from arbkit.chain.registry import verify_registry

    settings = Settings.load(name, args.data_dir)
    return asyncio.run(verify_registry(settings, console))


def cmd_handlabels(args, console):
    """handlabels"""
    name = chain_name(args)
    settings = Settings.load(name, args.data_dir, args.output_dir)
    from arbkit.verify.handlabels import compare, load_handlabels, write_markdown

    labels = load_handlabels(settings.chain)
    blocks = sorted({int(item["block"]) for item in labels["rows"]})
    report = run_scan(settings, console, blocks, args, extra={"handlabels": True})
    path = save_report(report, settings.reports, stem=f"{name}_handlabels")
    result = compare(settings.chain, labels, report)
    md = write_markdown(settings.chain, result)
    write_json(md.with_suffix(".json"), result)
    for r in result["results"]:
        mark = "OK " if r["ok"] else "MISS"
        console.print(
            f"{mark} {r['tx_hash']} block {r['block']} expected {r.get('expected')} -> "
            f"{r.get('assessment')} / {r.get('arb_class')} ({r.get('mev_type')}) gross {r.get('gross')} {r.get('profit_symbol') or ''} "
            f"labelled {r.get('labelled_proceeds') or ''} position {r.get('trigger_type')}",
            markup=False,
            soft_wrap=True,
        )
    console.print(
        f"{result['ok']}/{result['total']} as expected; {result['proceeds_exact']} proceeds exact. Sheet: {md}  Report: {path}",
        markup=False,
        soft_wrap=True,
    )
    return 0 if result["ok"] == result["total"] else 1
