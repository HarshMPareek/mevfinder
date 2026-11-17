"""`arb` / `python -m arbkit`: parse the arguments and run the command (or prompt for one)."""

from __future__ import annotations

import json
import os
import re
import sys

from rich.console import Console

from arbkit.chain.settings import DEFAULT_CHAIN
from arbkit.cli.commands.live import (
    cmd_follow,
    cmd_live_audit_dedupe,
    cmd_live_import,
    cmd_live_reclassify,
    cmd_live_recount_held,
    cmd_live_status,
)
from arbkit.cli.commands.scan import (
    cmd_blind,
    cmd_chains,
    cmd_daily,
    cmd_day,
    cmd_reprice,
    cmd_scan,
    cmd_show,
)
from arbkit.cli.commands.site import cmd_ui
from arbkit.cli.commands.verify import (
    cmd_audit,
    cmd_brackets,
    cmd_builders,
    cmd_coverage,
    cmd_explorer_check,
    cmd_explorer_pull,
    cmd_facts,
    cmd_handlabels,
    cmd_handreview,
    cmd_recall,
    cmd_review,
    cmd_verify_registry,
)
from arbkit.cli.parser import parser
from mevlab.rpc import RpcError


def interactive(console, chain=None):
    name = chain or os.environ.get("ARB_CHAIN") or DEFAULT_CHAIN
    console.print(
        f"Arbitrage classifier [{name}] — enter blocks, an inclusive range, or 'sample'. Type q to quit.",
        style="bold cyan",
    )
    while True:
        try:
            start = input("Block(s), START-END, or sample: ").strip()
        except EOFError:
            return 0
        if start.lower() in {"q", "quit", "exit"}:
            return 0
        if not start:
            continue
        try:
            base_args = ["--chain", name]
            if start == "sample":
                execute(parser().parse_args([*base_args, "sample"]), console)
            else:
                if re.fullmatch(r"\d+", start):
                    end = input("End block (Enter for a single block): ").strip()
                    if end:
                        start += "-" + end
                execute(parser().parse_args([*base_args, "scan", start]), console)
        except (ValueError, RpcError, OSError) as exc:
            console.print(f"Error: {exc}", style="red", markup=False)


COMMANDS = {
    "ui": cmd_ui,
    "live-status": cmd_live_status,
    "chains": cmd_chains,
    "follow": cmd_follow,
    "live-reclassify": cmd_live_reclassify,
    "live-audit-dedupe": cmd_live_audit_dedupe,
    "live-recount-held": cmd_live_recount_held,
    "live-import": cmd_live_import,
    "review": cmd_review,
    "blind": cmd_blind,
    "reprice": cmd_reprice,
    "show": cmd_show,
    "audit": cmd_audit,
    "explorer-check": cmd_explorer_check,
    "explorer-pull": cmd_explorer_pull,
    "builders": cmd_builders,
    "handreview": cmd_handreview,
    "brackets": cmd_brackets,
    "recall": cmd_recall,
    "facts": cmd_facts,
    "coverage": cmd_coverage,
    "daily": cmd_daily,
    "verify-registry": cmd_verify_registry,
    "day": cmd_day,
    "handlabels": cmd_handlabels,
    "scan": cmd_scan,
    "sample": cmd_scan,
    "tx": cmd_scan,
}


def execute(args, console):
    return COMMANDS[args.command](args, console)


def main(argv=None):
    console = Console(highlight=False)
    args = parser().parse_args(argv)
    try:
        code = interactive(console, args.chain) if args.command is None else execute(args, console)
    except KeyboardInterrupt:
        console.print("\nInterrupted. Completed raw block downloads remain cached.")
        code = 130
    except (RpcError, ValueError, OSError, KeyError, json.JSONDecodeError) as exc:
        console.print(f"Error: {exc}", style="red", markup=False)
        code = 1
    sys.exit(code)
