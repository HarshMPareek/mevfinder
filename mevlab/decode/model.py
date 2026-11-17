"""What a decoded receipt is made of: swap legs (one per pool swap) and token transfers.

- `amount0` / `amount1` on a SwapLeg are POOL-perspective: positive = pool received, negative = pool paid.
  Uniswap V3 emits pool-perspective; V4 emits user-perspective, so V4 is negated on decode.
- token_in / token_out are filled only after pool metadata (token0/token1) is known, except for
  Balancer where the event carries them.
- ETH and WETH are one asset (`canonical_token`)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hexbytes import HexBytes

from mevlab.decode.signatures import NATIVE_ETH, ZERO_ADDRESS


def canonical_token(token: str | None, weth: str) -> str | None:
    """ETH ≡ WETH (token equivalence, 2608.20377 Def. 5): native ETH in any form maps to the chain's WETH."""
    if token is None:
        return None
    t = token.lower()
    return weth.lower() if t in (NATIVE_ETH, ZERO_ADDRESS) else t


# --- types ------------------------------------------------------------------------


@dataclass
class SwapLeg:
    kind: str  # cpmm_v2 | solidly_v2 | clmm_v3 | clmm_algebra | clmm_v4_singleton | vault | pmm_dodo
    #      | mm_pool_fcfa | clmm_pair_cd8b | liquidity_book (tokens inferred from the pool's transfers)
    pool: str  # emitting contract (pool, or PoolManager / Vault for singleton designs)
    log_index: int
    pool_id: str | None = None  # bytes32 for v4 / balancer
    dex: str | None = None  # resolved later from pool -> factory
    amount0: int | None = None  # pool-perspective
    amount1: int | None = None
    token_in: str | None = None
    token_out: str | None = None
    amount_in: int = 0
    amount_out: int = 0
    sqrt_price_x96: int | None = None
    liquidity: int | None = None
    tick: int | None = None
    sender: str | None = None
    recipient: str | None = None
    tx_hash: str | None = None

    @property
    def key(self) -> str:
        """Identity of the venue: pool address, or manager:pool_id for singletons."""
        return f"{self.pool}:{self.pool_id}" if self.pool_id else self.pool

    @property
    def zero_for_one(self) -> bool | None:
        if self.amount0 is None or self.amount1 is None:
            return None
        return self.amount0 > 0 and self.amount1 < 0

    def resolve_tokens(self, token0: str, token1: str) -> SwapLeg:
        """Fill token_in/out + amounts from pool-perspective amounts and pool token order."""
        if self.amount0 is None or self.amount1 is None:
            return self
        if self.amount0 > 0 and self.amount1 <= 0:
            self.token_in, self.amount_in = token0, self.amount0
            self.token_out, self.amount_out = token1, -self.amount1
        elif self.amount1 > 0 and self.amount0 <= 0:
            self.token_in, self.amount_in = token1, self.amount1
            self.token_out, self.amount_out = token0, -self.amount0
        else:  # both in or both out: pathological (donation / drain); keep the larger as input
            if abs(self.amount0) >= abs(self.amount1):
                self.token_in, self.amount_in = token0, abs(self.amount0)
                self.token_out, self.amount_out = token1, abs(self.amount1)
            else:
                self.token_in, self.amount_in = token1, abs(self.amount1)
                self.token_out, self.amount_out = token0, abs(self.amount0)
        return self


@dataclass
class Transfer:
    token: str
    sender: str
    receiver: str
    value: int
    log_index: int


def log_topics(log: dict[str, Any]) -> list[str]:
    return [t.lower() for t in log.get("topics", [])]


def log_data(log: dict[str, Any]) -> bytes:
    return bytes(HexBytes(log.get("data") or "0x"))
