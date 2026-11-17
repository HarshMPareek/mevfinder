"""Geometry of a cycle: pre-trade prices, cycle product, price-move direction, amount continuity.

Prices are ln(token1/token0) in raw units per pool ("log price"). V3/V4 legs carry the post-swap
sqrtPriceX96; V2/Solidly pools carry reserves in the Sync event emitted right after each swap. The tracker
stores the last known log price per venue, so the value stored *before* a tx's legs are applied is the
pre-trade price. Liquidity changes do not move the price, so a stale value between two swaps is exact."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from mevlab.decode.model import SwapLeg

Q96 = 2**96


@dataclass
class PoolPriceTracker:
    log_price: dict[str, float] = field(default_factory=dict)  # venue key -> ln(p1/p0)
    seen_swaps: dict[str, int] = field(default_factory=dict)

    def get(self, key: str) -> float | None:
        return self.log_price.get(key)

    def apply_leg(self, leg: SwapLeg) -> tuple[float | None, float | None]:
        """Record the post-swap price of a V3/V4 leg. Returns (pre, post) log prices."""
        pre = self.log_price.get(leg.key)
        post = None
        if leg.sqrt_price_x96:
            post = 2 * math.log(leg.sqrt_price_x96 / Q96)
            self.log_price[leg.key] = post
        self.seen_swaps[leg.key] = self.seen_swaps.get(leg.key, 0) + 1
        return pre, post

    def apply_sync(self, pool: str, r0: int, r1: int) -> None:
        if r0 > 0 and r1 > 0:
            self.log_price[pool] = math.log(r1 / r0)


def leg_log_rate(leg: SwapLeg, log_price: float) -> float | None:
    """ln(out per in) at spot for a leg, given the pool log price ln(p1/p0), ignoring fees.
    Selling token0 for token1 yields p1/p0 per unit; selling token1 yields 1/(p1/p0)."""
    if leg.zero_for_one is None:
        return None
    return log_price if leg.zero_for_one else -log_price


def cycle_pre_log_product(legs: list[SwapLeg], pre_prices: dict[str, float]) -> float | None:
    """Σ ln(rate) around the cycle using pre-trade prices. > 0 means the cycle was profitable at zero size
    before fees; ≤ 0 means the loop was not an arbitrage geometry (settlement, wash, zap)."""
    total = 0.0
    for leg in legs:
        lp = pre_prices.get(leg.key)
        if lp is None:
            return None
        r = leg_log_rate(leg, lp)
        if r is None:
            return None
        total += r
    return total


def realized_log_product(legs: list[SwapLeg]) -> float | None:
    """Σ ln(amount_out/amount_in) from the events themselves (realized rates, fees included)."""
    total = 0.0
    for leg in legs:
        if leg.amount_in <= 0 or leg.amount_out <= 0:
            return None
        total += math.log(leg.amount_out / leg.amount_in)
    return total


INVARIANT_KINDS = {"clmm_v3", "clmm_algebra", "clmm_v4_singleton"}
INVARIANT_TOLERANCE = math.log(1.01)


def invariant_violations(
    legs: list[SwapLeg], hooked: set[str] | None = None, pre_prices: dict[str, float] | None = None
) -> list[tuple[str, float]]:
    """Concentrated-liquidity pools whose swaps in this transaction broke the pool's own price math.

    Two facts hold in every correct concentrated-liquidity pool, whatever its fee, liquidity or ticks (minting,
    burning and fee reinvestment never move the price):
      (a) a swap never executes at a better rate than the pool's price just before it;
      (b) the price moves against the pool's net flow: a pool that paid out token0 on balance ends with a higher
          token1/token0 price than it started with, and one that took token0 in ends lower.
    The price before a swap is the previous Swap event's post-swap sqrtPrice of the same pool in the same
    transaction (exact), else the price the tracker held before the transaction (`pre_prices`), else the bound the
    first leg itself gives (selling token0 at rate r means the pool started at >= r; buying at price p means it
    started at <= p). OP 0x782ca99b… (KyberSwap Elastic): the bot sold WETH at 2,070.9 USDC.e, so the pool started at
    >= 2,070.9; it then paid out 0.447 WETH net and still ended at 2,038.3 — the tick-crossing exploit, not a price
    difference between venues. Hooked pools are skipped (a hook may price a swap however it likes).
    Returns [(venue key, ln excess beyond the bound)]."""
    hooked = hooked or set()
    pre_prices = pre_prices or {}
    by_pool: dict[str, list[SwapLeg]] = {}
    for leg in sorted(legs, key=lambda x: x.log_index):
        if (
            leg.kind in INVARIANT_KINDS
            and leg.key not in hooked
            and leg.sqrt_price_x96
            and leg.token_in
            and leg.token_out
            and leg.amount_in > 0
            and leg.amount_out > 0
            and leg.amount0 is not None
            and leg.amount1 is not None
            and leg.zero_for_one is not None
        ):
            by_pool.setdefault(leg.key, []).append(leg)
    out: list[tuple[str, float]] = []
    for key, pool_legs in by_pool.items():
        worst = 0.0
        last_post: float | None = None
        for leg in pool_legs:  # (a) each leg against the pool's exact pre-swap price
            if last_post is not None:
                spot = leg_log_rate(leg, last_post)
                if spot is not None:
                    worst = max(worst, math.log(leg.amount_out / leg.amount_in) - spot)
            last_post = 2 * math.log(leg.sqrt_price_x96 / Q96)
        first, post = pool_legs[0], last_post
        net0 = sum(leg.amount0 for leg in pool_legs)
        rate = math.log(abs(first.amount1) / abs(first.amount0)) if first.amount0 and first.amount1 else None
        pre = pre_prices.get(key)
        if net0 < 0:  # (b) the pool paid out token0: its price must not end below where it started
            lower = pre if pre is not None else (rate if first.zero_for_one else None)
            if lower is not None and post is not None:
                worst = max(worst, lower - post)
        elif net0 > 0:  # the pool took token0 in: its price must not end above where it started
            upper = pre if pre is not None else (rate if not first.zero_for_one else None)
            if upper is not None and post is not None:
                worst = max(worst, post - upper)
        if worst > INVARIANT_TOLERANCE:
            out.append((key, worst))
    return out


def price_move_sign(pre: float | None, post: float | None) -> int:
    if pre is None or post is None or pre == post:
        return 0
    return 1 if post > pre else -1
