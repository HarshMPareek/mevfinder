"""Exact integer fee accounting per chain family, around the unchanged classifier.

op-stack (Base, Optimism): total = gasUsed × effectiveGasPrice + receipt.l1Fee + operator fee.
arbitrum (Arbitrum One, Robinhood): total = gasUsed × effectiveGasPrice; the L1 component is already folded
    into gasUsed and reported separately as gasUsedForL1 × effectiveGasPrice (informational, not added again).
ethereum / bsc: total = gasUsed × effectiveGasPrice; base fee burned (Ethereum) or zero (BSC); no L1 fee.
Builder payments made by native transfer inside the transaction are not fees; they are reported as
`builder_tip_wei` on the row by the trace pass.
"""

from __future__ import annotations

from decimal import Decimal, localcontext

from arbkit.chain.spec import ChainSpec
from mevlab.hexutil import to_int


def units(value: int | str | None, decimals: int = 18) -> str:
    if value is None:
        return "unknown"
    with localcontext() as ctx:
        ctx.prec = 100
        return format(Decimal(value) / Decimal(10) ** decimals, "f")


def is_system_tx(tx: dict, receipt: dict, spec: ChainSpec) -> bool:
    tx_type = to_int(tx.get("type", receipt.get("type")))
    return tx_type in spec.system_tx_types


def receipt_fees(
    tx: dict, receipt: dict, block: dict, spec: ChainSpec, operator_fee: int | None = None
) -> dict:
    system = is_system_tx(tx, receipt, spec)
    family = spec.family
    missing = []

    def required(obj, key):
        if obj.get(key) is None:
            missing.append(key)
            return None
        return to_int(obj[key])

    gas = required(receipt, "gasUsed")
    base = to_int(block.get("baseFeePerGas")) if block.get("baseFeePerGas") is not None else None
    if base is None:
        if family in {"op-stack", "ethereum"}:
            missing.append("baseFeePerGas")
        else:
            base = 0  # pre-1559 style block (BSC legacy) or Nitro block without the field
    price = 0 if system else required(receipt, "effectiveGasPrice")
    l1_included = False
    if system:
        l1, operator_fee = 0, 0
    elif family == "op-stack":
        l1 = required(receipt, "l1Fee")
        if receipt.get("operatorFee") is not None:
            operator_fee = to_int(receipt["operatorFee"])
        elif not any(to_int(receipt.get(k)) for k in ("operatorFeeScalar", "operatorFeeConstant")):
            operator_fee = 0
        elif operator_fee is None:
            missing.append("operatorFee")
    elif family == "arbitrum":
        l1_gas = to_int(receipt.get("gasUsedForL1")) if receipt.get("gasUsedForL1") is not None else 0
        l1 = l1_gas * price if price is not None else None
        l1_included = True
        operator_fee = 0
    else:
        l1, operator_fee = 0, 0
    execution = gas * price if gas is not None and price is not None else None
    tip = max(price - base, 0) if price is not None and base is not None else None
    base_paid = gas * min(base, price) if None not in (gas, base, price) else None
    if execution is None or l1 is None or operator_fee is None:
        total = None
    else:
        total = execution + operator_fee + (0 if l1_included else l1)
    return {
        "is_system": system,
        "family": family,
        "gas_used": gas,
        "effective_gas_price_wei": price,
        "base_fee_per_gas_wei": base,
        "priority_fee_per_gas_wei": tip,
        "priority_fee_per_gas_gwei": units(tip, 9),
        "max_priority_fee_per_gas_wei": to_int(tx["maxPriorityFeePerGas"])
        if tx.get("maxPriorityFeePerGas") is not None
        else None,
        "max_fee_per_gas_wei": to_int(tx["maxFeePerGas"]) if tx.get("maxFeePerGas") is not None else None,
        "execution_fee_wei": execution,
        "base_fee_wei": base_paid,
        "priority_fee_wei": gas * tip if gas is not None and tip is not None else None,
        "l1_fee_wei": l1,
        "l1_fee_included_in_execution": l1_included,
        "operator_fee_wei": operator_fee,
        "timeboosted": receipt.get("timeboosted") if family == "arbitrum" else None,
        "total_fee_wei": total,
        "total_fee_eth": units(total),
        "complete": not missing,
        "missing_fields": missing,
    }
