"""Token side effects (transfer taxes, dividend shares) are never the operator's flows, and a counted gross must be
reproduced from the raw receipts (BSC 131010461 #5, BTDAO, 2026-11-01)."""

from arbkit.chain.spec import load_chain
from arbkit.scan.block import require_conserved_gross
from arbkit.verify.audit import cash_check
from mevlab.trace.movements import movements
from mevlab.trace.verdict import trace_verdict

WETH = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
TAX = "0xff00e4b2d1db5c8fafd4daf666bf688dabf873ba"  # a fee-on-transfer token (BTDAO)
BOT_EOA, BOT = "0x5dc7dc5b605d7cf11a27fa1f33526daf649448d8", "0xb585639ff3d61de1187d960cc18924d033f2768f"
POOL_A, POOL_B = "0xd6b5f8d543c6d4000dfca50bedafb267ede016f4", "0xf1c179c381c1a4d3ff1c48ffe427e73218f6b1db"
DISTRIBUTOR, DEAD = "0x75d5f37c22eab32f3765c117e19326c6efce0df9", "0x000000000000000000000000000000000000dead"
HOLDERS = ["0x" + f"{i:040x}" for i in range(1, 4)]
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def word(x) -> str:
    return f"{x:064x}" if isinstance(x, int) else x[2:].rjust(64, "0")


def call(frm, to, sel, *args, calls=None):
    return {
        "type": "CALL",
        "from": frm,
        "to": to,
        "input": sel + "".join(word(a) for a in args),
        "value": "0x0",
        "calls": calls or [],
    }


def log(token, a, b, amount):
    return {
        "address": token,
        "topics": [TRANSFER, "0x" + word(a), "0x" + word(b)],
        "data": "0x" + word(amount),
    }


def taxed_cycle():
    """Pool A sells 1,000 TAX for 100 WETH; the bot sends 1,000 TAX to pool B, whose token takes 2 % (1 % burned,
    1 % to its distributor, which pays dividends from its balance) and pool B pays 101 WETH: +1 WETH."""
    trace = call(
        BOT_EOA,
        BOT,
        "0x81dbb6c4",
        calls=[
            call(BOT, POOL_A, "0x022c0d9f", 0, 1000, BOT, calls=[call(POOL_A, TAX, "0xa9059cbb", BOT, 1000)]),
            call(BOT, TAX, "0xa9059cbb", POOL_B, 1000),
            call(BOT, POOL_B, "0x022c0d9f", 101, 0, BOT, calls=[call(POOL_B, WETH, "0xa9059cbb", BOT, 101)]),
            call(BOT, WETH, "0xa9059cbb", POOL_A, 100),
        ],
    )
    logs = [
        log(TAX, POOL_A, BOT, 1000),
        log(TAX, BOT, DEAD, 10),
        log(TAX, BOT, DISTRIBUTOR, 10),
        log(TAX, BOT, POOL_B, 980),
        *[log(TAX, DISTRIBUTOR, h, 50) for h in HOLDERS],
        log(WETH, POOL_B, BOT, 101),
        log(WETH, BOT, POOL_A, 100),
    ]
    return trace, logs


def test_tax_cuts_are_side_effects_not_directed_flows():
    trace, logs = taxed_cycle()
    movs, _ = movements(trace, WETH, logs)
    kinds = {(m.receiver, m.kind) for m in movs if m.token == TAX and m.sender == BOT}
    assert (POOL_B, "receipt_transfer") in kinds
    assert (DISTRIBUTOR, "receipt_side_effect") in kinds and (DEAD, "receipt_side_effect") in kinds


def test_tax_distributor_never_joins_the_operator_and_the_tax_is_never_profit():
    trace, logs = taxed_cycle()
    tv = trace_verdict(trace, WETH, BOT_EOA, BOT, logs=logs)
    assert tv.verdict == "cycle"
    assert DISTRIBUTOR not in tv.group
    assert tv.profit_token == WETH and tv.gross == 1
    assert TAX not in tv.extra_gains


def test_counted_gross_must_be_reproduced_from_receipts():
    spec = load_chain("bsc")
    trace, logs = taxed_cycle()
    receipt = {"logs": logs, "status": "0x1"}
    tv = trace_verdict(trace, WETH, BOT_EOA, BOT, logs=logs)
    row = {
        "assessment": "trace_supported",
        "is_arb": True,
        "gross_source": "trace",
        "profit_token": WETH,
        "gross_profit_raw": str(tv.gross),
        "reasons": [],
        "trace": tv.to_json(),
    }
    assert cash_check(row, receipt, trace, WETH)["gross_matches"]
    require_conserved_gross(spec, row, trace, receipt)
    assert row["assessment"] == "trace_supported"
    # the pre-fix reading: the distributor folded into the operator, its 1 % cut taken as the profit
    bad = {
        **row,
        "profit_token": TAX,
        "gross_profit_raw": "10",
        "reasons": [],
        "trace": {**row["trace"], "group": [BOT_EOA, BOT, DISTRIBUTOR]},
    }
    require_conserved_gross(spec, bad, trace, receipt)
    assert (
        bad["assessment"] == "needs_review" and "gross_not_reproduced_from_receipts" in bad["review_reasons"]
    )


def test_an_extra_gain_the_receipts_do_not_reproduce_never_counts():
    spec = load_chain("bsc")
    trace, logs = taxed_cycle()
    receipt = {"logs": logs, "status": "0x1"}
    tv = trace_verdict(trace, WETH, BOT_EOA, BOT, logs=logs)
    row = {
        "assessment": "trace_supported",
        "is_arb": True,
        "gross_source": "trace",
        "profit_token": WETH,
        "gross_profit_raw": str(tv.gross),
        "reasons": [],
        "trace": {**tv.to_json(), "extra_gains": {TAX: "20"}},
    }
    require_conserved_gross(spec, row, trace, receipt)
    assert row["assessment"] == "trace_supported"  # the WETH gain is real
    assert (
        row["trace"]["extra_gains"] == {}
    )  # the 20 TAX "gain" (the tax) is not, so pricing can never use it
    assert any(r.startswith("extra_gain_not_reproduced:") for r in row["reasons"])
