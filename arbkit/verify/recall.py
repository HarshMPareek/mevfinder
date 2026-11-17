"""Decoder-free loop screen over every successful transaction of a report's cached blocks: an independent check
for cycles the classifier did not count.

The screen knows no DEX. From the settled ERC-20 Transfer logs (plus WETH deposits, and native value calls when a
trace is cached) it takes the party {tx.from, tx.to} together with the terminal receivers of its own transfers
(collectors, builders), and flags a transaction when some token both left and came back to the party with a
positive net while at least one counterparty exchanged one asset for another ("exchange node"). Every flagged
transaction the classifier did not count as a positive, losing or review row is a suspect, listed with the
classifier's class and reasons so a person can read it; the screen's own sensitivity against the classifier's
positives is reported next to it (singleton flash-accounting routes, native-only legs and operator groups that
differ from {from, to} are where the screen is blind, so it is a floor, not a recall figure).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from arbkit.chain.spec import ChainSpec
from arbkit.files import read_json
from arbkit.verify.audit import DEPOSIT, TRANSFER, ZERO, _addr, native_calls, with_natives
from mevlab.hexutil import to_int

COUNTED = {"trace_supported", "trace_supported_loss", "needs_review"}


def _screen(moves, party):
    out_by: dict[str, int] = defaultdict(int)
    in_by: dict[str, int] = defaultdict(int)
    recv: dict[str, set[str]] = defaultdict(set)
    sent: dict[str, set[str]] = defaultdict(set)
    for token, frm, to, amt in moves:
        if frm in party and to not in party:
            out_by[token] += amt
            recv[to].add(token)
        elif to in party and frm not in party:
            in_by[token] += amt
            sent[frm].add(token)
    nets = {t: in_by[t] - out_by[t] for t in set(in_by) | set(out_by)}
    round_trip = [t for t in nets if out_by[t] > 0 and in_by[t] > 0 and nets[t] > 0]
    if not round_trip:
        return None
    nodes = [c for c in set(recv) & set(sent) if c != ZERO and (recv[c] - sent[c] or sent[c] - recv[c])]
    return round_trip, {t: v for t, v in nets.items() if v}, nodes


def loop_screen(spec: ChainSpec, report: dict, data_dir: Path) -> dict:
    weth = spec.weth
    rows = {r["tx_hash"].lower(): r for r in report["rows"]}
    screened = 0
    flagged: list[dict] = []
    for info in report["blocks"]:
        path = data_dir / "blocks" / f"{info['number']}.json.gz"
        if not path.exists():
            continue
        bundle = read_json(path)
        block = bundle["block"]
        receipts = {r["transactionHash"].lower(): r for r in bundle["receipts"]}
        for tx in block["transactions"]:
            tx_hash = tx["hash"].lower()
            receipt = receipts.get(tx_hash)
            if (
                not receipt
                or to_int(receipt.get("status", "0x0")) != 1
                or to_int(tx.get("type", "0x0")) in spec.system_tx_types
            ):
                continue
            screened += 1
            cash = []
            for lg in receipt.get("logs", []):
                topics = lg.get("topics", [])
                data = lg.get("data", "0x")
                words = [data[i : i + 64] for i in range(2, len(data), 64)]
                token = lg["address"].lower()
                if topics and topics[0] == TRANSFER and len(topics) == 3 and len(words) == 1:
                    cash.append((token, _addr(topics[1]), _addr(topics[2]), int(words[0], 16)))
                elif token == weth and len(topics) == 2 and words and topics[0] == DEPOSIT:
                    cash.append((weth, ZERO, _addr(topics[1]), int(words[0], 16)))
            trace_path = data_dir / "traces" / f"{block['hash']}_{tx_hash}.json.gz"
            traced = trace_path.exists()
            if traced:
                cash = [
                    (weth if t in {"ETH", ZERO} else t, f, to, v)
                    for t, f, to, v in with_natives(cash, native_calls(read_json(trace_path)), weth)
                ]
            frm, to = tx["from"].lower(), (tx.get("to") or "").lower()
            party = {frm} | ({to} if to else set())
            senders = {f for _, f, _, _ in cash}
            terminal = {t for _, f, t, _ in cash if f in party and t not in senders and t != ZERO}
            res = _screen(cash, party | terminal)
            if not res:
                continue
            round_trip, nets, nodes = res
            if not nodes:
                continue
            row = rows.get(tx_hash) or {}
            flagged.append(
                {
                    "block": info["number"],
                    "tx_index": to_int(tx["transactionIndex"]),
                    "tx_hash": tx_hash,
                    "round_trip_tokens": round_trip,
                    "nets": {k: str(v) for k, v in nets.items()},
                    "exchange_nodes": len(nodes),
                    "traced": traced,
                    "assessment": row.get("assessment"),
                    "arb_class": row.get("arb_class"),
                    "mev_type": row.get("mev_type"),
                    "reasons": sorted({str(x).split(":")[0] for x in row.get("reasons") or []}),
                    "trace_verdict": (row.get("trace") or {}).get("verdict"),
                    "sandwich_role": (row.get("sandwich") or {}).get("role"),
                    "explorer_url": spec.explorer_url(tx_hash),
                }
            )
    positives = {h for h, r in rows.items() if r["assessment"] in {"trace_supported", "trace_supported_loss"}}
    hits = {f["tx_hash"] for f in flagged}
    suspects = [f for f in flagged if f["assessment"] not in COUNTED]
    by_bucket = Counter((f["arb_class"], f["sandwich_role"], tuple(f["reasons"][:4])) for f in suspects)
    return {
        "chain": spec.name,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "report": report.get("created_at"),
        "blocks": len(report["blocks"]),
        "screened": screened,
        "flagged": len(flagged),
        "classifier_positives": len(positives),
        "positives_flagged_by_screen": len(positives & hits),
        "suspects_not_counted": len(suspects),
        "suspects": suspects,
        "buckets": [
            {"arb_class": k[0], "sandwich_role": k[1], "reasons": list(k[2]), "count": n}
            for k, n in by_bucket.most_common()
        ],
        "method": (
            "receipt Transfer logs (+ WETH deposits, native calls when a trace is cached); party = {from, to} + terminal receivers; "
            "flag = a token out and back with positive net and >= 1 exchange node. Suspects are flagged rows the classifier "
            "did not count as positive, losing or review; read them, the screen proves nothing by itself."
        ),
    }


def write_markdown(spec: ChainSpec, result: dict, path: Path) -> Path:
    lines = [
        f"# {spec.name}: decoder-free loop screen",
        "",
        f"Checked {result['checked_at']} over {result['blocks']} blocks, {result['screened']:,} successful user transactions. "
        f"The screen flagged {result['flagged']}; it saw {result['positives_flagged_by_screen']} of the classifier's {result['classifier_positives']} "
        f"positives (its blind spots: singleton flash accounting, native-only legs, operator groups beyond {{from, to}}). "
        f"**{result['suspects_not_counted']} flagged transactions are not counted by the classifier** and are listed below with its reasons.",
        "",
        "| Class | Sandwich | Reasons | Count |",
        "|---|---|---|---:|",
    ]
    for b in result["buckets"]:
        lines.append(
            f"| {b['arb_class']} | {b['sandwich_role'] or ''} | {', '.join(b['reasons'])} | {b['count']} |"
        )
    lines += ["", "## Suspects", ""]
    for s in result["suspects"]:
        nets = ", ".join(f"{k[:10]}… {v}" for k, v in list(s["nets"].items())[:4])
        lines.append(
            f"- block {s['block']} #{s['tx_index']} [{s['tx_hash'][:14]}…]({s['explorer_url']}) — {s['arb_class']} / {s['mev_type']}; "
            f"trace {s['trace_verdict']}; nodes {s['exchange_nodes']}; nets {nets}; reasons {', '.join(s['reasons'])}"
        )
    lines += ["", result["method"], ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines))
    return path
