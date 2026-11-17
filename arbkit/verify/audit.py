"""Independent reconciliation of a report against the raw cached blocks and traces.

Nothing here calls the classifier's decoders or verdict functions. For every transaction the network fee is
recomputed from the raw receipt with the chain family's formula; for every trace-supported cycle the operator
group's net per token is rebuilt from raw Transfer logs, WETH deposit/withdraw, successful native calls and
ERC-6909 claim logs, the payouts the report names are added back, and the result is compared with the
classifier's gross to the unit. The group and payees are taken from the report (this checks accounting, not
ownership or recall).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from arbkit.chain.spec import ChainSpec
from arbkit.files import read_json

ZERO = "0x" + "0" * 40
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
DEPOSIT = "0xe1fffcc4923d04b559f4d29a8bfc6cda04eb5b0d3c460751c2402c5c5cc9109c"
WITHDRAWAL = "0x7fcf532c15f0a6db0bd6d0e038bea71d30d808c7d98cb3bf7268a95bf5081b65"
CLAIM = "0x1b3d7edb2e9c0b0e7c525b20aaaef0f5940d2ed71663c7d39266ecafac728859"


def num(x):
    return int(x, 16) if isinstance(x, str) and x.startswith("0x") else int(x or 0)


def _addr(word):
    return "0x" + word[-40:].lower()


def native_calls(trace):
    out = []

    def walk(frame):
        if frame.get("error") or frame.get("revertReason"):
            return
        if frame.get("type") in {"CALL", "CREATE", "CREATE2", "SELFDESTRUCT"} and num(frame.get("value")):
            out.append(("ETH", frame["from"].lower(), (frame.get("to") or ZERO).lower(), num(frame["value"])))
        for child in frame.get("calls", []):
            walk(child)

    walk(trace)
    return out


def movements(receipt, trace, weth):
    cash, claims = [], []
    for log in receipt["logs"]:
        topics = log.get("topics", [])
        words = [log["data"][i : i + 64] for i in range(2, len(log["data"]), 64)]
        token = log["address"].lower()
        if topics and topics[0] == TRANSFER and len(topics) == 3 and len(words) == 1:
            # A proxy WETH mint/burn (Robinhood) is the WETH side of a wrap; the native side is the value call
            # already present in the trace, so no synthetic leg is added here.
            cash.append((token, _addr(topics[1]), _addr(topics[2]), int(words[0], 16)))
        elif token == weth and len(topics) == 2 and words:
            if topics[0] == DEPOSIT:
                cash.append((weth, ZERO, _addr(topics[1]), int(words[0], 16)))
            elif topics[0] == WITHDRAWAL:
                cash.append((weth, _addr(topics[1]), ZERO, int(words[0], 16)))
        elif topics and topics[0] == CLAIM and len(topics) == 4 and len(words) >= 2:
            claims.append((_addr(topics[3]), _addr(topics[1]), _addr(topics[2]), int(words[1], 16)))
    return with_natives(cash, native_calls(trace), weth), claims


def with_natives(cash, natives, weth):
    """Append native value movements to the token movements, attributing proxy-WETH `withdrawTo` payments."""
    cash = list(cash)
    # `withdrawTo` on a proxy WETH: X burns WETH and the contract pays Y. Attribute the native payment to X and
    # drop that burn (the native leg now carries the unwrap), so X is charged once.
    burns: dict[int, list[int]] = defaultdict(list)
    for index, (token, frm, to, amount) in enumerate(cash):
        if token == weth and to == ZERO:
            burns[amount].append(index)
    consumed: set[int] = set()
    for token, frm, to, amount in natives:
        if frm == weth and burns.get(amount):
            index = next((i for i in burns[amount] if cash[i][1] != to), None)
            if index is not None:
                burns[amount].remove(index)
                consumed.add(index)
                frm = cash[index][1]
        cash.append((token, frm, to, amount))
    return [m for i, m in enumerate(cash) if i not in consumed]


def nets(moves, group, weth):
    net = defaultdict(int)
    for token, frm, to, amount in moves:
        token = weth if token in {"ETH", ZERO} else token
        if frm in group:
            net[token] -= amount
        if to in group:
            net[token] += amount
    return {k: v for k, v in net.items() if v}


def cash_check(row, receipt, trace, weth):
    cash, claims = movements(receipt, trace, weth)
    tv = row["trace"]
    group = set(tv.get("group", []))
    payees = {t: set(by) for t, by in (tv.get("payouts_by_token") or {}).items()}
    for t, who in (tv.get("payout_recipients") or {}).items():
        payees.setdefault(t, set()).update(who)
    net = nets(cash, group, weth)
    claim_net = nets(claims, group, weth)
    payments = defaultdict(int)
    for token, by_recipient in payees.items():
        for recipient in by_recipient:
            paid = sum(
                value * ((frm in group and to == recipient) - (to in group and frm == recipient))
                for tok, frm, to, value in cash
                if (weth if tok == "ETH" else tok) == token
            )
            payments[token] += paid
    token = row.get("profit_token")
    gross = net.get(token, 0) + payments.get(token, 0) + claim_net.get(token, 0) if token else None
    portfolio = dict(net)
    for asset, amount in payments.items():
        portfolio[asset] = portfolio.get(asset, 0) + amount
    for asset, amount in claim_net.items():
        portfolio[asset] = portfolio.get(asset, 0) + amount
    losses = {asset: amount for asset, amount in portfolio.items() if amount < 0}
    return {
        "gross_from_receipts": gross,
        "classifier_gross": row.get("gross_profit_raw"),
        "gross_matches": gross == num(row.get("gross_profit_raw")) if token else None,
        "open_losses": losses,
        "group": sorted(group),
    }


def expected_fee(spec: ChainSpec, tx, receipt, block):
    tx_type = num(tx.get("type", receipt.get("type")))
    if tx_type in spec.system_tx_types:
        return 0, 0
    gas, price = num(receipt["gasUsed"]), num(receipt["effectiveGasPrice"])
    base = num(block.get("baseFeePerGas")) if block.get("baseFeePerGas") is not None else 0
    total = gas * price
    if spec.family == "op-stack":
        total += num(receipt.get("l1Fee"))
        if receipt.get("operatorFee") is not None:
            total += num(receipt["operatorFee"])
        elif num(receipt.get("operatorFeeScalar")) or num(receipt.get("operatorFeeConstant")):
            return None, None  # needs the historical GasPriceOracle; left to the classifier's RPC call
    return total, gas * max(price - base, 0)


def audit(spec: ChainSpec, report: dict, data_dir: Path) -> dict:
    fee_failures, gross_mismatches, open_losses, missing = [], [], [], []
    checked = Counter()
    for info in report["blocks"]:
        path = data_dir / "blocks" / f"{info['number']}.json.gz"
        if not path.exists():
            missing.append(info["number"])
            continue
        bundle = read_json(path)
        block = bundle["block"]
        receipts = {r["transactionHash"].lower(): r for r in bundle["receipts"]}
        txs = {t["hash"].lower(): t for t in block["transactions"]}
        for row in (r for r in report["rows"] if r["block_number"] == info["number"]):
            tx, receipt = txs[row["tx_hash"].lower()], receipts[row["tx_hash"].lower()]
            total, priority = expected_fee(spec, tx, receipt, block)
            checked["fees"] += 1
            if total is not None and (
                total != row["fees"]["total_fee_wei"] or priority != row["fees"]["priority_fee_wei"]
            ):
                fee_failures.append(
                    {
                        "tx_hash": row["tx_hash"],
                        "expected_total": total,
                        "reported": row["fees"]["total_fee_wei"],
                        "expected_priority": priority,
                        "reported_priority": row["fees"]["priority_fee_wei"],
                    }
                )
            if (
                row["assessment"] in {"trace_supported", "trace_supported_loss"}
                and row.get("gross_source") == "trace"
            ):
                trace_path = data_dir / "traces" / f"{block['hash']}_{row['tx_hash']}.json.gz"
                if not trace_path.exists():
                    missing.append(row["tx_hash"])
                    continue
                check = cash_check(row, receipt, read_json(trace_path), spec.weth)
                checked["cycles"] += 1
                if not check["gross_matches"]:
                    gross_mismatches.append({"tx_hash": row["tx_hash"], **check})
                if row["assessment"] == "trace_supported" and check["open_losses"]:
                    open_losses.append({"tx_hash": row["tx_hash"], "open_losses": check["open_losses"]})
    return {
        "chain": spec.name,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "report": report.get("created_at"),
        "transactions_fee_checked": checked["fees"],
        "fee_failures": fee_failures,
        "cycles_checked": checked["cycles"],
        "gross_mismatches": gross_mismatches,
        "positive_cycles_with_open_losses": open_losses,
        "missing_cached_data": missing,
        "scope": "fees and cycle amounts recomputed from raw receipts, native calls and claims; group and payees from the report",
    }
