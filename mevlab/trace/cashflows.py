"""Observed cash and claim movements, independent of arbitrage/owner classification.

Receipt logs establish ERC20 movements; callTracer establishes successful native
value movements. These are flow-derived deltas, not pre/post-state proofs for
arbitrary tokens. ETH, WETH and ERC6909 claims deliberately stay separate.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from mevlab.hexutil import addr, to_int

TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
CLAIM_TRANSFER = "0x1b3d7edb2e9c0b0e7c525b20aaaef0f5940d2ed71663c7d39266ecafac728859"


def native_flows(trace: dict[str, Any]) -> list[tuple[str, str | None, int]]:
    """Successful native transfers, including direct payments from a WETH proxy."""
    result = []

    def walk(frame):
        if frame.get("error") or frame.get("revertReason"):
            return
        if frame.get("type", "CALL") in {"CALL", "CREATE", "CREATE2", "SELFDESTRUCT"}:
            value = to_int(frame.get("value", "0x0"))
            if value and frame.get("from"):
                result.append((addr(frame["from"]), addr(frame["to"]) if frame.get("to") else None, value))
        for child in frame.get("calls") or []:
            walk(child)

    walk(trace)
    return result


def _sparse_strings(values):
    return {key: str(value) for key, value in values.items() if value}


def cashflow_evidence(
    trace: dict[str, Any],
    logs: list[dict[str, Any]] | None,
    weth: str,
    recipients: dict[str, str],
    *,
    receipt_status: int | str | None = None,
) -> dict[str, Any]:
    """Keep raw address deltas and known-recipient receipts even if closure fails.

    Known-recipient amounts are gross ETH+WETH receipts, not inferred owner payouts.
    Both the incoming and net amount are exposed so refunds/forwarding are auditable.
        No arbitrary recipient is automatically treated as an external fee taker.
    """
    trace_failed = bool(trace.get("error") or trace.get("revertReason"))
    if receipt_status is not None and trace_failed != (to_int(receipt_status) == 0):
        raise ValueError("Trace root status disagrees with receipt; cashflows are unknown")
    native, token, claim = defaultdict(int), defaultdict(lambda: defaultdict(int)), defaultdict(int)
    incoming = {addr(who): {"native_wei": 0, "weth_raw": 0} for who in recipients}
    transfers = native_flows(trace)
    for sender, receiver, amount in transfers:
        native[sender] -= amount
        native[receiver or "unknown_native_recipient"] += amount
        if receiver in incoming:
            incoming[receiver]["native_wei"] += amount
    # Logs from a reverted transaction must not establish token proceeds.
    if not (trace.get("error") or trace.get("revertReason")):
        for log in logs or []:
            topics = log.get("topics") or []
            if not topics:
                continue
            signature = str(topics[0]).lower()
            data = str(log.get("data") or "0x").removeprefix("0x")
            if len(topics) == 3 and signature == TRANSFER and len(data) == 64:
                currency = addr(log["address"])
                sender, receiver = ("0x" + str(x)[-40:].lower() for x in topics[1:3])
                amount = int(data, 16)
                token[sender][currency] -= amount
                token[receiver][currency] += amount
                if currency == weth.lower() and receiver in incoming:
                    incoming[receiver]["weth_raw"] += amount
            elif len(topics) == 4 and signature == CLAIM_TRANSFER and len(data) >= 128:
                manager = addr(log["address"])
                sender, receiver = ("0x" + str(x)[-40:].lower() for x in topics[1:3])
                currency_id = str(int(str(topics[3]), 16))
                amount = int(data[64:128], 16)
                claim[(manager, currency_id, sender)] -= amount
                claim[(manager, currency_id, receiver)] += amount
    known = {}
    for who, received in incoming.items():
        known[who] = {
            **{key: str(value) for key, value in received.items()},
            "eth_weth_received_wei": str(sum(received.values())),
            "native_net_wei": str(native.get(who, 0)),
            "weth_net_raw": str(token.get(who, {}).get(weth.lower(), 0)),
        }
    return {
        "version": 1,
        "native_net_wei": _sparse_strings(native),
        "erc20_net_raw": {
            who: _sparse_strings(values) for who, values in token.items() if any(values.values())
        },
        "erc6909_net_raw": [
            {"manager": manager, "currency_id": currency_id, "address": who, "delta_raw": str(value)}
            for (manager, currency_id, who), value in claim.items()
            if value
        ],
        "known_recipient_receipts": known,
        "native_trace_available": True,
        "receipt_logs_available": logs is not None,
        "owner_attribution_verified": False,
        "cash_and_claims_separate": True,
    }
