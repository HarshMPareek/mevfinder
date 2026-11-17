"""Receipt-backed ERC-4337 outcomes; layout from v0.7 IEntryPoint.sol.

Only logs emitted by the transaction's known v0.6/v0.7 EntryPoint are accepted.
"""

ENTRYPOINTS = {"0x5ff137d4b0fdcd49dca30c7cf57e578a026d2789", "0x0000000071727de22e5e9d8baf0edac6f37da032"}
USER_OPERATION = "0x49628fd1471006c1482da88028e9ce4dbb080b815c9b0344d39e5a8e6ec1419f"


def operation_outcomes(tx, receipt):
    entrypoint = (tx.get("to") or "").lower()
    if entrypoint not in ENTRYPOINTS:
        return []
    result = []
    for log in receipt.get("logs", []):
        topics, data = log.get("topics", []), log.get("data", "")
        if (
            log.get("address", "").lower() != entrypoint
            or len(topics) != 4
            or topics[0].lower() != USER_OPERATION
            or len(data) != 258
        ):
            continue
        words = [int(data[i : i + 64], 16) for i in range(2, len(data), 64)]
        if words[1] not in {0, 1}:
            continue
        result.append(
            {
                "entrypoint": entrypoint,
                "user_op_hash": topics[1].lower(),
                "sender": "0x" + topics[2][-40:].lower(),
                "paymaster": "0x" + topics[3][-40:].lower(),
                "nonce": str(words[0]),
                "success": bool(words[1]),
                "actual_gas_cost_wei": str(words[2]),
                "actual_gas_used": str(words[3]),
            }
        )
    return result


def annotate_operations(row, tx, receipt):
    operations = operation_outcomes(tx, receipt)
    row["user_operations"] = operations
    if (
        operations
        and row["assessment"] == "not_arb"
        and row["arb_class"] in {"probe", "no_swaps"}
        and all(not op["success"] for op in operations)
    ):
        row["arb_class"] = "userop_failure"
        row["reasons"] = [*row["reasons"], "all_reported_user_operations_failed"]
