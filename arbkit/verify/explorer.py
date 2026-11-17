"""Reconcile classified rows with what a block explorer shows for the same transaction.

Explorer copies are saved verbatim under chains/<chain>/validation/explorer/:
  <hash>.json  a Blockscout API v2 pull, {"compact": {"s", "b", "f", "from", "to", "t": [[from, to, token, symbol,
               decimals, value, type], ...], "i": [[from, to, value], ...], "more"}} (the full {"tx",
               "token_transfers", "internal"} shape is accepted too); raw integer amounts
  <hash>.txt   the text of an explorer transaction page as the browser renders it: the Etherscan family
               (Etherscan, Arbiscan, BaseScan, BscScan, OP Etherscan) or BSCTrace (NodeReal). Amounts are
               decimal strings, addresses are abbreviated (0xAbCd...1234) or replaced by labels

For every row the check records: status and block agree; the explorer's token transfer list against the raw
receipt decoded locally (every entry matched on amount, token and endpoints); the fee to the wei (or to the
digits the page shows); the internal native transfers the explorer lists against the cached trace; and the
operator group's result rebuilt from the explorer's movements with the audit's cash rules (payouts the report
names added back) against the classifier's gross. The explorer and the node read the same receipts, so
agreement proves our decoding and arithmetic, not independent ownership.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from arbkit.chain.spec import ChainSpec
from arbkit.files import read_json
from arbkit.verify.audit import CLAIM, DEPOSIT, TRANSFER, WITHDRAWAL, _addr, native_calls, nets, with_natives

ZERO = "0x" + "0" * 40
WEI = Decimal(10) ** 18
CANDIDATE_DECIMALS = (18, 6, 8, 9, 2, 4, 12)


# --------------------------------------------------------------------------------------------- address matching


def _abbrev_match(shown: str, full: str) -> bool:
    """Explorer pages abbreviate addresses (0xAbCd...1234, 0xabcdef...) or label them; labels never match."""
    s, full = shown.strip().lower(), full.lower()
    if s.startswith("null"):
        return full == ZERO
    m = re.match(r"(0x[0-9a-f]+)(?:\.\.\.|…)([0-9a-f]*)$", s)
    if m:
        return full.startswith(m.group(1)) and full.endswith(m.group(2))
    if re.fullmatch(r"0x[0-9a-f]{40}", s):
        return s == full
    return False


def _is_abbrev(shown: str) -> bool:
    s = shown.strip().lower()
    return (
        s.startswith("null")
        or bool(re.match(r"0x[0-9a-f]+(\.\.\.|…)", s))
        or bool(re.fullmatch(r"0x[0-9a-f]{40}", s))
    )


def _endpoint_ok(shown: str, full: str) -> bool:
    """A shown endpoint is consistent with an address when it abbreviates it or is a label (unverifiable)."""
    return _abbrev_match(shown, full) if _is_abbrev(shown) else True


# --------------------------------------------------------------------------------------------- page parsers


def parse_blockscout(data: dict) -> dict:
    """Blockscout API v2 data, either the compact pull shape or the full objects."""
    out = {
        "source": "blockscout",
        "transfers": [],
        "internal": [],
        "truncated": False,
        "has_internal_section": True,
    }
    if "compact" in data:
        c = data["compact"]
        out.update(
            {
                "status": c.get("s"),
                "block": c.get("b"),
                "from": (c.get("from") or "").lower() or None,
                "to": (c.get("to") or "").lower() or None,
                "fee_wei": int(c["f"]) if c.get("f") is not None else None,
                "truncated": bool(c.get("more")),
            }
        )
        for frm, to, token, symbol, decimals, value, kind in c.get("t") or []:
            if value is None:
                continue
            out["transfers"].append(
                {
                    "from": (frm or ZERO).lower(),
                    "to": (to or ZERO).lower(),
                    "token": (token or "").lower(),
                    "symbol": symbol,
                    "decimals": int(decimals) if decimals is not None else None,
                    "value": int(value),
                    "type": kind,
                }
            )
        for frm, to, value in c.get("i") or []:
            out["internal"].append(
                {"from": (frm or ZERO).lower(), "to": (to or ZERO).lower(), "value": int(value)}
            )
        return out
    tx = data.get("tx") or {}
    out.update(
        {
            "status": tx.get("status"),
            "block": tx.get("block_number") or tx.get("block"),
            "from": ((tx.get("from") or {}).get("hash") or "").lower() or None,
            "to": ((tx.get("to") or {}).get("hash") or "").lower() or None,
            "fee_wei": int((tx.get("fee") or {}).get("value") or 0) if tx.get("fee") else None,
        }
    )
    for t in data.get("token_transfers") or []:
        total, token = t.get("total") or {}, t.get("token") or {}
        if total.get("value") is None:
            continue
        out["transfers"].append(
            {
                "from": ((t.get("from") or {}).get("hash") or ZERO).lower(),
                "to": ((t.get("to") or {}).get("hash") or ZERO).lower(),
                "token": (token.get("address_hash") or token.get("address") or "").lower(),
                "symbol": token.get("symbol"),
                "decimals": int(total.get("decimals") or token.get("decimals") or 18),
                "value": int(total["value"]),
                "type": t.get("type"),
            }
        )
    for it in data.get("internal") or []:
        value = int(it.get("value") or 0)
        if value and not it.get("error"):
            out["internal"].append(
                {
                    "from": ((it.get("from") or {}).get("hash") or ZERO).lower(),
                    "to": ((it.get("to") or {}).get("hash") or ZERO).lower(),
                    "value": value,
                }
            )
    return out


def _first_after(lines: list[str], label: str, start: int = 0, exact: bool = False) -> tuple[int, str | None]:
    for i in range(start, len(lines)):
        ln = lines[i]
        if (ln == label) if exact else ln.lower().startswith(label.lower()):
            for j in range(i + 1, min(i + 4, len(lines))):
                if lines[j]:
                    return i, lines[j]
            return i, None
    return -1, None


def _amount_text(text: str) -> tuple[str, bool]:
    """'0.0260 ETH' / '754.294718702 Gwei BNB' / '1,234.5' -> decimal string in whole native units, truncated flag."""
    truncated = text.endswith("…")
    word = text.replace(",", "").rstrip("…").split()
    amount = word[0] if word else "0"
    if len(word) > 1 and word[1].lower() == "gwei":
        amount = format(Decimal(amount) / Decimal(10) ** 9, "f")
    return amount, truncated


def parse_etherscan_text(text: str) -> dict:
    """Status, block, from, to, fee, the ERC-20 list and the internal transfers of an Etherscan-family page."""
    lines = [ln.strip() for ln in text.splitlines()]
    if any(ln == "Txn Hash" for ln in lines) and any(ln == "Txn Fee" for ln in lines):
        return parse_bsctrace_text(text)
    out: dict = {
        "source": "etherscan",
        "transfers": [],
        "internal": [],
        "truncated": False,
        "has_internal_section": False,
    }
    _, out["status"] = _first_after(lines, "Status:")
    _, block = _first_after(lines, "Block:")
    out["block"] = int(re.match(r"\d+", block).group(0)) if block and re.match(r"\d+", block) else None
    _, frm = _first_after(lines, "From:")
    out["from"] = frm.split()[0].lower() if frm else None
    _, to = _first_after(lines, "Interacted With (To):")
    if to is None:
        _, to = _first_after(lines, "To:")
    out["to"] = to.split()[0].lower() if to else None
    _, fee = _first_after(lines, "Transaction Fee:")
    out["fee_native"] = _amount_text(fee)[0] if fee else None
    # ERC-20 list: repeated blocks "From / X / To / Y / For / amount / ($usd) / Name (SYMBOL)"
    start = next((i for i, ln in enumerate(lines) if ln.startswith("ERC-20 Tokens Transferred")), None)
    if start is not None:
        i = start + 1
        while i < len(lines):
            ln = lines[i]
            if ln.startswith("Value:") or ln.startswith("Transaction Fee"):
                break
            if ln == "From" and i + 5 < len(lines) and lines[i + 2] == "To" and lines[i + 4] == "For":
                amount, truncated = _amount_text(lines[i + 5])
                symbol = None
                for cand in lines[i + 6 : i + 9]:
                    if cand == "From":
                        break
                    if cand in {"To", "Value:"} or cand.startswith("$"):
                        continue
                    m = re.search(r"\(([^()]+)\)\s*$", cand)
                    symbol = m.group(1) if m else cand
                    break
                out["transfers"].append(
                    {
                        "from": lines[i + 1],
                        "to": lines[i + 3],
                        "amount": amount,
                        "truncated": truncated,
                        "symbol": symbol,
                    }
                )
                i += 6
                continue
            i += 1
    # internal native transfers ("Transfer / 0.0026 ETH / ($usd) / From / A / To / B")
    istart = next((i for i, ln in enumerate(lines) if ln.startswith("Internal Transactions:")), None)
    if istart is not None:
        out["has_internal_section"] = True
        i = istart + 1
        while (
            i < len(lines)
            and not lines[i].startswith("ERC-20 Tokens Transferred")
            and not lines[i].startswith("Value:")
        ):
            if lines[i] == "Transfer" and i + 1 < len(lines):
                amount, truncated = _amount_text(lines[i + 1])
                j = i + 2
                while j < len(lines) and (
                    lines[j].startswith("(") or lines[j].startswith("$") or not lines[j]
                ):
                    j += 1
                if j + 3 < len(lines) and lines[j] == "From" and lines[j + 2] == "To":
                    out["internal"].append(
                        {"from": lines[j + 1], "to": lines[j + 3], "amount": amount, "truncated": truncated}
                    )
                    i = j + 4
                    continue
            i += 1
    return out


def parse_bsctrace_text(text: str) -> dict:
    """BSCTrace (NodeReal) transaction page: 'Txn Hash', 'Status', 'Block', internal 'Transfer' blocks,
    'Tokens Transferred' (symbol on its own line, no USD line) and 'Txn Fee'."""
    lines = [ln.strip() for ln in text.splitlines()]
    out: dict = {
        "source": "bsctrace",
        "transfers": [],
        "internal": [],
        "truncated": False,
        "has_internal_section": True,
    }
    top = next((i for i, ln in enumerate(lines) if ln == "Txn Hash"), 0)
    lines = lines[top:]
    _, out["status"] = _first_after(lines, "Status", exact=True)
    _, block = _first_after(lines, "Block", exact=True)
    out["block"] = int(re.match(r"\d+", block).group(0)) if block and re.match(r"\d+", block) else None
    status_at = next((i for i, ln in enumerate(lines) if ln == "Status"), 0)
    from_at = next(
        (
            i
            for i in range(status_at, len(lines))
            if lines[i] == "From" and i + 1 < len(lines) and re.fullmatch(r"0x[0-9a-fA-F]{40}", lines[i + 1])
        ),
        None,
    )
    out["from"] = lines[from_at + 1].lower() if from_at is not None else None
    out["to"] = None
    if from_at is not None and from_at + 3 < len(lines):
        if lines[from_at + 2].startswith("Interacted With"):
            cand = lines[from_at + 4] if lines[from_at + 3] == "Contract" else lines[from_at + 3]
        elif lines[from_at + 2] == "To":
            cand = lines[from_at + 3]
        else:
            cand = ""
        out["to"] = cand.lower() if re.fullmatch(r"0x[0-9a-fA-F]{40}", cand) else None
    _, fee = _first_after(lines, "Txn Fee", exact=True)
    out["fee_native"] = _amount_text(fee)[0] if fee else None
    tokens_at = next((i for i, ln in enumerate(lines) if ln == "Tokens Transferred"), None)
    value_at = next((i for i, ln in enumerate(lines) if ln == "Value"), len(lines))
    body_start = (from_at + 2) if from_at is not None else 0
    i = body_start
    end_internal = tokens_at if tokens_at is not None else value_at
    while i < end_internal:
        if lines[i] == "Transfer" and i + 5 < len(lines) and lines[i + 2] == "From" and lines[i + 4] == "To":
            amount, truncated = _amount_text(lines[i + 1])
            out["internal"].append(
                {"from": lines[i + 3], "to": lines[i + 5], "amount": amount, "truncated": truncated}
            )
            i += 6
            continue
        i += 1
    if tokens_at is not None:
        i = tokens_at + 1
        while i < value_at:
            if lines[i] == "From" and i + 6 < len(lines) and lines[i + 2] == "To" and lines[i + 4] == "For":
                amount, truncated = _amount_text(lines[i + 5])
                out["transfers"].append(
                    {
                        "from": lines[i + 1],
                        "to": lines[i + 3],
                        "amount": amount,
                        "truncated": truncated,
                        "symbol": lines[i + 6],
                    }
                )
                i += 7
                continue
            i += 1
    return out


def load_page(path: Path) -> dict:
    if path.suffix == ".json":
        return parse_blockscout(json.loads(path.read_text()))
    return parse_etherscan_text(path.read_text())


# --------------------------------------------------------------------------------------------- local side


def local_transfers(receipt: dict, weth: str) -> tuple[list[tuple], list[tuple]]:
    """(token, from, to, value, kind) from Transfer logs, with canonical-WETH Deposit/Withdrawal as mint/burn legs
    of kind "wrap"; plus ERC-6909 claim movements (token, from, to, value)."""
    cash, claims = [], []
    for log in receipt.get("logs", []):
        topics = log.get("topics") or []
        data = str(log.get("data") or "0x")
        words = [data[i : i + 64] for i in range(2, len(data), 64)]
        token = log["address"].lower()
        if topics and topics[0].lower() == TRANSFER and len(topics) == 3 and len(words) == 1:
            cash.append((token, _addr(topics[1]), _addr(topics[2]), int(words[0], 16), "transfer"))
        elif token == weth and len(topics) == 2 and words and topics[0].lower() == DEPOSIT:
            cash.append((weth, ZERO, _addr(topics[1]), int(words[0], 16), "wrap"))
        elif token == weth and len(topics) == 2 and words and topics[0].lower() == WITHDRAWAL:
            cash.append((weth, _addr(topics[1]), ZERO, int(words[0], 16), "wrap"))
        elif topics and topics[0].lower() == CLAIM and len(topics) == 4 and len(words) >= 2:
            claims.append((_addr(topics[3]), _addr(topics[1]), _addr(topics[2]), int(words[1], 16)))
    return cash, claims


def trace_natives(trace: dict | None) -> tuple[list[tuple], list[tuple], list[tuple]]:
    """(all value calls, the internal ones explorers list, delegatecall frames carrying value).

    Explorers show the transaction's own value under "Value", not as an internal transfer, so the root frame is
    split off; Blockscout also lists DELEGATECALL frames with the parent's value, which move nothing."""
    if trace is None:
        return [], [], []
    calls = native_calls(trace)
    root_value = (
        int(trace.get("value") or "0x0", 16)
        if isinstance(trace.get("value"), str)
        else int(trace.get("value") or 0)
    )
    internal = list(calls)
    if root_value and calls and calls[0][1] == trace["from"].lower() and calls[0][3] == root_value:
        internal = calls[1:]
    echoes = []

    def walk(frame):
        if frame.get("error") or frame.get("revertReason"):
            return
        value = frame.get("value")
        value = int(value, 16) if isinstance(value, str) else int(value or 0)
        if frame.get("type") in {"DELEGATECALL", "CALLCODE"} and value:
            echoes.append(("ETH", frame["from"].lower(), (frame.get("to") or ZERO).lower(), value))
        for child in frame.get("calls", []):
            walk(child)

    walk(trace)
    return calls, internal, echoes


def reverted_natives(trace: dict | None) -> list[tuple]:
    """Value calls inside reverted frames (the frame itself or an ancestor errored). They moved nothing, but some
    explorers list them (BSCTrace's API lists a failed Uniswap V4 attempt next to the Infinity leg that succeeded)."""
    out: list[tuple] = []
    if trace is None:
        return out

    def walk(frame, failed):
        failed = failed or bool(frame.get("error") or frame.get("revertReason"))
        value = frame.get("value")
        value = int(value, 16) if isinstance(value, str) else int(value or 0)
        if failed and value and frame.get("type") in {"CALL", "CREATE", "CREATE2", "SELFDESTRUCT"}:
            out.append(("ETH", frame["from"].lower(), (frame.get("to") or ZERO).lower(), value))
        for child in frame.get("calls", []):
            walk(child, failed)

    walk(trace, False)
    return out


def _norm_symbol(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower().replace("₮", "t"))


def _formatted(value: int, decimals: int) -> str:
    return format(Decimal(value) / Decimal(10) ** decimals, "f")


def _amount_equal(shown: str, truncated: bool, value: int, decimals: int) -> bool:
    text = _formatted(value, decimals)
    if truncated:
        return text.startswith(shown)
    try:
        return Decimal(shown) == Decimal(text)
    except Exception:
        return False


# --------------------------------------------------------------------------------------------- reconciliation


def reconcile(
    spec: ChainSpec,
    row: dict,
    receipt: dict,
    trace: dict | None,
    page: dict,
    decimals: dict[str, int],
    symbols: dict[str, str],
) -> dict:
    """Compare one classified row with the explorer's view of its transaction."""
    weth = spec.weth
    result = {
        "tx_hash": row["tx_hash"],
        "block": row["block_number"],
        "source": page["source"],
        "checks": {},
        "notes": [],
        "assessment": row.get("assessment"),
        "mev_type": row.get("mev_type"),
    }
    checks = result["checks"]
    checks["status_success"] = str(page.get("status") or "").lower() in {"success", "ok"}
    checks["block_agrees"] = page.get("block") == row["block_number"]
    checks["to_agrees"] = (page.get("to") == str(row.get("tx_to") or "").lower()) if page.get("to") else None
    if page.get("from"):
        checks["from_agrees"] = page["from"] == str(row.get("tx_from") or "").lower()
    fee = int(row["fees"]["total_fee_wei"])
    if page.get("fee_wei") is not None:
        checks["fee_exact"] = page["fee_wei"] == fee
        checks["fee_shown_wei"] = str(page["fee_wei"])
    elif page.get("fee_native") is not None:
        shown = Decimal(page["fee_native"])
        exact = shown == Decimal(fee) / WEI
        checks["fee_exact"] = exact
        checks["fee_shown"] = page["fee_native"]
        if not exact:
            digits = len(page["fee_native"].split(".")[1]) if "." in page["fee_native"] else 0
            rounded = (Decimal(fee) / WEI).quantize(Decimal(10) ** -digits)
            if rounded == shown:
                checks["fee_exact"] = True
                result["notes"].append(f"fee shown rounded to {digits} decimals")
    else:
        checks["fee_exact"] = None
    if page.get("truncated"):
        result["notes"].append("explorer transfer list has more pages than were saved")

    cash, claims = local_transfers(receipt, weth)
    transfers_only = [m for m in cash if m[4] == "transfer"]
    all_natives, internal_natives, echoes = trace_natives(trace)
    group = {a.lower() for a in row["trace"].get("group", [])}
    token = (row.get("profit_token") or "").lower()
    partial: list[str] = result.setdefault("partial", [])
    page_cap = 20 if page["source"] == "bsctrace" else None
    moves: list[tuple] = []  # explorer-confirmed movements in raw units: (token|"ETH", from, to, value)
    native_moves: list[tuple] = []

    if page["source"] == "blockscout":
        theirs = sorted((t["token"], t["from"], t["to"], t["value"]) for t in page["transfers"])
        convention = None
        for name, mine in (("with_weth_wraps", cash), ("transfers_only", transfers_only)):
            if sorted((m[0], m[1], m[2], m[3]) for m in mine) == theirs:
                convention = name
                break
        checks["erc20_list_convention"] = convention
        checks["erc20_count_agrees"] = len(theirs) in {len(cash), len(transfers_only)}
        checks["erc20_amounts_agree"] = convention is not None
        if convention is None:
            mine_set = {(m[0], m[1], m[2], m[3]) for m in cash}
            result["notes"].append(
                f"erc20 list differs: explorer-only {len(set(theirs) - mine_set)}, local-only {len(mine_set - set(theirs))}"
            )
        moves = [(t, f, to, v) for t, f, to, v in theirs]
        if convention == "transfers_only":
            moves += [(m[0], m[1], m[2], m[3]) for m in cash if m[4] == "wrap"]
        listed = [("ETH", it["from"], it["to"], it["value"]) for it in page["internal"]]
        echo_set = list(echoes)
        kept = []
        for entry in listed:
            if entry in echo_set:
                echo_set.remove(entry)
            else:
                kept.append(entry)
        if len(kept) < len(listed):
            result["notes"].append(
                f"{len(listed) - len(kept)} delegatecall echo(es) in the explorer's internal list ignored"
            )
        failed_pool = reverted_natives(trace)
        live = []
        for entry in kept:
            if entry in failed_pool:
                failed_pool.remove(entry)
            else:
                live.append(entry)
        if len(live) < len(kept):
            result["notes"].append(
                f"{len(kept) - len(live)} internal transfer(s) inside reverted frames listed by the explorer ignored"
            )
        kept = live
        native_moves = kept
        if trace is not None:
            theirs_native = sorted((f, t, v) for _, f, t, v in kept)
            mine_native = sorted((f, t, v) for _, f, t, v in internal_natives)
            if theirs_native == mine_native:
                checks["internal_agrees"] = True
            else:
                remaining = list(mine_native)
                extra = []
                for e in theirs_native:
                    if e in remaining:
                        remaining.remove(e)
                    else:
                        extra.append(e)
                if extra:
                    checks["internal_agrees"] = False
                    result["notes"].append(
                        f"explorer lists {len(extra)} internal transfer(s) absent from the trace"
                    )
                else:
                    checks["internal_agrees"] = None
                    partial.append(
                        "explorer has not indexed internal transfers"
                        if not theirs_native
                        else f"explorer lists {len(theirs_native)} of {len(mine_native)} internal transfers"
                    )
                    native_moves = list(internal_natives)
        else:
            checks["internal_agrees"] = None
    else:
        # page amounts are decimal strings and endpoints abbreviated: align every shown transfer with a local leg
        aligned: list[tuple] = []
        unmatched: list[dict] = []
        capped = (
            page_cap is not None and len(page["transfers"]) == page_cap and len(transfers_only) > page_cap
        )
        conventions = (
            [("transfers_only", transfers_only[:page_cap])]
            if capped
            else [("with_weth_wraps", cash), ("transfers_only", transfers_only)]
        )
        for conv_name, mine in conventions:
            pool = list(mine)
            aligned, unmatched = [], []
            for t in page["transfers"]:
                candidates = []
                for m in pool:
                    tok, frm, to, value, _ = m
                    decs = [decimals[tok]] if tok in decimals else list(CANDIDATE_DECIMALS)
                    if not any(_amount_equal(t["amount"], t.get("truncated", False), value, d) for d in decs):
                        continue
                    if not (_endpoint_ok(t["from"], frm) and _endpoint_ok(t["to"], to)):
                        continue
                    candidates.append(m)
                if not candidates:
                    unmatched.append(t)
                    continue
                candidates.sort(
                    key=lambda m: _norm_symbol(symbols.get(m[0])) != _norm_symbol(t.get("symbol"))
                )
                pool.remove(candidates[0])
                aligned.append(candidates[0])
            if not unmatched and not pool:
                checks["erc20_list_convention"] = conv_name
                break
        checks["erc20_count_agrees"] = capped or len(page["transfers"]) in {len(cash), len(transfers_only)}
        checks["erc20_amounts_agree"] = (
            not unmatched
            and len(aligned) == len(page["transfers"])
            and (capped or len(page["transfers"]) in {len(cash), len(transfers_only)})
        )
        if capped:
            partial.append(f"page shows only the first {page_cap} of {len(transfers_only)} token transfers")
        if unmatched:
            # share-based tokens (e.g. Binance tokenized stocks on BSC) emit Transfer in shares and a second event
            # carrying (shares, amount); explorers display the amount. Recognise that from the receipt itself.
            secondary = []
            for log in receipt.get("logs", []):
                topics = log.get("topics") or []
                data = str(log.get("data") or "0x")
                words = [int(data[i : i + 64], 16) for i in range(2, len(data), 64)]
                if len(topics) == 3 and topics[0].lower() != TRANSFER and len(words) >= 2:
                    secondary.append((log["address"].lower(), _addr(topics[1]), _addr(topics[2]), words[1]))
            still = []
            for t in unmatched:
                hit = next(
                    (
                        sec
                        for sec in secondary
                        if _endpoint_ok(t["from"], sec[1])
                        and _endpoint_ok(t["to"], sec[2])
                        and any(
                            _amount_equal(t["amount"], t.get("truncated", False), sec[3], d)
                            for d in CANDIDATE_DECIMALS
                        )
                    ),
                    None,
                )
                if hit is None:
                    still.append(t)
                else:
                    secondary.remove(hit)
                    partial.append(
                        f"{t.get('symbol')} is a share-based token: explorer shows the amount from its secondary event, the Transfer log carries shares"
                    )
            if not still and len(aligned) + len(unmatched) == len(page["transfers"]):
                checks["erc20_amounts_agree"] = True
                checks["erc20_share_tokens"] = len(unmatched)
            unmatched = still
        if unmatched:
            result["notes"].append(
                "unmatched explorer transfers: "
                + "; ".join(f"{u['amount']} {u.get('symbol')} {u['from']}->{u['to']}" for u in unmatched[:4])
            )
        confirmed = set(aligned)
        share_ok = checks.get("erc20_share_tokens", 0) > 0 and checks["erc20_amounts_agree"]
        moves = [
            (m[0], m[1], m[2], m[3]) for m in cash if m in confirmed or m[4] == "wrap" or capped or share_ok
        ]
        # internal native transfers: align with the trace's value calls when it is cached
        if page["internal"]:
            if trace is not None:
                pool = list(internal_natives)
                echo_pool = list(echoes) + reverted_natives(trace)
                extra = []
                for it in page["internal"]:
                    hit = None
                    for n in pool + echo_pool:
                        _, frm, to, value = n
                        shown = Decimal(it["amount"])
                        actual = Decimal(value) / WEI
                        close = abs(shown - actual) <= Decimal(10) ** -9 or (
                            it.get("truncated") and format(actual, "f").startswith(it["amount"])
                        )
                        if close and _endpoint_ok(it["from"], frm) and _endpoint_ok(it["to"], to):
                            hit = n
                            break
                    if hit is None:
                        extra.append(it)
                    elif hit in pool:
                        pool.remove(hit)
                        native_moves.append(hit)
                    else:
                        echo_pool.remove(hit)
                native_capped = (
                    page_cap is not None
                    and len(page["internal"]) == page_cap
                    and len(internal_natives) > page_cap
                )
                if extra:
                    checks["internal_agrees"] = False
                    result["notes"].append(
                        f"{len(extra)} internal transfer(s) on the page not found in the trace"
                    )
                elif pool and native_capped:
                    checks["internal_agrees"] = None
                    partial.append(
                        f"page shows only the first {page_cap} of {len(internal_natives)} internal transfers"
                    )
                    native_moves = list(internal_natives)
                elif pool:
                    checks["internal_agrees"] = None
                    partial.append(
                        f"page lists {len(page['internal'])} of {len(internal_natives)} internal transfers"
                    )
                    native_moves = list(internal_natives)
                else:
                    checks["internal_agrees"] = True
            else:
                checks["internal_agrees"] = None
                for it in page["internal"]:
                    frm = next((g for g in group if _abbrev_match(it["from"], g)), it["from"].lower())
                    to = next((g for g in group if _abbrev_match(it["to"], g)), it["to"].lower())
                    native_moves.append(("ETH", frm, to, int(Decimal(it["amount"]) * WEI)))
                result["notes"].append("no cached trace: native amounts taken from the page as shown")
        elif internal_natives:
            checks["internal_agrees"] = False if page.get("has_internal_section") else None
            (result["notes"] if page.get("has_internal_section") else partial).append(
                "page lists no internal transfers; trace natives used for the result"
            )
            native_moves = list(internal_natives)
        else:
            checks["internal_agrees"] = True if trace is not None else None
    if (
        trace is not None
        and all_natives
        and all_natives[0] not in native_moves
        and len(all_natives) > len(internal_natives)
    ):
        native_moves = [all_natives[0]] + list(
            native_moves
        )  # the transaction's own value, shown as "Value" by explorers

    # the operator's result from the explorer-confirmed movements, with the audit's cash rules
    if token and row.get("gross_source") == "trace" and row.get("gross_profit_raw") is not None:
        all_moves = with_natives(moves, native_moves, weth)
        net = nets(all_moves, group, weth)
        claim_net = nets(claims, group, weth)
        payments: dict[str, int] = defaultdict(int)
        for ptoken, by_recipient in (row["trace"].get("payouts_by_token") or {}).items():
            for recipient in by_recipient:
                payments[ptoken] += sum(
                    value * ((frm in group and to == recipient) - (to in group and frm == recipient))
                    for tok, frm, to, value in all_moves
                    if (weth if tok in {"ETH", ZERO} else tok) == ptoken
                )
        explorer_gross = net.get(token, 0) + payments.get(token, 0) + claim_net.get(token, 0)
        if claim_net.get(token):
            result["notes"].append("ERC-6909 claims (not shown by explorers) taken from the receipt")
        checks["explorer_gross_raw"] = str(explorer_gross)
        checks["classifier_gross_raw"] = str(row["gross_profit_raw"])
        checks["gross_exact"] = explorer_gross == int(row["gross_profit_raw"])
    else:
        checks["gross_exact"] = None
        if token and row.get("gross_source") != "trace":
            result["notes"].append(f"gross source {row.get('gross_source')}: not rebuilt from the explorer")
    result["partial"] = list(dict.fromkeys(result.get("partial", [])))
    hard = [
        k
        for k in (
            "status_success",
            "block_agrees",
            "to_agrees",
            "fee_exact",
            "erc20_amounts_agree",
            "internal_agrees",
            "gross_exact",
        )
        if checks.get(k) is False
    ]
    result["verdict"] = (
        "mismatch:" + ",".join(hard) if hard else ("partial" if result.get("partial") else "exact")
    )
    return result


def run_check(spec: ChainSpec, report: dict, pages_dir: Path, data_dir: Path) -> dict:
    rows = {r["tx_hash"].lower(): r for r in report["rows"]}
    decimals: dict[str, int] = {t.address.lower(): t.decimals for t in spec.tokens}
    symbols: dict[str, str] = {t.address.lower(): t.symbol for t in spec.tokens}
    for r in report["rows"]:
        if r.get("profit_token") and r.get("profit_decimals") is not None:
            decimals[r["profit_token"].lower()] = r["profit_decimals"]
            if r.get("profit_symbol"):
                symbols[r["profit_token"].lower()] = r["profit_symbol"]
    receipts: dict[str, dict] = {}
    block_hash: dict[int, str] = {}
    wanted = {p.stem.lower() for p in pages_dir.glob("0x*")}
    needed_blocks = {rows[h]["block_number"] for h in wanted if h in rows}
    for info in report["blocks"]:
        if info["number"] not in needed_blocks:
            continue
        path = data_dir / "blocks" / f"{info['number']}.json.gz"
        if path.exists():
            bundle = read_json(path)
            block_hash[info["number"]] = bundle["block"]["hash"]
            for rc in bundle["receipts"]:
                receipts[rc["transactionHash"].lower()] = rc
    results, skipped = [], []
    for page_path in sorted(pages_dir.glob("0x*")):
        tx_hash = page_path.stem.lower()
        row, receipt = rows.get(tx_hash), receipts.get(tx_hash)
        if row is None or receipt is None:
            skipped.append(
                {
                    "tx_hash": tx_hash,
                    "reason": "no classified row" if row is None else "block bundle not cached",
                }
            )
            continue
        trace_path = data_dir / "traces" / f"{block_hash.get(row['block_number'])}_{row['tx_hash']}.json.gz"
        trace = read_json(trace_path) if trace_path.exists() else None
        page = load_page(page_path)
        results.append(reconcile(spec, row, receipt, trace, page, decimals, symbols))
    summary = {
        "chain": spec.name,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "pages": len(results),
        "exact": sum(r["verdict"] == "exact" for r in results),
        "partial": sum(r["verdict"] == "partial" for r in results),
        "skipped": skipped,
        "mismatches": [r for r in results if r["verdict"].startswith("mismatch")],
        "results": results,
        "scope": "status, block, counterparties, fee, token transfer list, internal native transfers and the operator's "
        "result rebuilt from the explorer's data; 'partial' means everything the explorer shows agrees but it "
        "omits internal transfers (not indexed yet, or a page cap) that the cached trace supplies",
    }
    return summary


def write_markdown(spec: ChainSpec, summary: dict) -> Path:
    out_dir = spec.directory / "validation"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"explorer_check_{datetime.now(timezone.utc).strftime('%Y%m%d')}.md"
    mark = lambda v: "✓" if v else ("n/a" if v is None else "✗")  # noqa: E731
    lines = [
        f"# {spec.name}: explorer copies vs classifier",
        "",
        f"Checked {summary['checked_at']}. {summary['exact']}/{summary['pages']} transactions agree with the explorer on status, block, "
        f"counterparty, fee, every token transfer, the internal native transfers and the operator's result; {summary.get('partial', 0)} agree on "
        f"everything the explorer shows but the explorer omits internal transfers the trace has; {len(summary['mismatches'])} disagree.",
        "",
        "| Tx | Block | Source | Type | Fee | Tokens | Internal | Gross | Verdict |",
        "|---|---:|---|---|---|---|---|---|---|",
    ]
    for r in summary["results"]:
        c = r["checks"]
        lines.append(
            f"| [{r['tx_hash'][:10]}…]({spec.explorer_tx}{r['tx_hash']}) | {r['block']} | {r['source']} | {r.get('mev_type') or ''} | "
            f"{mark(c.get('fee_exact'))} | {mark(c.get('erc20_amounts_agree'))} | {mark(c.get('internal_agrees'))} | {mark(c.get('gross_exact'))} | "
            f"{r['verdict']}{(' — ' + '; '.join(r.get('partial', []) + r['notes'])) if (r['notes'] or r.get('partial')) else ''} |"
        )
    if summary.get("skipped"):
        lines += [
            "",
            "Skipped copies: "
            + ", ".join(f"{s['tx_hash'][:10]}… ({s['reason']})" for s in summary["skipped"]),
        ]
    path.write_text("\n".join(lines) + "\n")
    return path
