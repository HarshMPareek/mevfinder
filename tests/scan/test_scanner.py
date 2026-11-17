import io
import json

import httpx
import pytest
from rich.console import Console

from arbkit.chain.rpc import SafeRpc
from arbkit.chain.spec import load_chain
from arbkit.cli.main import interactive
from arbkit.cli.parser import parse_blocks, parser
from arbkit.cli.reports import save_report
from arbkit.cli.terminal import show_report
from arbkit.scan.block import classify_block
from arbkit.scan.bundle import download_bundle, validate_bundle
from arbkit.scan.scanner import Scanner, summarize
from arbkit.value.fees import receipt_fees, units
from mevlab.rpc import RpcError
from tests.mevlab.test_receipt import (
    BOT,
    POOL_A,
    POOL_B,
    RESOLVER,
    USDC,
    WETH,
    make_tx,
    solidly_swap,
    transfer,
    v3_swap,
)
from tests.mevlab.test_trace import two_leg_cycle_trace

SPEC = load_chain("base")


def bundle():
    logs = [
        transfer(WETH, BOT, POOL_A, 10**18, 0),
        v3_swap(POOL_A, 10**18, -3000 * 10**6, 1),
        transfer(USDC, POOL_A, BOT, 3000 * 10**6, 2),
        transfer(USDC, BOT, POOL_B, 3000 * 10**6, 3),
        solidly_swap(POOL_B, 0, 3000 * 10**6, 101 * 10**16, 0, 4),
        transfer(WETH, POOL_B, BOT, 101 * 10**16, 5),
    ]
    tx, receipt = make_tx(logs)
    for obj in (tx, receipt):
        obj["blockHash"] = "0x" + "a1" * 32
        obj["transactionIndex"] = "0x0"
    tx["maxPriorityFeePerGas"] = hex(900_000)
    block = {
        "number": "0x10",
        "hash": tx["blockHash"],
        "timestamp": "0x10",
        "baseFeePerGas": hex(1_000_000),
        "transactions": [tx],
    }
    return block, [receipt]


@pytest.mark.parametrize(
    "parts,expected",
    [
        (["7"], [7]),
        (["7", "9"], [7, 8, 9]),
        (["7-9,11"], [7, 8, 9, 11]),
        (["9,7,7"], [7, 9]),
        (["latest"], ["latest"]),
    ],
)
def test_block_inputs(parts, expected):
    assert parse_blocks(parts) == expected


@pytest.mark.parametrize("parts", [["9", "7"], ["-1"], ["a"], ["1-999999"], [""]])
def test_invalid_inputs(parts):
    with pytest.raises(ValueError):
        parse_blocks(parts)


def test_exact_fees_paid_tip_vs_cap_and_l1():
    block, receipts = bundle()
    fees = receipt_fees(block["transactions"][0], receipts[0], block, SPEC)
    assert fees["priority_fee_per_gas_wei"] == 500_000
    assert fees["max_priority_fee_per_gas_wei"] == 900_000
    assert fees["total_fee_wei"] == 2_225_000_000_000
    assert fees["total_fee_wei"] == fees["priority_fee_wei"] + fees["base_fee_wei"] + fees["l1_fee_wei"]
    assert units(1) == "0.000000000000000001"
    assert units(2**100).replace(".", "").rstrip("0")


def test_unknown_l1_not_silently_zero_and_deposits_free():
    block, receipts = bundle()
    receipts[0].pop("l1Fee")
    fees = receipt_fees(block["transactions"][0], receipts[0], block, SPEC)
    assert fees["total_fee_wei"] is None and "l1Fee" in fees["missing_fields"]
    block["transactions"][0]["type"] = "0x7e"
    fees = receipt_fees(block["transactions"][0], receipts[0], block, SPEC)
    assert fees["total_fee_wei"] == 0 and fees["base_fee_wei"] == 0


def test_operator_fee_is_accounted_or_unknown():
    block, receipts = bundle()
    receipts[0]["operatorFeeScalar"] = "0x5"
    fees = receipt_fees(block["transactions"][0], receipts[0], block, SPEC)
    assert not fees["complete"] and fees["total_fee_wei"] is None
    fees = receipt_fees(block["transactions"][0], receipts[0], block, SPEC, 13)
    assert fees["total_fee_wei"] == 2_225_000_000_013


def test_missing_duplicate_and_reorg_receipts_rejected():
    block, receipts = bundle()
    with pytest.raises(ValueError, match="incomplete"):
        validate_bundle(block, [], 16)
    with pytest.raises(ValueError, match="duplicate"):
        validate_bundle(block, receipts * 2, 16)
    receipts[0]["blockHash"] = "0xwrong"
    with pytest.raises(ValueError, match="another block"):
        validate_bundle(block, receipts, 16)


async def test_full_report_exports_full_hash_and_exact_wei(tmp_path):
    block, receipts = bundle()
    rows = await classify_block(SPEC, block, receipts, RESOLVER, trace_mode="none")
    assert rows[0]["assessment"] == "event_candidate"
    report = {
        "requested_blocks": [16],
        "classifier_version": "test",
        "blocks": [{"number": 16}],
        "rows": rows,
        "failures": [],
        "summary": summarize(rows),
    }
    path = save_report(report, tmp_path)
    saved = json.loads(path.read_text())
    assert saved["rows"][0]["fees"]["total_fee_wei"] == 2_225_000_000_000
    assert rows[0]["tx_hash"] in path.with_suffix(".csv").read_text()
    output = io.StringIO()
    show_report(report, Console(file=output, width=80, color_system=None))
    assert rows[0]["tx_hash"] in output.getvalue()
    assert rows[0]["explorer_url"] in output.getvalue()
    assert "event candidate" in output.getvalue()


async def test_trace_unavailable_keeps_candidate():
    class NoTraces:
        async def call(self, *args):
            raise RpcError(-32601, "unavailable")

    block, receipts = bundle()
    rows = await classify_block(SPEC, block, receipts, RESOLVER, None, NoTraces())
    assert rows[0]["assessment"] == "event_candidate"
    assert rows[0]["trace"]["status"] == "unavailable"


async def test_trace_status_disagreement_not_reported_as_verified():
    class WrongTrace:
        async def call(self, *args):
            trace = two_leg_cycle_trace()
            trace["error"] = "execution reverted"
            return trace

    block, receipts = bundle()
    rows = await classify_block(SPEC, block, receipts, RESOLVER, None, WrongTrace())
    assert rows[0]["assessment"] == "event_candidate"
    assert rows[0]["trace"]["status"] == "unavailable"


async def test_batch_receipt_fallback():
    block, receipts = bundle()

    class FakeRpc:
        async def get_block(self, *args):
            return block

        async def call(self, method, params):
            raise RpcError(-32601, "method unavailable")

        async def batch(self, requests):
            assert requests[0][0] == "eth_getTransactionReceipt"
            return receipts

    got = await download_bundle(FakeRpc(), 16)
    assert got == (block, receipts)


async def test_rpc_error_redacts_authentication_url():
    secret = "SECRET_RPC_TOKEN"
    rpc = SafeRpc("https://example.invalid/" + secret)
    await rpc._client.aclose()
    rpc._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(403, text=secret))
    )
    rpc.retries = 0
    try:
        with pytest.raises(RpcError) as caught:
            await rpc.chain_id()
        assert secret not in str(caught.value)
    finally:
        await rpc.aclose()


def test_interactive_block_range_and_quit(monkeypatch):
    answers = iter(["16", "18", "q"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    commands = []
    monkeypatch.setattr("arbkit.cli.main.execute", lambda args, console: commands.append(args))
    assert interactive(Console(file=io.StringIO())) == 0
    assert parse_blocks(commands[0].blocks) == [16, 17, 18]


async def test_token_callee_transfer_not_probe():
    from mevlab.receipt.classify import classify_tx

    tx, receipt = make_tx([transfer(BOT, BOT, POOL_A, 5, 0)])
    label = await classify_tx(tx, receipt, RESOLVER, base_fee_per_gas=1, weth=WETH)
    assert label.arb_class == "no_swaps"


async def test_plain_transfer_is_not_a_probe_but_quiet_contract_call_is():
    from mevlab.receipt.classify import classify_tx

    tx, receipt = make_tx([])
    receipt["gasUsed"] = hex(21_000)
    plain = await classify_tx(tx, receipt, RESOLVER, base_fee_per_gas=1, weth=WETH)
    assert plain.arb_class == "no_swaps"
    receipt["gasUsed"] = hex(30_000)
    quiet = await classify_tx(tx, receipt, RESOLVER, base_fee_per_gas=1, weth=WETH)
    assert quiet.arb_class == "probe" and not quiet.is_arb


async def test_base_l1_attributes_deposit_does_not_inflate_probe_counts():
    from mevlab.receipt.classify import classify_tx

    tx, receipt = make_tx([])
    tx.update(type="0x7e", to="0x4200000000000000000000000000000000000015")
    receipt["gasUsed"] = hex(46_913)
    label = await classify_tx(tx, receipt, RESOLVER, base_fee_per_gas=1, weth=WETH)
    assert label.arb_class == "system_deposit"
    assert not label.is_arb
    assert "op_stack_deposit_type_126" in label.reasons


async def test_class_filter_shows_probes_and_their_fee_totals():
    block, receipts = bundle()
    rows = await classify_block(SPEC, block, receipts, RESOLVER, trace_mode="none")
    probe = {
        **rows[0],
        "tx_hash": "0x" + "b2" * 32,
        "arb_class": "probe",
        "assessment": "not_arb",
        "profit_token": None,
        "is_arb": False,
    }
    probe["explorer_url"] = "https://basescan.org/tx/" + probe["tx_hash"]
    report = {
        "classifier_version": "test",
        "blocks": [16],
        "rows": [*rows, probe],
        "summary": summarize([*rows, probe]),
        "failures": [],
    }
    args = parser().parse_args(["show", "--class", "probe", "--class", "reverted"])
    output = io.StringIO()
    show_report(report, Console(file=output, width=160, color_system=None), classes=args.classes)
    text = output.getvalue()
    assert probe["tx_hash"] in text and rows[0]["tx_hash"] not in text
    assert "1 transactions" in text
    assert "network fees 0.000002225 ETH" in text
    assert "does not prove simulation spam" in text


async def test_trace_recovers_cycle_without_swap_events():
    block, receipts = bundle()
    tx = block["transactions"][0]
    receipts[0]["logs"] = [
        log
        for log in receipts[0]["logs"]
        if len(log["topics"]) == 3
        and log["topics"][0] == "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    ]

    def frame(sender, receiver, children=None, data="0x"):
        return {
            "type": "CALL",
            "from": sender,
            "to": receiver,
            "input": data,
            "value": "0x0",
            "calls": children or [],
        }

    def send(currency, sender, recipient, amount):
        data = "0xa9059cbb" + f"{int(recipient, 16):064x}{amount:064x}"
        return frame(sender, currency, data=data)

    trace = frame(
        tx["from"],
        BOT,
        [
            send(WETH, BOT, POOL_A, 10**18),
            frame(BOT, POOL_A, [send(USDC, POOL_A, BOT, 3000 * 10**6)]),
            send(USDC, BOT, POOL_B, 3000 * 10**6),
            frame(BOT, POOL_B, [send(WETH, POOL_B, BOT, 101 * 10**16)]),
        ],
    )

    class Traces:
        async def call(self, *args):
            return trace

    rows = await classify_block(SPEC, block, receipts, RESOLVER, None, Traces(), trace_mode="all")
    row = rows[0]
    # The trace still closes the loop and keeps its money for review, but with no Swap event in the receipt it is not
    # counted: the promotion rule (a decoded swap is required; withdrawals had closed as cycles).
    assert row["assessment"] == "needs_review" and not row["is_arb"]
    assert "no_decoded_swap" in row["review_reasons"]
    assert not row["event_label"]["is_arb"]
    assert row["gross_profit_raw"] == str(10**16)
    assert row["n_legs"] == 2


async def test_wrong_chain_rejected_before_any_download(monkeypatch, tmp_path):
    from arbkit.chain.settings import Settings

    class WrongChain:
        def __init__(self, url):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def chain_id(self):
            return 4663

    monkeypatch.setattr("arbkit.scan.scanner.SafeRpc", WrongChain)
    with pytest.raises(ValueError, match="not base"):
        await Scanner(Settings(SPEC, "http://test", "http://test", tmp_path, tmp_path)).run([16])
