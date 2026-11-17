"""Swap events -> SwapLeg, one decoder per DEX family (Uniswap V2/V3/V4, Solidly, PancakeSwap V3 and Infinity,
Algebra, Balancer V2/V3, Curve, DODO, Liquidity Book, Ekubo, Maverick V2, WOOFi and two unnamed Base families)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from eth_abi import decode as abi_decode

from mevlab.decode.model import SwapLeg, Transfer, log_data, log_topics
from mevlab.decode.signatures import (
    SIG_ALGEBRA_SWAP,
    SIG_BALANCER_SWAP,
    SIG_BALANCER_V3_SWAP,
    SIG_CURVE_CRYPTO_EXCHANGE,
    SIG_CURVE_CRYPTO_NG_EXCHANGE,
    SIG_CURVE_STABLE_EXCHANGE,
    SIG_CURVE_STABLE_UNDERLYING,
    SIG_DODO_SWAP,
    SIG_INFINITY_CL_SWAP,
    SIG_LB_SWAP,
    SIG_LB_V21_SWAP,
    SIG_MAVERICK_V2_SWAP,
    SIG_PANCAKE_V3_SWAP,
    SIG_SOLIDLY_SWAP,
    SIG_V2_SWAP,
    SIG_V3_SWAP,
    SIG_V4_SWAP,
    SIG_WOOFI_SWAP,
    TOPIC_MM_POOL_SWAP,
    TOPIC_PAIR_CD8B_SWAP,
    event_topic,
)
from mevlab.hexutil import addr, to_int

# Ekubo EVM Core (deterministic 0x00000000000014aA86C5d3c41765bb24e11bd701 on every chain). Its Swap is NOT a topic
# event: Core.sol `swap_6269342730` ends with `log0(o, 116)` of the packed bytes
#   locker (20 B) | poolId (32 B) | PoolBalanceUpdate (32 B: int128 delta0 << 128 | int128 delta1) |
#   PoolState (32 B: SqrtRatio uint96 << 160 | int32 tick << 128 | uint128 liquidity)
# [source: Sourcify exact match of Core, src/Core.sol, src/types/poolBalanceUpdate.sol, poolState.sol, sqrtRatio.sol,
# research repo 2026-10-05]. Deltas are POOL-perspective already (positive = the locker owes the pool). The SqrtRatio
# is a 96-bit floating format (two tag bits select a shift of 2 / 34 / 66 / 98 to a 64.128 fixed value); it is
# converted to the Q64.96 `sqrt_price_x96` the other decoders use. The pool's tokens come from `PoolInitialized(
# bytes32 poolId, (address token0, address token1, bytes32 config) poolKey, int32 tick, uint96 sqrtRatio)`.
EKUBO_CORE = "0x00000000000014aa86c5d3c41765bb24e11bd701"
EKUBO_LOG0_LEN = 116


# --- decoders -----------------------------------------------------------------------


def _decode_infinity_cl(log: dict[str, Any], kind: str) -> SwapLeg:
    """PancakeSwap Infinity CLPoolManager Swap: Uniswap V4 layout (user perspective, negated) + protocolFee."""
    t = log_topics(log)
    data = log_data(log)
    a0, a1, sqrt_p, liq, tick, fee, _protocol = abi_decode(
        ["int128", "int128", "uint160", "uint128", "int24", "uint24", "uint16"], data[: 7 * 32]
    )
    leg = _base(log, kind)
    leg.pool_id = t[1]
    leg.sender = addr(t[2]) if len(t) > 2 else None
    leg.amount0, leg.amount1 = -a0, -a1
    leg.sqrt_price_x96, leg.liquidity, leg.tick = sqrt_p, liq, tick
    return leg


def _decode_curve(log: dict[str, Any], kind: str, signed_ids: bool, extra: int = 0) -> SwapLeg:
    """Curve TokenExchange: amounts are known, coins are resolved from the pool's own Transfer logs."""
    t = log_topics(log)
    data = log_data(log)
    id_type = "int128" if signed_ids else "uint256"
    types = [id_type, "uint256", id_type, "uint256"] + ["uint256"] * extra
    _sold_id, sold, _bought_id, bought, *_ = abi_decode(types, data[: 32 * len(types)])
    leg = _base(log, kind)
    leg.sender = addr(t[1]) if len(t) > 1 else None
    leg.amount_in, leg.amount_out = sold, bought
    # coin indexes are not token0/token1: the coins come from the pool's Transfer logs (infer_tokens_from_transfers)
    leg.amount0, leg.amount1 = None, None
    leg.pool_id = None
    return leg


def _decode_maverick_v2(log: dict[str, Any], kind: str) -> SwapLeg:
    data = log_data(log)
    sender, recipient, (_amount, _token_a_in, _exact, _limit), amount_in, amount_out = abi_decode(
        ["address", "address", "(uint256,bool,bool,int32)", "uint256", "uint256"], data
    )
    leg = _base(log, kind)
    leg.sender, leg.recipient = addr(sender), addr(recipient)
    leg.amount_in, leg.amount_out = amount_in, amount_out
    leg.amount0, leg.amount1 = None, None
    return leg


def _decode_woofi(log: dict[str, Any], kind: str) -> SwapLeg:
    t = log_topics(log)
    data = log_data(log)
    from_amount, to_amount = abi_decode(["uint256", "uint256"], data[:64])
    leg = _base(log, kind)
    leg.token_in, leg.token_out = addr(t[1]), addr(t[2])
    leg.amount_in, leg.amount_out = from_amount, to_amount
    leg.recipient = addr(t[3]) if len(t) > 3 else None
    return leg


def _decode_balancer_v3(log: dict[str, Any], kind: str) -> SwapLeg:
    t = log_topics(log)
    data = log_data(log)
    amount_in, amount_out = abi_decode(["uint256", "uint256"], data[:64])
    leg = _base(log, kind)
    leg.pool_id = t[1]  # the pool address, as the venue identity under the shared Vault
    leg.token_in, leg.token_out = addr(t[2]), addr(t[3])
    leg.amount_in, leg.amount_out = amount_in, amount_out
    return leg


def _base(log: dict[str, Any], kind: str) -> SwapLeg:
    return SwapLeg(
        kind=kind,
        pool=addr(log["address"]),
        log_index=to_int(log.get("logIndex", 0)),
        tx_hash=log.get("transactionHash"),
    )


def _decode_v2_like(log: dict[str, Any], kind: str) -> SwapLeg:
    t = log_topics(log)
    a0in, a1in, a0out, a1out = abi_decode(["uint256"] * 4, log_data(log))
    leg = _base(log, kind)
    leg.sender = addr(t[1]) if len(t) > 1 else None
    leg.recipient = addr(t[2]) if len(t) > 2 else None
    leg.amount0 = a0in - a0out
    leg.amount1 = a1in - a1out
    return leg


def _decode_v3_like(log: dict[str, Any], kind: str, pancake: bool = False, algebra: bool = False) -> SwapLeg:
    t = log_topics(log)
    types = ["int256", "int256", "uint160", "uint128", "int24"]
    if pancake:
        types += ["uint128", "uint128"]
    if algebra:
        types += ["uint24", "uint24"]
    vals = abi_decode(types, log_data(log))
    leg = _base(log, kind)
    leg.sender = addr(t[1]) if len(t) > 1 else None
    leg.recipient = addr(t[2]) if len(t) > 2 else None
    leg.amount0, leg.amount1, leg.sqrt_price_x96, leg.liquidity, leg.tick = vals[:5]
    return leg


def _decode_v4(log: dict[str, Any], kind: str) -> SwapLeg:
    t = log_topics(log)
    a0, a1, sqrt_p, liq, tick, _fee = abi_decode(
        ["int128", "int128", "uint160", "uint128", "int24", "uint24"], log_data(log)
    )
    leg = _base(log, kind)
    leg.pool_id = t[1] if len(t) > 1 else None
    leg.sender = addr(t[2]) if len(t) > 2 else None
    # V4 deltas are user-perspective (negative = user paid). Normalise to pool-perspective.
    leg.amount0, leg.amount1 = -a0, -a1
    leg.sqrt_price_x96, leg.liquidity, leg.tick = sqrt_p, liq, tick
    return leg


def _decode_balancer(log: dict[str, Any], kind: str) -> SwapLeg:
    t = log_topics(log)
    amount_in, amount_out = abi_decode(["uint256", "uint256"], log_data(log))
    leg = _base(log, kind)
    leg.pool_id = t[1] if len(t) > 1 else None
    leg.token_in = addr(t[2]) if len(t) > 2 else None
    leg.token_out = addr(t[3]) if len(t) > 3 else None
    leg.amount_in, leg.amount_out = amount_in, amount_out
    return leg


def _decode_dodo(log: dict[str, Any], kind: str) -> SwapLeg:
    from_tok, to_tok, from_amt, to_amt, trader, receiver = abi_decode(
        ["address", "address", "uint256", "uint256", "address", "address"], log_data(log)
    )
    leg = _base(log, kind)
    leg.token_in, leg.amount_in = addr(from_tok), from_amt  # pool received fromAmount of fromToken
    leg.token_out, leg.amount_out = addr(to_tok), to_amt
    leg.sender, leg.recipient = addr(trader), addr(receiver)
    return leg


def _decode_mm_pool(log: dict[str, Any], kind: str) -> SwapLeg:
    token_in, amount_in, amount_out, _flag, recipient = abi_decode(
        ["address", "uint256", "uint256", "uint256", "address"], log_data(log)
    )
    leg = _base(log, kind)
    leg.token_in, leg.amount_in, leg.amount_out = addr(token_in), amount_in, amount_out
    leg.recipient = addr(recipient)  # token_out: the pool's own Transfer of amount_out (pipeline)
    return leg


def _decode_pair_cd8b(log: dict[str, Any], kind: str) -> SwapLeg:
    sender, recipient, _flag, a0, a1, _x, _price = abi_decode(
        ["address", "address", "uint256", "int256", "int256", "int256", "uint256"], log_data(log)
    )
    leg = _base(log, kind)
    leg.sender, leg.recipient = addr(sender), addr(recipient)
    leg.amount0, leg.amount1 = a0, a1  # pool-perspective in sorted token order (checked on chain)
    return leg


def _decode_lb(log: dict[str, Any], kind: str) -> SwapLeg:
    t = log_topics(log)
    swap_for_y, amount_in, amount_out, _vol, fees = abi_decode(
        ["bool", "uint256", "uint256", "uint256", "uint256"], log_data(log)
    )
    leg = _base(log, kind)
    leg.sender = addr(t[1]) if len(t) > 1 else None
    leg.recipient = addr(t[2]) if len(t) > 2 else None
    got = amount_in + fees  # what the pool received in this bin (fee is charged on the input)
    # pool-perspective, X then Y (the pool's own order, not address order)
    leg.amount0, leg.amount1 = (got, -amount_out) if swap_for_y else (-amount_out, got)
    return leg


def _decode_lb_v21(log: dict[str, Any], kind: str) -> SwapLeg:
    t = log_topics(log)
    _id, amounts_in, amounts_out, _vol, _fees, _protocol = abi_decode(
        ["uint24", "bytes32", "bytes32", "uint24", "bytes32", "bytes32"], log_data(log)
    )
    mask = (1 << 128) - 1
    x_in, y_in = int.from_bytes(amounts_in, "big") & mask, int.from_bytes(amounts_in, "big") >> 128
    x_out, y_out = int.from_bytes(amounts_out, "big") & mask, int.from_bytes(amounts_out, "big") >> 128
    leg = _base(log, kind)
    leg.sender = addr(t[1]) if len(t) > 1 else None
    leg.recipient = addr(t[2]) if len(t) > 2 else None
    leg.amount0, leg.amount1 = x_in - x_out, y_in - y_out  # pool-perspective, X then Y
    return leg


def merge_bin_legs(legs: list[SwapLeg], kind: str = "liquidity_book") -> list[SwapLeg]:
    """One Liquidity Book call emits a Swap per bin crossed; sum consecutive events of the same pool into one
    leg so its amounts match the pool's transfers (and the cycle has one leg per venue visit)."""
    out: list[SwapLeg] = []
    for leg in legs:
        prev = out[-1] if out else None
        if (
            leg.kind == kind
            and prev is not None
            and prev.kind == kind
            and prev.pool == leg.pool
            and prev.amount0 is not None
            and leg.amount0 is not None
        ):
            prev.amount0 += leg.amount0
            prev.amount1 += leg.amount1
            continue
        out.append(leg)
    return out


def infer_tokens_from_transfers(leg: SwapLeg, transfers: list[Transfer]) -> bool:
    """Fill the missing token(s) of a leg from the pool's own ERC-20 transfers in the same transaction:
    the token the pool received in exactly the leg's input amount, the token it sent in exactly the output
    amount. Used for venues whose pool contract exposes no token0()/token1(). Returns True when complete."""
    if leg.token_in and leg.token_out:
        return True
    got = [t for t in transfers if t.receiver == leg.pool]
    sent = [t for t in transfers if t.sender == leg.pool]
    if leg.amount0 is not None and leg.amount1 is not None and not leg.token_in:
        pos = [a for a in (leg.amount0, leg.amount1) if a > 0]
        neg = [-a for a in (leg.amount0, leg.amount1) if a < 0]
        if len(pos) != 1 or len(neg) != 1:
            return False
        tin = {t.token for t in got if t.value == pos[0]}
        tout = {t.token for t in sent if t.value == neg[0]}
        if len(tin) != 1 or len(tout) != 1:
            return False
        leg.token_in, leg.amount_in = tin.pop(), pos[0]
        leg.token_out, leg.amount_out = tout.pop(), neg[0]
        return True
    if leg.token_in and not leg.token_out and leg.amount_out:
        tout = {t.token for t in sent if t.value == leg.amount_out and t.token != leg.token_in}
        if len(tout) == 1:
            leg.token_out = tout.pop()
            return True
    if (
        not leg.token_in
        and not leg.token_out
        and leg.amount0 is None
        and leg.amount_in > 0
        and leg.amount_out > 0
    ):
        # Curve TokenExchange: coin indexes and plain amounts, no token addresses. The coins are the token the pool
        # received in exactly tokens_sold and the token it sent in exactly tokens_bought. Without this branch every
        # Curve leg stayed unresolved and one unresolved leg blocks a verdict: 862 Ethereum and 2,447 OP live rows,
        # among them a USDC -> crvFRAX -> RSR -> WETH -> USDC cycle left uncounted (Ethereum 26314163 #15).
        tin = {t.token for t in got if t.value == leg.amount_in}
        tout = {t.token for t in sent if t.value == leg.amount_out}
        if len(tin) == 1 and len(tout) == 1 and tin != tout:
            leg.token_in, leg.token_out = tin.pop(), tout.pop()
            return True
    return False


Decoder = Callable[[dict[str, Any]], SwapLeg]

# topic0 -> decoder. Uniswap V3, Slipstream and Sushi V3 share a topic; dex is resolved later.
SWAP_DECODERS: dict[str, Decoder] = {
    event_topic(SIG_V2_SWAP): lambda log: _decode_v2_like(log, "cpmm_v2"),
    event_topic(SIG_SOLIDLY_SWAP): lambda log: _decode_v2_like(log, "solidly_v2"),
    event_topic(SIG_V3_SWAP): lambda log: _decode_v3_like(log, "clmm_v3"),
    event_topic(SIG_PANCAKE_V3_SWAP): lambda log: _decode_v3_like(log, "clmm_v3", pancake=True),
    event_topic(SIG_ALGEBRA_SWAP): lambda log: _decode_v3_like(log, "clmm_algebra", algebra=True),
    event_topic(SIG_V4_SWAP): lambda log: _decode_v4(log, "clmm_v4_singleton"),
    event_topic(SIG_BALANCER_SWAP): lambda log: _decode_balancer(log, "vault"),
    event_topic(SIG_DODO_SWAP): lambda log: _decode_dodo(log, "pmm_dodo"),
    TOPIC_MM_POOL_SWAP: lambda log: _decode_mm_pool(log, "mm_pool_fcfa"),
    TOPIC_PAIR_CD8B_SWAP: lambda log: _decode_pair_cd8b(log, "clmm_pair_cd8b"),
    event_topic(SIG_LB_SWAP): lambda log: _decode_lb(log, "liquidity_book"),
    event_topic(SIG_LB_V21_SWAP): lambda log: _decode_lb_v21(log, "liquidity_book"),
    event_topic(SIG_INFINITY_CL_SWAP): lambda log: _decode_infinity_cl(log, "clmm_v4_singleton"),
    event_topic(SIG_CURVE_STABLE_EXCHANGE): lambda log: _decode_curve(log, "curve", True),
    event_topic(SIG_CURVE_STABLE_UNDERLYING): lambda log: _decode_curve(log, "curve", True),
    event_topic(SIG_CURVE_CRYPTO_EXCHANGE): lambda log: _decode_curve(log, "curve", False),
    event_topic(SIG_CURVE_CRYPTO_NG_EXCHANGE): lambda log: _decode_curve(log, "curve", False, extra=2),
    event_topic(SIG_BALANCER_V3_SWAP): lambda log: _decode_balancer_v3(log, "vault"),
    event_topic(SIG_MAVERICK_V2_SWAP): lambda log: _decode_maverick_v2(log, "maverick_v2"),
    event_topic(SIG_WOOFI_SWAP): lambda log: _decode_woofi(log, "pmm_woofi"),
}


def _ekubo_sqrt_ratio_to_fixed(raw: int) -> int:
    """SqrtRatio (uint96 floating container) -> 64.128 fixed sqrt price, as Core's `toFixed`."""
    tag = (raw >> 94) & 0x3
    mant = raw & ((1 << 94) - 1)
    return mant << (2 + 32 * tag)


def decode_ekubo_log0(log: dict[str, Any], core: str = EKUBO_CORE) -> SwapLeg | None:
    """Ekubo Core anonymous Swap (log0, 116 bytes, no topics). None for any other log of Core (its other
    anonymous records, if any, have other lengths) or for a topic log."""
    if log_topics(log) or addr(log["address"]) != addr(core):
        return None
    data = log_data(log)
    if len(data) != EKUBO_LOG0_LEN:
        return None
    locker = "0x" + data[:20].hex()
    pool_id = "0x" + data[20:52].hex()
    st = int.from_bytes(data[84:116], "big")
    d0 = int.from_bytes(data[52:68], "big", signed=True)
    d1 = int.from_bytes(data[68:84], "big", signed=True)
    sqrt_raw = st >> 160
    tick = int.from_bytes(((st >> 128) & 0xFFFFFFFF).to_bytes(4, "big"), "big", signed=True)
    liquidity = st & ((1 << 128) - 1)
    leg = _base(log, "ekubo_core")
    leg.pool_id = pool_id
    leg.sender = locker
    leg.amount0, leg.amount1 = d0, d1  # pool-perspective already
    leg.sqrt_price_x96 = _ekubo_sqrt_ratio_to_fixed(sqrt_raw) >> 32  # 64.128 -> Q64.96
    leg.liquidity, leg.tick = liquidity, tick
    return leg


def decode_swap_log(log: dict[str, Any]) -> SwapLeg | None:
    t = log_topics(log)
    if not t:
        try:
            return decode_ekubo_log0(log)
        except Exception:
            return None
    dec = SWAP_DECODERS.get(t[0])
    if dec is None:
        return None
    try:
        return dec(log)
    except Exception:  # malformed / non-standard emitter with a colliding topic
        return None
