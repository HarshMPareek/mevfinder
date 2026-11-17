"""Layer 5: economics. Gross profit, execution fee, L1 data fee, base fee and priority fee are reported
SEPARATELY. Never collapse them into one net number at this layer (index §6, §8).

Fee destinations by chain family (verified from OP Stack specs 2026-09-05; Arbitrum docs):
- op-stack (Base, Optimism): priority fee -> SequencerFeeVault (block coinbase), base fee -> BaseFeeVault
  (not burned), L1 data fee (`l1Fee` receipt field) -> L1FeeVault. All three are operator revenue.
- arbitrum (Arbitrum One, Robinhood Chain): receipt `gasUsedForL1` is the L1 component folded into gasUsed;
  fee = gasUsed * effectiveGasPrice; tips are not honoured by FCFS ordering (Timeboost sells priority).
- ethereum: base fee burned; priority fee to the proposer/builder.
- bsc: same arithmetic as ethereum; the base fee is 0 so the whole gas price is the validator's.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from mevlab.hexutil import to_int


@dataclass
class Economics:
    family: str
    gas_used: int
    effective_gas_price: int
    base_fee_per_gas: int
    priority_fee_per_gas: int
    total_fee_wei: int  # everything the sender paid for execution (+ L1 data on op-stack)
    base_fee_wei: int  # gas_used * base fee (to BaseFeeVault / burned / ArbOS)
    priority_fee_wei: int  # gas_used * (effective - base)
    l1_fee_wei: int  # op-stack: receipt l1Fee; arbitrum: gasUsedForL1 * effective price; ethereum: 0
    l1_gas_used: int  # arbitrum gasUsedForL1, op-stack l1GasUsed if present
    timeboosted: bool | None  # arbitrum express-lane flag
    gross_profit_raw: dict[str, int]  # token -> raw units (positive beneficiary deltas)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def economics_from_receipt(
    receipt: dict[str, Any],
    base_fee_per_gas: int,
    gross: dict[str, int],
    family: str = "op-stack",
) -> Economics:
    gas_used = to_int(receipt.get("gasUsed"))
    egp = to_int(receipt.get("effectiveGasPrice"))
    prio = max(egp - base_fee_per_gas, 0)
    exec_fee = gas_used * egp
    if family == "op-stack":
        l1_fee = to_int(receipt.get("l1Fee"))
        l1_gas = to_int(receipt.get("l1GasUsed"))
        total = exec_fee + l1_fee
        base_wei = gas_used * base_fee_per_gas
        timeboosted = None
    elif family == "arbitrum":
        l1_gas = to_int(receipt.get("gasUsedForL1"))
        l1_fee = l1_gas * egp
        total = exec_fee
        base_wei = (gas_used - l1_gas) * base_fee_per_gas
        tb = receipt.get("timeboosted")
        timeboosted = bool(tb) if tb is not None else None
    else:
        l1_fee, l1_gas, total, timeboosted = 0, 0, exec_fee, None
        base_wei = gas_used * base_fee_per_gas
    return Economics(
        family=family,
        gas_used=gas_used,
        effective_gas_price=egp,
        base_fee_per_gas=base_fee_per_gas,
        priority_fee_per_gas=prio,
        total_fee_wei=total,
        base_fee_wei=base_wei,
        priority_fee_wei=gas_used * prio,
        l1_fee_wei=l1_fee,
        l1_gas_used=l1_gas,
        timeboosted=timeboosted,
        gross_profit_raw={k: v for k, v in gross.items() if v > 0},
    )
