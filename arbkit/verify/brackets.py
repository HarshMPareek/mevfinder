"""Direction-agnostic bracket screen over cached blocks: an independent check for sandwiches the classifier did
not count.

The screen knows no swap event. A venue is any contract that emitted a decoded swap event, or that emitted any
other event while exchanging one token for another in the transaction. A bracket is one wallet trading the same
venue twice with another party's trade on that venue in between (up to MAX_GAP positions, any direction). Every
bracket is classified against the report: counted sandwich, JIT pair, counted cycle as a leg, position change,
round trip without gain, no measurable party flows, or an uncounted round trip with a gain. The last two are the
suspects a person reads; the screen proves nothing by itself and is blind to operators whose inventory sits
outside {wallet, callee} when the legs' transfers do not reveal the holder.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from arbkit.chain.spec import ChainSpec
from arbkit.files import read_json
from arbkit.scan.sandwich import DIRECTIONAL_TOPICS, ROUND_TRIP_MAX_SHARE, V4_SWAP_TOPIC
from arbkit.value.daily import load_reports
from mevlab.decode.signatures import TRANSFER_TOPIC, WETH_DEPOSIT_TOPIC, WETH_WITHDRAWAL_TOPIC
from mevlab.hexutil import addr, to_int

MAX_GAP = 40
APPROVAL_TOPIC = "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925"
IGNORED_TOPICS = {
    TRANSFER_TOPIC.lower(),
    APPROVAL_TOPIC,
    WETH_DEPOSIT_TOPIC.lower(),
    WETH_WITHDRAWAL_TOPIC.lower(),
}
SUSPECT_KINDS = ("uncounted round trip with gain", "no party flows")


def _transfers(logs):
    for log in logs:
        topics = log.get("topics") or []
        if topics and topics[0].lower() == TRANSFER_TOPIC.lower() and len(topics) == 3:
            data = log.get("data", "0x")
            yield (
                addr(log["address"]),
                addr("0x" + topics[1][-40:]),
                addr("0x" + topics[2][-40:]),
                (int(data, 16) if data not in ("0x", "") else 0),
            )


def venues_of(logs: list[dict]) -> set[str]:
    """Decoded swap venues (V4 keyed by pool id) plus undecoded contracts that exchanged one token for another."""
    venues: set[str] = set()
    decoded: set[str] = set()
    emitters: set[str] = set()
    sent: dict[str, set[str]] = defaultdict(set)
    received: dict[str, set[str]] = defaultdict(set)
    for log in logs:
        topics = log.get("topics") or []
        if not topics:
            continue
        topic, address = topics[0].lower(), addr(log["address"])
        if topic in DIRECTIONAL_TOPICS:
            decoded.add(address)
            venues.add(
                address + (":" + topics[1].lower() if topic == V4_SWAP_TOPIC and len(topics) > 1 else "")
            )
        elif topic not in IGNORED_TOPICS:
            emitters.add(address)
    for token, sender, receiver, _ in _transfers(logs):
        sent[sender].add(token)
        received[receiver].add(token)
    for address in emitters - decoded:
        if sent.get(address) and received.get(address) and sent[address] != received[address]:
            venues.add(address)
    return venues


def merged_report(spec: ChainSpec, reports_dir: Path) -> dict:
    """Every scanned block of the chain with its newest classification: the input for a chain-wide screen."""
    newest: dict[int, tuple[str, dict]] = {}
    rows_by_block: dict[int, list[dict]] = {}
    for report in sorted(load_reports(reports_dir), key=lambda r: r.get("created_at", "")):
        grouped: dict[int, list[dict]] = defaultdict(list)
        for row in report["rows"]:
            grouped[row["block_number"]].append(row)
        for info in report["blocks"]:
            newest[info["number"]] = (report.get("created_at", ""), info)
            rows_by_block[info["number"]] = grouped.get(info["number"], [])
    return {
        "created_at": max((c for c, _ in newest.values()), default=None),
        "merged_reports": True,
        "blocks": [entry[1] for _, entry in sorted(newest.items())],
        "rows": [r for n in sorted(rows_by_block) for r in rows_by_block[n]],
    }


def bracket_screen(spec: ChainSpec, report: dict, data_dir: Path) -> dict:
    tokens = spec.token_by_address
    rows = {r["tx_hash"].lower(): r for r in report["rows"]}
    counted = {h for h, r in rows.items() if (r.get("sandwich") or {}).get("role") == "back"}
    screened = 0
    brackets: list[dict] = []
    for info in report["blocks"]:
        path = data_dir / "blocks" / f"{info['number']}.json.gz"
        if not path.exists():
            continue
        receipts = read_json(path).get("receipts") or []
        txs = []
        for receipt in sorted(receipts, key=lambda r: to_int(r["transactionIndex"])):
            if to_int(receipt.get("status", "0x1")) != 1 or not receipt.get("to"):
                continue
            screened += 1
            logs = receipt.get("logs", [])
            venues = venues_of(logs)
            if venues:
                txs.append(
                    {
                        "index": to_int(receipt["transactionIndex"]),
                        "hash": receipt["transactionHash"].lower(),
                        "from": addr(receipt["from"]),
                        "to": addr(receipt["to"]),
                        "venues": venues,
                        "logs": logs,
                    }
                )
        by_wallet: dict[str, list[dict]] = defaultdict(list)
        for tx in txs:
            by_wallet[tx["from"]].append(tx)
        for wallet, legs in by_wallet.items():
            for front, back in zip(legs, legs[1:]):
                shared = front["venues"] & back["venues"]
                if not shared or back["index"] - front["index"] > MAX_GAP:
                    continue
                victims = [
                    v
                    for v in txs
                    if front["index"] < v["index"] < back["index"]
                    and v["from"] != wallet
                    and v["to"] != front["to"]
                    and v["venues"] & shared
                ]
                if not victims:
                    continue
                brackets.append(
                    _classify(
                        spec, tokens, rows, counted, info["number"], wallet, front, back, victims, shared
                    )
                )
    counts = Counter(b["kind"] for b in brackets)
    suspects = [b for b in brackets if b["kind"] in SUSPECT_KINDS]
    return {
        "chain": spec.name,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "report": report.get("created_at"),
        "blocks": len(report["blocks"]),
        "screened": screened,
        "brackets": len(brackets),
        "classifier_sandwiches": len(counted),
        "sandwiches_seen_by_screen": counts.get("counted sandwich", 0),
        "kinds": dict(counts.most_common()),
        "suspects": suspects,
        "all": [
            {k: v for k, v in b.items() if k != "victims"} | {"victims": len(b["victims"])} for b in brackets
        ],
        "method": (
            f"venues = decoded swap events (V4 by pool id) + undecoded contracts that exchanged one token for another; bracket = one wallet "
            f"trading a shared venue twice with another party's trade on it in between (gap <= {MAX_GAP}); party = wallet + callees; "
            f"round trip = no token's |net| above {ROUND_TRIP_MAX_SHARE} of its volume. Suspects: uncounted round trips with a gain and "
            "brackets without measurable party flows. Read them; the screen proves nothing by itself."
        ),
    }


def _classify(spec, tokens, rows, counted, number, wallet, front, back, victims, shared) -> dict:
    party = {wallet, front["to"], back["to"]}
    net: dict[str, int] = defaultdict(int)
    moved: dict[str, int] = defaultdict(int)
    for leg in (front, back):
        for token, sender, receiver, value in _transfers(leg["logs"]):
            if receiver in party and sender not in party:
                net[token] += value
                moved[token] += value
            if sender in party and receiver not in party:
                net[token] -= value
                moved[token] += value
    nets = {t: v for t, v in net.items() if v}
    share = max((abs(v) / moved[t] for t, v in nets.items() if moved[t]), default=0.0)
    front_row, back_row = rows.get(front["hash"], {}), rows.get(back["hash"], {})
    classes = {front_row.get("arb_class"), back_row.get("arb_class")}
    mev = {front_row.get("mev_type"), back_row.get("mev_type")}
    if back["hash"] in counted:
        kind = "counted sandwich"
    elif back_row.get("sandwich_rejected"):
        kind = "rejected by classifier"
    elif "jit_liquidity" in mev or "lp_action" in classes:
        kind = "jit pair"
    elif classes & {"atomic_cycle", "loss_cycle", "embedded_cycle"}:
        kind = "cycle leg"
    elif not nets:
        kind = "no party flows"
    elif share > ROUND_TRIP_MAX_SHARE:
        kind = "position change"
    elif not any(v > 0 for v in nets.values()):
        kind = "round trip without gain"
    else:
        kind = "uncounted round trip with gain"

    def units(token, value):
        info = tokens.get(token)
        decimals = info.decimals if info else 18
        symbol = info.symbol if info else ("W" + spec.native_symbol if token == spec.weth else token[:10])
        return symbol, value / 10**decimals

    return {
        "block": number,
        "kind": kind,
        "wallet": wallet,
        "front": front["hash"],
        "back": back["hash"],
        "front_index": front["index"],
        "back_index": back["index"],
        "callees": sorted({front["to"], back["to"]}),
        "victims": [v["hash"] for v in victims],
        "venue": sorted(shared)[0],
        "share": round(share, 3),
        "nets": {
            token: {"symbol": units(token, v)[0], "amount": units(token, v)[1], "raw": str(v)}
            for token, v in nets.items()
        },
        "classes": [front_row.get("arb_class"), back_row.get("arb_class")],
        "mev_types": [front_row.get("mev_type"), back_row.get("mev_type")],
        "explorer_url": spec.explorer_url(back["hash"]),
    }


def write_markdown(spec: ChainSpec, result: dict, path: Path) -> Path:
    lines = [
        f"# {spec.name}: bracket screen (sandwich recall)",
        "",
        f"Checked {result['checked_at']} over {result['blocks']} blocks, {result['screened']:,} successful transactions: "
        f"{result['brackets']} brackets; the screen saw {result['sandwiches_seen_by_screen']} of the classifier's "
        f"{result['classifier_sandwiches']} sandwiches. **{len(result['suspects'])} suspects** (uncounted round trips with a gain, "
        "or brackets without measurable party flows) are listed below.",
        "",
        "| Kind | Count |",
        "|---|---:|",
    ]
    for kind, n in result["kinds"].items():
        lines.append(f"| {kind} | {n} |")
    lines += ["", "## Suspects", ""]
    for s in result["suspects"]:
        nets = ", ".join(f"{v['symbol']} {v['amount']:+.6g}" for v in s["nets"].values()) or "no party flows"
        lines.append(
            f"- block {s['block']} #{s['front_index']}->#{s['back_index']} wallet {s['wallet'][:12]}… via {', '.join(c[:12] + '…' for c in s['callees'])} "
            f"on {s['venue'][:14]}…, {len(s['victims'])} other-party trade(s) between — [{s['kind']}] {nets}; "
            f"classes {s['classes']}; [{s['back'][:14]}…]({s['explorer_url']})"
        )
    lines += ["", result["method"], ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines))
    return path
