"""Pool lifecycle events: reserve syncs, singleton pool initialisation (pool id -> currencies, fee, hooks) and
liquidity changes."""

from __future__ import annotations

from typing import Any

from eth_abi import decode as abi_decode

from mevlab.decode.model import log_data, log_topics
from mevlab.decode.signatures import (
    INFINITY_CL_INITIALIZE_TOPIC,
    LIQUIDITY_TOPICS,
    SIG_INFINITY_CL_INITIALIZE,
    SIG_V4_INITIALIZE,
    SYNC_TOPICS,
    V4_INITIALIZE_TOPIC,
    event_topic,
)
from mevlab.hexutil import addr, to_int


def liquidity_change(log: dict[str, Any]) -> bool:
    """True for a liquidity event of a position (Uniswap V2/V3 Mint and Burn, V4 ModifyLiquidity). A zero-delta
    `ModifyLiquidity` or a zero-liquidity `Burn` is a fee collect: it is still the position holder's action (a fee
    harvest sold on the spot is an LP operation, Ethereum 26139831 #180, 26143936 #138), so it counts here; whether the
    holder is a hook acting inside the swap it serves is the trace's question (`hook_liquidity_only`)."""
    topics = log.get("topics") or []
    return bool(topics) and str(topics[0]).lower() in LIQUIDITY_TOPICS


def decode_sync_log(log: dict[str, Any]) -> tuple[str, int, int] | None:
    """(pool, reserve0, reserve1) from a V2 / Solidly Sync event."""
    t = log_topics(log)
    if not t or t[0] not in SYNC_TOPICS:
        return None
    data = log_data(log)
    if len(data) < 64:
        return None
    r0, r1 = abi_decode(["uint256", "uint256"], data[:64])
    return addr(log["address"]), r0, r1


def decode_infinity_initialize(log: dict[str, Any]) -> dict[str, Any] | None:
    """PancakeSwap Infinity CLPoolManager Initialize -> the same keys as decode_v4_initialize."""
    t = log_topics(log)
    if len(t) < 4 or t[0] != INFINITY_CL_INITIALIZE_TOPIC:
        return None
    hooks, fee, parameters, sqrt_p, tick = abi_decode(
        ["address", "uint24", "bytes32", "uint160", "int24"], log_data(log)
    )
    params = int.from_bytes(parameters, "big")
    return {
        "pool_id": t[1],
        "currency0": addr(t[2]),
        "currency1": addr(t[3]),
        "fee": fee,
        "tick_spacing": (params >> 16) & 0xFFFFFF,
        "hooks": hooks.lower(),
        "sqrt_price_x96": sqrt_p,
        "tick": tick,
        "block_number": to_int(log.get("blockNumber", 0)),
    }


def decode_pool_initialize(log: dict[str, Any], signature: str | None = None) -> dict[str, Any] | None:
    """Dispatch on the registry's `init_event` signature: Uniswap V4 or PancakeSwap Infinity layout."""
    if signature == SIG_INFINITY_CL_INITIALIZE:
        return decode_infinity_initialize(log)
    if signature in (None, SIG_V4_INITIALIZE):
        return decode_v4_initialize(log)
    t = log_topics(log)
    if t and t[0] == event_topic(signature):
        return (
            decode_infinity_initialize(log)
            if len(log_data(log)) >= 5 * 32 and "bytes32" in signature
            else decode_v4_initialize(log)
        )
    return None


def decode_v4_initialize(log: dict[str, Any]) -> dict[str, Any] | None:
    t = log_topics(log)
    if not t or t[0] != V4_INITIALIZE_TOPIC:
        return None
    fee, tick_spacing, hooks, sqrt_p, tick = abi_decode(
        ["uint24", "int24", "address", "uint160", "int24"], log_data(log)
    )
    return {
        "pool_id": t[1],
        "currency0": addr(t[2]),
        "currency1": addr(t[3]),
        "fee": fee,
        "tick_spacing": tick_spacing,
        "hooks": hooks.lower(),
        "sqrt_price_x96": sqrt_p,
        "tick": tick,
        "block_number": to_int(log.get("blockNumber", 0)),
    }
