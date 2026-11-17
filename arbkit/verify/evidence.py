"""Proof material for the explorer pages: everything behind a block or a transaction label, taken from the cached raw
data the classifier itself read (block bodies and receipts under data/<chain>/blocks/, call traces under
data/<chain>/traces/), with the sha256 of those files so a reader can re-verify, plus the explorer copy and its
reconciliation verdict when one was saved under chains/<chain>/validation/explorer/.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from arbkit.chain.spec import ChainSpec
from arbkit.files import read_json
from arbkit.value.fees import units
from arbkit.verify.audit import DEPOSIT, TRANSFER, WITHDRAWAL, ZERO, _addr, native_calls
from mevlab.decode.signatures import SINGLETON_SWAP_TOPICS
from mevlab.hexutil import to_int
from mevlab.trace.movements import claim_movements
from mevlab.version import CLASSIFIER_VERSION


def _sha256(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def _symbols(spec: ChainSpec, rows: list[dict]) -> tuple[dict[str, str], dict[str, int]]:
    symbols = {t.address: t.symbol for t in spec.tokens}
    decimals = {t.address: t.decimals for t in spec.tokens}
    for r in rows:
        tok = (r.get("profit_token") or "").lower()
        if tok and r.get("profit_decimals") is not None:
            decimals[tok] = r["profit_decimals"]
            if r.get("profit_symbol"):
                symbols[tok] = r["profit_symbol"]
        sw = r.get("sandwich") or {}
        tok = (sw.get("profit_token") or "").lower()
        if tok and sw.get("profit_decimals") is not None:
            decimals[tok] = sw["profit_decimals"]
            if sw.get("profit_symbol"):
                symbols[tok] = sw["profit_symbol"]
    return symbols, decimals


def token_transfers(
    receipt: dict, weth: str, symbols: dict[str, str], decimals: dict[str, int]
) -> list[dict]:
    out = []
    for lg in receipt.get("logs") or []:
        topics = lg.get("topics") or []
        data = str(lg.get("data") or "0x")
        words = [data[i : i + 64] for i in range(2, len(data), 64)]
        token = str(lg["address"]).lower()
        entry = None
        if topics and str(topics[0]).lower() == TRANSFER and len(topics) == 3 and len(words) == 1:
            entry = {
                "token": token,
                "from": _addr(topics[1]),
                "to": _addr(topics[2]),
                "raw": str(int(words[0], 16)),
                "kind": "transfer",
            }
        elif token == weth and len(topics) == 2 and words and str(topics[0]).lower() == DEPOSIT:
            entry = {
                "token": token,
                "from": ZERO,
                "to": _addr(topics[1]),
                "raw": str(int(words[0], 16)),
                "kind": "wrap",
            }
        elif token == weth and len(topics) == 2 and words and str(topics[0]).lower() == WITHDRAWAL:
            entry = {
                "token": token,
                "from": _addr(topics[1]),
                "to": ZERO,
                "raw": str(int(words[0], 16)),
                "kind": "unwrap",
            }
        if entry is None:
            continue
        dec = decimals.get(token)
        entry["symbol"] = symbols.get(token)
        entry["amount"] = units(int(entry["raw"]), dec) if dec is not None else None
        entry["log_index"] = to_int(lg.get("logIndex", "0x0"))
        out.append(entry)
    return out


def singleton_swaps(receipt: dict, symbols: dict[str, str], decimals: dict[str, int]) -> list[dict]:
    """Uniswap V4 / PancakeSwap Infinity Swap events: pool id, locker and the signed amounts (the swapper's
    deltas: positive = received from the pool, negative = paid). The only record of a route that never leaves a
    singleton."""
    out = []
    for lg in receipt.get("logs") or []:
        topics = lg.get("topics") or []
        if len(topics) < 3 or str(topics[0]).lower() not in SINGLETON_SWAP_TOPICS:
            continue
        data = str(lg.get("data") or "0x")[2:]
        words = [int(data[i : i + 64], 16) for i in range(0, min(len(data), 128), 64)]
        signed = [w - 2**256 if w >= 2**255 else w for w in words]
        out.append(
            {
                "manager": str(lg["address"]).lower(),
                "pool_id": str(topics[1]).lower(),
                "locker": "0x" + str(topics[2])[-40:].lower(),
                "amount0": str(signed[0]) if signed else None,
                "amount1": str(signed[1]) if len(signed) > 1 else None,
                "log_index": to_int(lg.get("logIndex", "0x0")),
            }
        )
    return out


def claims(receipt: dict, symbols: dict[str, str], decimals: dict[str, int]) -> list[dict]:
    """ERC-6909 claim mints (the singleton paying a claimant) and burns (the claimant paying it)."""
    out = []
    for m in claim_movements(receipt.get("logs") or []):
        token = m.token.lower()
        dec = decimals.get(token)
        out.append(
            {
                "kind": m.kind,
                "manager": m.sender if m.kind == "claim_mint" else m.receiver,
                "claimant": m.receiver if m.kind == "claim_mint" else m.sender,
                "token": token,
                "symbol": symbols.get(token),
                "raw": str(m.amount),
                "amount": units(m.amount, dec) if dec is not None else None,
            }
        )
    return out


def explorer_copy(spec: ChainSpec, tx_hash: str) -> dict | None:
    directory = spec.directory / "validation" / "explorer"
    copy = next(
        (p for p in (directory / f"{tx_hash}.json", directory / f"{tx_hash}.txt") if p.exists()), None
    )
    if copy is None:
        return None
    out = {"file": copy.name, "sha256": _sha256(copy), "verdict": None, "notes": [], "checked_at": None}
    checks = sorted((spec.directory / "validation").glob("explorer_check_*.json"))
    if checks:
        try:
            data = json.loads(checks[-1].read_text())
        except ValueError:
            data = {}
        hit = next((r for r in data.get("results", []) if str(r.get("tx_hash", "")).lower() == tx_hash), None)
        if hit:
            out.update(
                verdict=hit.get("verdict"),
                notes=hit.get("notes") or [],
                checked_at=data.get("checked_at"),
                checks=hit.get("checks"),
            )
    return out


def transaction_evidence(spec: ChainSpec, data_dir: Path, row: dict, block_hash: str | None) -> dict:
    """Raw material behind one classified row: receipt transfers, native calls from the trace, file hashes."""
    number = row["block_number"]
    tx_hash = row["tx_hash"].lower()
    block_path = data_dir / "blocks" / f"{number}.json.gz"
    out: dict = {
        "block_file": str(block_path.relative_to(data_dir.parent.parent)) if block_path.exists() else None,
        "block_file_sha256": _sha256(block_path),
        "trace_file": None,
        "trace_file_sha256": None,
        "token_transfers": [],
        "native_calls": [],
        "singleton_swaps": [],
        "claims": [],
        "logs": None,
        "status": None,
        "gas_used": None,
        "explorer_copy": explorer_copy(spec, tx_hash),
        "classifier_version": CLASSIFIER_VERSION,
    }
    if not block_path.exists():
        return out
    bundle = read_json(block_path)
    block = bundle["block"]
    receipt = next((r for r in bundle["receipts"] if str(r["transactionHash"]).lower() == tx_hash), None)
    tx = next((t for t in block["transactions"] if str(t["hash"]).lower() == tx_hash), None)
    symbols, decimals = _symbols(spec, [row])
    if receipt:
        out.update(
            status=to_int(receipt.get("status", "0x0")),
            gas_used=to_int(receipt.get("gasUsed", "0x0")),
            logs=len(receipt.get("logs") or []),
            token_transfers=token_transfers(receipt, spec.weth, symbols, decimals),
            singleton_swaps=singleton_swaps(receipt, symbols, decimals),
            claims=claims(receipt, symbols, decimals),
        )
    if tx:
        out["value_wei"] = str(to_int(tx.get("value", "0x0")))
        out["nonce"] = to_int(tx.get("nonce", "0x0")) if tx.get("nonce") is not None else None
        out["input_selector"] = (tx.get("input") or "0x")[:10]
        out["type"] = to_int(tx.get("type", "0x0"))
    trace_path = data_dir / "traces" / f"{block_hash or block['hash']}_{tx_hash}.json.gz"
    if trace_path.exists():
        out["trace_file"] = str(trace_path.relative_to(data_dir.parent.parent))
        out["trace_file_sha256"] = _sha256(trace_path)
        trace = read_json(trace_path)
        out["native_calls"] = [
            {"from": frm, "to": to, "value_wei": str(value), "value": units(value)}
            for _, frm, to, value in native_calls(trace)
        ]
        out["trace_root"] = {
            "from": str(trace.get("from", "")).lower(),
            "to": str(trace.get("to", "")).lower(),
            "type": trace.get("type"),
            "calls": _count_calls(trace),
            "error": trace.get("error"),
        }
    out["block"] = {
        "number": number,
        "hash": block["hash"],
        "parent_hash": block.get("parentHash"),
        "timestamp": to_int(block["timestamp"]),
        "miner": str(block.get("miner", "")).lower(),
        "base_fee_per_gas": to_int(block["baseFeePerGas"]) if block.get("baseFeePerGas") else None,
        "gas_used": to_int(block.get("gasUsed", "0x0")),
        "transactions": len(block["transactions"]),
    }
    return out


def _count_calls(frame: dict) -> int:
    return 1 + sum(_count_calls(c) for c in frame.get("calls") or [])


def block_proof(spec: ChainSpec, data_dir: Path, number: int, info: dict, rows: list[dict]) -> dict:
    """Self-contained proof of one block's classification: header, file hashes, every labelled row with its evidence."""
    block_path = data_dir / "blocks" / f"{number}.json.gz"
    header = None
    if block_path.exists():
        block = read_json(block_path)["block"]
        header = {
            "number": number,
            "hash": block["hash"],
            "parent_hash": block.get("parentHash"),
            "timestamp": to_int(block["timestamp"]),
            "miner": str(block.get("miner", "")).lower(),
            "extra_data": block.get("extraData"),
            "base_fee_per_gas": to_int(block["baseFeePerGas"]) if block.get("baseFeePerGas") else None,
            "gas_used": to_int(block.get("gasUsed", "0x0")),
            "gas_limit": to_int(block.get("gasLimit", "0x0")),
            "transactions": len(block["transactions"]),
        }
    labelled = [r for r in rows if r.get("mev_type") not in (None, "none") or r["assessment"] != "not_arb"]
    proof_rows = []
    for r in sorted(labelled, key=lambda x: x["tx_index"]):
        tv = r.get("trace") or {}
        ev = transaction_evidence(spec, data_dir, r, header["hash"] if header else None)
        proof_rows.append(
            {
                "tx_index": r["tx_index"],
                "tx_hash": r["tx_hash"],
                "from": r.get("tx_from"),
                "to": r.get("tx_to"),
                "assessment": r["assessment"],
                "arb_class": r["arb_class"],
                "mev_type": r.get("mev_type"),
                "position": {
                    "trigger_type": r.get("trigger_type"),
                    "trigger_tx_hash": r.get("trigger_tx_hash"),
                    "trigger_distance_blocks": r.get("trigger_distance_blocks"),
                    "slot_index_est": r.get("slot_index_est"),
                },
                "result": {
                    "profit_token": r.get("profit_token"),
                    "profit_symbol": r.get("profit_symbol"),
                    "gross_profit": r.get("gross_profit"),
                    "gross_profit_raw": r.get("gross_profit_raw"),
                    "gross_usd": r.get("gross_usd"),
                    "net_usd": r.get("net_usd"),
                    "price_source": r.get("price_source"),
                    "fee_wei": r["fees"].get("total_fee_wei"),
                    "builder_tip_wei": r.get("builder_tip_wei"),
                    "kickback_wei": r.get("kickback_wei"),
                },
                "verdict": {
                    "trace": tv.get("verdict", tv.get("status")),
                    "group": tv.get("group"),
                    "venues": tv.get("venues"),
                    "cycle_path": tv.get("cycle_path"),
                    "payouts_by_token": tv.get("payouts_by_token"),
                    "notes": tv.get("notes"),
                    "reasons": r.get("reasons"),
                    "quote_venues": r.get("quote_venues"),
                },
                "sandwich": r.get("sandwich"),
                "liquidations": r.get("liquidations"),
                "snipe": r.get("snipe"),
                "jit": r.get("jit"),
                "cex_dex": r.get("cex_dex"),
                "evidence": {
                    k: ev.get(k)
                    for k in (
                        "status",
                        "gas_used",
                        "logs",
                        "token_transfers",
                        "native_calls",
                        "trace_file",
                        "trace_file_sha256",
                        "explorer_copy",
                    )
                },
                "explorer_url": r.get("explorer_url"),
            }
        )
    return {
        "chain": spec.name,
        "chain_id": spec.chain_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "classifier_version": CLASSIFIER_VERSION,
        "block": header or {"number": number, "timestamp": info.get("timestamp"), "miner": info.get("miner")},
        "builder_tag": info.get("builder_tag"),
        "block_file_sha256": _sha256(block_path),
        "rows_total": len(rows),
        "rows_labelled": len(proof_rows),
        "rows": proof_rows,
        "method": "docs/METHOD.md: closed chain in one asset through called venues, profit at the callee or its collector; amounts from settled receipts and call traces; USD from Binance 1-minute opens (final when the minute starts)",
        "how_to_verify": [
            f"./arb -c {spec.name} scan {number}   # re-downloads the block and reclassifies; compare the rows",
            f"./arb -c {spec.name} audit REPORT     # recomputes fees and cycle amounts from the raw files",
            "open explorer_url on each row and compare the token transfers and internal transactions listed here",
        ],
    }
