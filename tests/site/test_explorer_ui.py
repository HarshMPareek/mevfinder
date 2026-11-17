"""Explorer pages: search parsing, block-at-time lookup, on-demand jobs, transaction evidence and block proofs."""

import asyncio
import gzip
import json

import pytest

from arbkit.chain.spec import load_chain
from arbkit.site.jobs import MAX_BLOCKS_PER_JOB, block_at, parse_blocks_text, parse_time
from arbkit.verify.evidence import block_proof, token_transfers, transaction_evidence
from mevlab.decode.signatures import WETH_DEPOSIT_TOPIC
from tests.mevlab.test_receipt import BOT, EOA, POOL_A, USDC, WETH, t, transfer

SPEC = load_chain("base")


def test_block_text_parsing_accepts_numbers_ranges_and_lists_and_caps_the_size():
    assert parse_blocks_text("123") == [123]
    assert parse_blocks_text("100-103") == [100, 101, 102, 103]
    assert parse_blocks_text("100 103") == [100, 101, 102, 103]
    assert parse_blocks_text("5,3,3") == [3, 5]
    with pytest.raises(ValueError):
        parse_blocks_text(f"1-{MAX_BLOCKS_PER_JOB + 5}")
    with pytest.raises(ValueError):
        parse_blocks_text("10-5")
    with pytest.raises(ValueError):
        parse_blocks_text("0xabc")


def test_time_parsing_accepts_dates_times_and_unix_seconds():
    assert parse_time("1759795200") == 1759795200
    assert parse_time("2026-10-07") == 1791331200
    assert parse_time("2026-10-07 12:34") == 1791331200 + 12 * 3600 + 34 * 60
    assert parse_time("2026-10-07T12:34:56Z") == 1791331200 + 12 * 3600 + 34 * 60 + 56
    with pytest.raises(ValueError):
        parse_time("yesterday")


class FakeRpc:
    """Blocks every 2 seconds from timestamp 1000 at block 1; head at 1000."""

    def __init__(self, head=1000):
        self.head = head
        self.calls = 0

    async def block_number(self):
        return self.head

    async def get_block(self, n, full=False):
        self.calls += 1
        return {"number": hex(n), "timestamp": hex(1000 + 2 * (n - 1)), "hash": "0x" + f"{n:064x}"}


def test_block_at_time_is_the_first_block_at_or_after_the_timestamp():
    rpc = FakeRpc()
    found = asyncio.run(block_at(rpc, 1000 + 2 * 499))  # exactly block 500's timestamp
    assert found["number"] == 500 and found["timestamp"] == 1998
    found = asyncio.run(block_at(rpc, 1000 + 2 * 499 + 1))  # one second later -> block 501
    assert found["number"] == 501
    assert rpc.calls < 60  # binary search, not a walk
    after = asyncio.run(block_at(rpc, 10**9))
    assert after["number"] == 1000 and after["note"]


def _bundle(tmp_path, number, hash_, receipts, txs):
    data = tmp_path / "data" / "base"
    (data / "blocks").mkdir(parents=True)
    (data / "traces").mkdir()
    block = {
        "number": hex(number),
        "hash": hash_,
        "parentHash": "0x" + "ab" * 32,
        "timestamp": hex(1_700_000_000),
        "miner": "0x" + "42" * 20,
        "baseFeePerGas": hex(10**6),
        "gasUsed": hex(21_000),
        "gasLimit": hex(30_000_000),
        "extraData": "0x",
        "transactions": txs,
    }
    with gzip.open(data / "blocks" / f"{number}.json.gz", "wt") as f:
        json.dump({"chain_id": 8453, "block": block, "receipts": receipts}, f)
    return data


def test_token_transfers_decode_erc20_and_weth_wraps_with_symbols():
    receipt = {
        "logs": [
            transfer(USDC, EOA, POOL_A, 3000 * 10**6, 0),
            transfer(WETH, POOL_A, EOA, 10**18, 1),
            {
                "address": WETH,
                "logIndex": "0x2",
                "topics": [WETH_DEPOSIT_TOPIC, t(BOT)],
                "data": "0x" + (5 * 10**17).to_bytes(32, "big").hex(),
            },
        ]
    }
    symbols = {t_.address: t_.symbol for t_ in SPEC.tokens}
    decimals = {t_.address: t_.decimals for t_ in SPEC.tokens}
    out = token_transfers(receipt, SPEC.weth, symbols, decimals)
    assert [x["kind"] for x in out] == ["transfer", "transfer", "wrap"]
    assert (
        out[0]["symbol"] == "USDC"
        and out[0]["amount"] == "3000"
        and out[0]["from"] == EOA
        and out[0]["to"] == POOL_A
    )
    assert out[1]["symbol"] == "WETH" and out[1]["amount"] == "1"
    assert out[2]["to"] == BOT and out[2]["amount"] == "0.5"


def test_transaction_evidence_and_block_proof_read_the_cached_files(tmp_path):
    tx_hash = "0x" + "01" * 32
    block_hash = "0x" + "bb" * 32
    txs = [
        {
            "hash": tx_hash,
            "from": EOA,
            "to": BOT,
            "value": hex(0),
            "nonce": "0x5",
            "input": "0x12345678aa",
            "type": "0x2",
            "transactionIndex": "0x0",
        }
    ]
    receipts = [
        {
            "transactionHash": tx_hash,
            "status": "0x1",
            "gasUsed": hex(150_000),
            "logs": [transfer(WETH, BOT, POOL_A, 10**18, 0), transfer(USDC, POOL_A, BOT, 3000 * 10**6, 1)],
        }
    ]
    data = _bundle(tmp_path, 77, block_hash, receipts, txs)
    trace = {
        "type": "CALL",
        "from": EOA,
        "to": BOT,
        "value": "0x0",
        "input": "0x12345678",
        "calls": [
            {
                "type": "CALL",
                "from": BOT,
                "to": "0x" + "42" * 20,
                "value": hex(10**15),
                "input": "0x",
                "calls": [],
            }
        ],
    }
    with gzip.open(data / "traces" / f"{block_hash}_{tx_hash}.json.gz", "wt") as f:
        json.dump(trace, f)
    row = {
        "tx_hash": tx_hash,
        "block_number": 77,
        "tx_index": 0,
        "tx_from": EOA,
        "tx_to": BOT,
        "assessment": "trace_supported",
        "arb_class": "atomic_cycle",
        "mev_type": "atomic_mid_block",
        "profit_token": WETH,
        "profit_symbol": "WETH",
        "profit_decimals": 18,
        "gross_profit": "0.01",
        "gross_profit_raw": str(10**16),
        "fees": {"total_fee_wei": 10**14},
        "trace": {"verdict": "cycle", "group": [BOT], "venues": [POOL_A]},
        "reasons": [],
        "explorer_url": "https://basescan.org/tx/" + tx_hash,
    }
    ev = transaction_evidence(SPEC, data, row, block_hash)
    assert ev["status"] == 1 and ev["gas_used"] == 150_000 and ev["logs"] == 2
    assert [x["symbol"] for x in ev["token_transfers"]] == ["WETH", "USDC"]
    assert ev["native_calls"] == [
        {"from": BOT, "to": "0x" + "42" * 20, "value_wei": str(10**15), "value": "0.001"}
    ]
    assert ev["trace_root"]["calls"] == 2 and ev["block_file_sha256"] and ev["trace_file_sha256"]
    assert ev["block"]["hash"] == block_hash and ev["input_selector"] == "0x12345678" and ev["nonce"] == 5
    proof = block_proof(
        SPEC,
        data,
        77,
        {"number": 77, "hash": block_hash, "timestamp": 1_700_000_000, "builder_tag": None},
        [
            row,
            {
                **row,
                "tx_hash": "0x" + "02" * 32,
                "tx_index": 1,
                "assessment": "not_arb",
                "arb_class": "single_swap",
                "mev_type": "none",
            },
        ],
    )
    assert proof["block"]["hash"] == block_hash and proof["rows_total"] == 2 and proof["rows_labelled"] == 1
    assert proof["rows"][0]["evidence"]["token_transfers"][1]["amount"] == "3000"
    assert proof["rows"][0]["verdict"]["trace"] == "cycle" and proof["block_file_sha256"]
    assert any("scan 77" in line for line in proof["how_to_verify"])
