"""Token movements from receipt logs: ERC-20 Transfer events and WETH deposits / withdrawals (wraps)."""

from __future__ import annotations

from typing import Any

from eth_abi import decode as abi_decode

from mevlab.decode.model import Transfer, log_data, log_topics
from mevlab.decode.signatures import (
    NATIVE_ETH,
    TRANSFER_TOPIC,
    WETH_DEPOSIT_TOPIC,
    WETH_WITHDRAWAL_TOPIC,
    ZERO_ADDRESS,
)
from mevlab.hexutil import addr, to_int


def decode_transfer_log(log: dict[str, Any]) -> Transfer | None:
    t = log_topics(log)
    if len(t) != 3 or t[0] != TRANSFER_TOPIC:
        return None  # ERC-721 Transfer has 4 topics; skipped on purpose
    data = log_data(log)
    if len(data) < 32:
        return None
    (value,) = abi_decode(["uint256"], data[:32])
    return Transfer(
        token=addr(log["address"]),
        sender=addr(t[1]),
        receiver=addr(t[2]),
        value=value,
        log_index=to_int(log.get("logIndex", 0)),
    )


def decode_weth_log(log: dict[str, Any], weth: str) -> list[Transfer]:
    """WETH9 Deposit/Withdrawal do not emit Transfer. Model them as WETH<->native ETH moves."""
    t = log_topics(log)
    if len(t) != 2 or addr(log["address"]) != addr(weth):
        return []
    data = log_data(log)
    if len(data) < 32:
        return []
    (wad,) = abi_decode(["uint256"], data[:32])
    who = addr(t[1])
    li = to_int(log.get("logIndex", 0))
    w = addr(weth)
    if t[0] == WETH_DEPOSIT_TOPIC:
        return [Transfer(w, w, who, wad, li), Transfer(NATIVE_ETH, who, w, wad, li)]
    if t[0] == WETH_WITHDRAWAL_TOPIC:
        return [Transfer(w, who, w, wad, li), Transfer(NATIVE_ETH, w, who, wad, li)]
    return []


def weth_mint_burn(tr: Transfer, weth: str) -> list[Transfer]:
    """Some chains' WETH is a proxy that mints / burns on wrap and unwrap (Transfer from or to
    address(0)) instead of emitting WETH9's Deposit / Withdrawal (Robinhood Chain WETH 0x0bd7d308…,
    [chain, 2026-09-07]). Under the ETH ≡ WETH equivalence such a mint is a conversion, not a gain:
    model it exactly like decode_weth_log does for Deposit / Withdrawal. [] for an ordinary transfer."""
    w = addr(weth)
    if addr(tr.token) != w:
        return []
    if addr(tr.sender) == ZERO_ADDRESS:
        return [
            Transfer(w, w, tr.receiver, tr.value, tr.log_index),
            Transfer(NATIVE_ETH, tr.receiver, w, tr.value, tr.log_index),
        ]
    if addr(tr.receiver) == ZERO_ADDRESS:
        return [
            Transfer(w, tr.sender, w, tr.value, tr.log_index),
            Transfer(NATIVE_ETH, w, tr.sender, tr.value, tr.log_index),
        ]
    return []
