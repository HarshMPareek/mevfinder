from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

ArbClass = Literal[
    "system_deposit",
    "atomic_cycle",  # closed token loop through >=2 distinct venues (candidate arb; Layer 2 pending)
    "round_trip",  # closed loop through a single venue (wash / volume farming; cannot profit after fees)
    "attempted_cycle",  # geometric cycle through >=2 venues but no positive balance at the callee (lost)
    "settlement_like",  # cycle that closes through an intent settlement (UniswapX/1inch/CoW fill events)
    "hook_mechanism",  # the event-layer cycle was executed by a Uniswap V4 hook inside the party's swap (engine)
    "split_route",  # outputs not consumed by later legs (aggregator / multi-route)
    "single_swap",  # one leg (user trade or non-atomic CEX-DEX leg)
    "lp_action",  # same-tx mint/burn or LP-token gain (zap / JIT / rebalance)
    "probe",  # success, no external logs, value <= 1 wei
    "reverted",  # failed attempt (Layer 4 recovery input)
    "no_swaps",  # transfers only / other
    "pool_exploit",  # a leg beat its own pool's pre-swap price: broken pool math (engine), never an arbitrage
    "share_operation",  # the sender's shares of the callee burned or minted: a deposit / withdrawal at the issuer (engine)
    "unknown",
]

TriggerType = Literal["backrun", "top_of_block", "top_of_flashblock", "mid_block", "unknown"]


class ArbLabel(BaseModel):
    """Output schema of the layered classifier (index §6)."""

    tx_hash: str
    block_number: int
    tx_index: int
    tx_from: str | None = None
    tx_to: str | None = None
    is_arb: bool
    arb_class: ArbClass
    n_legs: int = 0
    tokens: list[str] = Field(default_factory=list)
    venues: list[str] = Field(default_factory=list)
    kinds: list[str] = Field(default_factory=list)
    dexes: list[str] = Field(default_factory=list)
    sequential_chain: bool | None = None
    beneficiary: str | None = None
    profit_token: str | None = None
    gross_profit_raw: int | None = None  # cycle leg arithmetic (entry token bought − sold) when a cycle
    gross_delta_raw: int | None = None  # beneficiary's net + netted payouts (attribution view)
    dislocation_evidence: dict[str, Any] | None = None  # Layer 2 (pending)
    slot_index_est: int | None = None  # flashblock index ESTIMATED from fee runs (hypothesis C-CHAIN-5)
    tof_est: bool | None = None  # first tx of an estimated flashblock (>1) with that slot's max fee
    trigger_type: TriggerType = "unknown"
    trigger_tx_index: int | None = None
    trigger_pool: str | None = None
    economics: dict[str, Any] | None = None
    # venue -> +1 / -1 direction the price moved because of this tx (0 or absent = unknown)
    price_moves: dict[str, int] = Field(default_factory=dict)
    # token -> {recipient -> amount} the callee paid out of the cycle (collectors, kickbacks)
    payouts: dict[str, dict[str, str]] = Field(default_factory=dict)
    # events cannot give the gross (hooked V4 zero-amount leg): the trace pass decides
    gross_unreliable: bool = False
    confidence: float = 0.0
    reasons: list[str] = Field(default_factory=list)
    classifier_version: str = "0.1.0"
