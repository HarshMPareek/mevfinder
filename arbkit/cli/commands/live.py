"""Live-store commands: follow a chain, import or re-classify stored blocks, store maintenance and status."""

from __future__ import annotations

import json

from arbkit.chain.spec import load_chain
from arbkit.cli.reports import chain_name


def cmd_live_status(args, console):
    """live-status"""
    from arbkit.live.status import render_status

    return render_status(console, args.db_dir)


def cmd_follow(args, console):
    """follow"""
    name = chain_name(args)
    from arbkit.live.follower import follow

    return follow(name, args.db_dir, args.once, args.batch)


def cmd_live_reclassify(args, console):
    """live-reclassify"""
    name = chain_name(args)
    from arbkit.live.follower import reclassify

    numbers = [int(n) for n in json.loads(args.blocks_file.read_text())] if args.blocks_file else None
    return reclassify(name, args.db_dir, args.first, args.last, args.all_sources, numbers, args.lead_in)


def cmd_live_audit_dedupe(args, console):
    """live-audit-dedupe"""
    name = chain_name(args)
    from arbkit.live import store as livedb
    from arbkit.live.follower import db_path

    removed = livedb.dedupe_audits(livedb.connect(db_path(name, args.db_dir)))
    console.print(f"{name}: removed {removed} superseded audits", markup=False)
    return 0


def cmd_live_recount_held(args, console):
    """live-recount-held"""
    name = chain_name(args)
    from arbkit.live import store as livedb
    from arbkit.live.follower import db_path

    out = livedb.recount_held(
        livedb.connect(db_path(name, args.db_dir)),
        load_chain(name),
        log=lambda m: console.print(m, markup=False),
    )
    console.print(f"{name}: {out}", markup=False)
    return 0


def cmd_live_import(args, console):
    """live-import"""
    name = chain_name(args)
    from arbkit.live.status import import_reports

    return import_reports(console, name, args.db_dir, args.reports_dir)
