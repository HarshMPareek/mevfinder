"""Explorer page parsers and the reconciliation rules (offline)."""

from decimal import Decimal

from arbkit.verify.explorer import (
    _abbrev_match,
    _amount_text,
    parse_blockscout,
    parse_bsctrace_text,
    parse_etherscan_text,
    trace_natives,
)

ZERO = "0x" + "0" * 40

ARBISCAN = """Status:
Success
Block:
512976175 458 L1 Block Confirmations
From:
0x97E75Ffa3e05Ace045BBE58691B40B8634627D6d
Interacted With (To):
0x000642A0966d9bd49870D9519f76b5cf823f3000
Internal Transactions:
All Transfers
Net Transfers
Transfer
0.020268786568941748 ETH
($50.05)
From
Wrapped Ether
To
0xC10eE903...357910fb4
Transfer
0.000016357 ETH
($0.04)
From
0x000642A0...f823f3000
To
0x000E474c...9f4b009A5
ERC-20 Tokens Transferred: 2
All Transfers
Net Transfers
From
dev-orbs.eth
To
0x000642A0...f823f3000
For
49.712887
$49.69
USD Coin (USDC)
From
0xC10eE903...357910fb4
To
Null: 0x000…000
For
0.020268786568941748
$50.07
Wrapped Ethe... (WETH)
Value:
0 ETH
($0.00)
Transaction Fee:
0.000010849947159793 ETH ($0.03)
"""

BSCTRACE = """Transaction Details
Overview
Txn Hash
0x6cd4c64edced9aeb922398104d23a19d5b8fbc3470744766b250a1a23e02bf3c
Status
Success
Block
126394384
(117505 Block Confirmations)
Method
0x201e2a16
From
0xbf26b0fd8eeed416947fe45048b6968a31ab5950
Interacted With (To)
Contract
0xb03f42644ed3804b45af4b3264a89bcd93d01ab7
Transfer
0.136549699 BNB
From
0xbb4cdb9cbd36b01bd1cba...
To
0xb03f...d01ab7
Transfer
754.294718702 Gwei BNB
From
0x9999...940515
To
0x9997...5c574f
Tokens Transferred
From
0x238a...d5e6c4
To
0xb03f...d01ab7
For
105.117498057118695424
USDT
From
0xb03f...d01ab7
To
0x650d...d0d1b7
For
0.000014248460035563
WBNB
Value
0 BNB
($0.0000)
Txn Fee
0.00001346455 BNB ($0.009880)
Gas Limit
403,201
"""


def test_abbreviations_and_labels():
    full = "0xc10ee903" + "a" * 23 + "357910fb4"
    assert _abbrev_match("0xC10eE903...357910fb4", full)
    assert not _abbrev_match("0xC10eE903...357910fb5", full)
    assert _abbrev_match("0xbb4cdb9cbd36b01bd1cba...", "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c")
    assert _abbrev_match("Null: 0x000…000", ZERO)
    assert not _abbrev_match("Wrapped Ether", "0x4200000000000000000000000000000000000006")


def test_amount_text_handles_units_and_gwei():
    assert _amount_text("0.0260 ETH") == ("0.0260", False)
    assert _amount_text("754.294718702 Gwei BNB") == ("0.000000754294718702", False)
    assert _amount_text("1,234.5 USDC…") == ("1234.5", True)


def test_parse_etherscan_page():
    page = parse_etherscan_text(ARBISCAN)
    assert page["source"] == "etherscan"
    assert page["status"] == "Success" and page["block"] == 512976175
    assert page["from"] == "0x97e75ffa3e05ace045bbe58691b40b8634627d6d"
    assert page["to"] == "0x000642a0966d9bd49870d9519f76b5cf823f3000"
    assert page["fee_native"] == "0.000010849947159793"
    assert [(t["amount"], t["symbol"]) for t in page["transfers"]] == [
        ("49.712887", "USDC"),
        ("0.020268786568941748", "WETH"),
    ]
    assert page["transfers"][0]["from"] == "dev-orbs.eth" and page["transfers"][1]["to"] == "Null: 0x000…000"
    assert page["has_internal_section"] and [i["amount"] for i in page["internal"]] == [
        "0.020268786568941748",
        "0.000016357",
    ]
    assert page["internal"][0]["from"] == "Wrapped Ether"


def test_parse_bsctrace_page():
    page = parse_etherscan_text(BSCTRACE)  # dispatches on the page's vocabulary
    assert page["source"] == "bsctrace"
    assert page["status"] == "Success" and page["block"] == 126394384
    assert page["from"] == "0xbf26b0fd8eeed416947fe45048b6968a31ab5950"
    assert page["to"] == "0xb03f42644ed3804b45af4b3264a89bcd93d01ab7"
    assert page["fee_native"] == "0.00001346455"
    assert [(t["amount"], t["symbol"]) for t in page["transfers"]] == [
        ("105.117498057118695424", "USDT"),
        ("0.000014248460035563", "WBNB"),
    ]
    assert [i["amount"] for i in page["internal"]] == ["0.136549699", "0.000000754294718702"]
    assert parse_bsctrace_text(BSCTRACE)["transfers"] == page["transfers"]


def test_parse_blockscout_compact():
    data = {
        "compact": {
            "s": "ok",
            "b": 10,
            "f": "123",
            "from": "0xAA",
            "to": "0xBB",
            "more": False,
            "t": [
                [ZERO, "0xcc", "0xweth", "WETH", "18", "5", "token_minting"],
                ["0xcc", "0xdd", "0xtok", "T", None, None, "token_transfer"],
            ],
            "i": [["0xcc", "0xdd", "7"]],
        }
    }
    page = parse_blockscout(data)
    assert page["fee_wei"] == 123 and page["from"] == "0xaa" and page["to"] == "0xbb"
    assert page["transfers"] == [
        {
            "from": ZERO,
            "to": "0xcc",
            "token": "0xweth",
            "symbol": "WETH",
            "decimals": 18,
            "value": 5,
            "type": "token_minting",
        }
    ]
    assert page["internal"] == [{"from": "0xcc", "to": "0xdd", "value": 7}]


def test_trace_natives_splits_root_and_delegatecall_echoes():
    trace = {
        "type": "CALL",
        "from": "0xEOA",
        "to": "0xOP",
        "value": "0x5",
        "calls": [
            {
                "type": "CALL",
                "from": "0xOP",
                "to": "0xWETH",
                "value": "0x3",
                "calls": [
                    {"type": "DELEGATECALL", "from": "0xWETH", "to": "0xIMPL", "value": "0x3", "calls": []}
                ],
            },
            {"type": "STATICCALL", "from": "0xOP", "to": "0xX", "value": "0x0", "calls": []},
        ],
    }
    all_calls, internal, echoes = trace_natives(trace)
    assert all_calls == [("ETH", "0xeoa", "0xop", 5), ("ETH", "0xop", "0xweth", 3)]
    assert internal == [("ETH", "0xop", "0xweth", 3)]
    assert echoes == [("ETH", "0xweth", "0ximpl", 3)]
    assert trace_natives(None) == ([], [], [])
    assert Decimal("1") == Decimal(1)
