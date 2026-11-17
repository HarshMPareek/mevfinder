"""explorer-pull: compact copies from Blockscout API v2 and NodeReal (BSCTrace) data, pagination, hash selection, saving (offline)."""

import json

from arbkit.chain.spec import load_chain
from arbkit.verify.explorer import parse_blockscout
from arbkit.verify.explorer_pull import (
    blockscout_pages,
    compact_blockscout,
    compact_nodereal,
    partial_hashes,
    pull,
    pull_blockscout,
    pull_nodereal,
    save_copy,
    sheet_hashes,
    text_copy_hashes,
)

H = "0x" + "ab" * 32


class Fake:
    """A fetcher answering from a url -> (status, body) table, recording the order of requests."""

    name = "fake"
    ready = True

    def __init__(self, table):
        self.table, self.calls = table, []

    def get(self, url):
        self.calls.append(url)
        hit = self.table.get(url)
        if hit is None:
            return 404, "not found"
        status, body = hit
        return status, body if isinstance(body, str) else json.dumps(body)

    def close(self):
        self.closed = True


def test_blockscout_pages_follow_next_page_params():
    base = "https://x/api/v2/transactions/0x1/token-transfers"
    fake = Fake(
        {
            base: (200, {"items": [{"a": 1}], "next_page_params": {"index": 5, "block_number": 9}}),
            base + "?index=5&block_number=9": (200, {"items": [{"a": 2}], "next_page_params": None}),
        }
    )
    items, complete, status = blockscout_pages(fake.get, base)
    assert [i["a"] for i in items] == [1, 2] and complete and status == 200
    items, complete, _ = blockscout_pages(
        Fake({base: (200, {"items": [{"a": 1}], "next_page_params": {"index": 1}})}).get, base
    )
    assert [i["a"] for i in items] == [
        1
    ] and not complete  # second page unreachable: incomplete, never silently "exact"


def test_compact_blockscout_matches_the_parser_shape():
    tx = {
        "status": "ok",
        "block_number": 77,
        "fee": {"value": "123"},
        "from": {"hash": "0xAA"},
        "to": {"hash": "0xBB"},
    }
    transfers = [
        {
            "from": {"hash": "0xcc"},
            "to": {"hash": "0xdd"},
            "token": {"address_hash": "0xtok", "symbol": "T", "decimals": "6"},
            "total": {"value": "5"},
            "type": "token_transfer",
        },
        {
            "from": {"hash": "0xcc"},
            "to": {"hash": "0xdd"},
            "token": {"address": "0xnft"},
            "total": {"token_id": "1"},
            "type": "token_transfer",
        },
    ]
    internal = [
        {"from": {"hash": "0xcc"}, "to": {"hash": "0xee"}, "value": "7"},
        {"from": {"hash": "0xcc"}, "to": {"hash": "0xee"}, "value": "0"},
        {"from": {"hash": "0xcc"}, "to": {"hash": "0xff"}, "value": "9", "error": "out of gas"},
    ]
    compact = compact_blockscout(tx, transfers, internal, complete=True)
    assert compact["t"] == [["0xcc", "0xdd", "0xtok", "T", "6", "5", "token_transfer"]] and compact["i"] == [
        ["0xcc", "0xee", "7"]
    ]
    assert (
        compact["f"] == "123"
        and compact["b"] == 77
        and not compact["more"]
        and compact["internal_items"] == 3
    )
    page = parse_blockscout({"compact": compact})
    assert (
        page["fee_wei"] == 123
        and page["from"] == "0xaa"
        and page["transfers"][0]["value"] == 5
        and page["internal"] == [{"from": "0xcc", "to": "0xee", "value": 7}]
    )
    assert compact_blockscout(tx, transfers, internal, complete=False)["more"]


def test_pull_blockscout_reports_challenge_and_http_failures():
    root = "https://x/api/v2/transactions/" + H
    good = Fake(
        {
            root: (
                200,
                {
                    "status": "ok",
                    "block_number": 1,
                    "fee": {"value": "1"},
                    "from": {"hash": "0xa"},
                    "to": {"hash": "0xb"},
                },
            ),
            root + "/token-transfers": (200, {"items": [], "next_page_params": None}),
            root + "/internal-transactions": (200, {"items": [], "next_page_params": None}),
        }
    )
    compact, reason = pull_blockscout(good.get, "https://x", H)
    assert reason is None and compact["t"] == [] and compact["i"] == [] and not compact["more"]
    assert pull_blockscout(Fake({root: (403, "<title>Just a moment...</title>")}).get, "https://x", H) == (
        None,
        "challenge",
    )
    assert pull_blockscout(Fake({root: (500, "boom")}).get, "https://x", H) == (
        None,
        "transaction endpoint http 500",
    )


def test_compact_nodereal_uses_raw_values_and_drops_the_root_value_call():
    detail = {
        "blockNumber": 126547915,
        "fees": 294443250000000,
        "value": "5",
        "ethereumSpecific": {"receiptsStatus": 1},
        "vin": {"address": "0xsender"},
        "vout": {"address": "0xcallee"},
        "tokenTransfers": [
            {
                "category": "20",
                "from": "0x1",
                "to": "0x2",
                "token": "0xt",
                "tokenName": "QQQB",
                "symbol": None,
                "decimal": 18,
                "value": "630682018568869",
            },
            {"category": "721", "from": "0x1", "to": "0x2", "token": "0xn", "tokenName": "NFT", "value": "1"},
        ],
    }
    internal = [
        {"from": "0xsender", "to": "0xcallee", "value": "0x5", "traceIndex": 0},
        {"from": "0xa", "to": "0xb", "value": "0xb622ccab95c6a9", "traceIndex": 5},
        {"from": "0xa", "to": "0xc", "value": "0x0", "traceIndex": 6},
    ]
    compact = compact_nodereal(detail, internal, complete=True)
    assert compact == {
        "s": "ok",
        "b": 126547915,
        "f": "294443250000000",
        "from": "0xsender",
        "to": "0xcallee",
        "t": [["0x1", "0x2", "0xt", "QQQB", "18", "630682018568869", "token_transfer"]],
        "i": [["0xa", "0xb", str(0xB622CCAB95C6A9)]],
        "more": False,
        "internal_items": 3,
    }
    assert compact_nodereal({**detail, "ethereumSpecific": {"receiptsStatus": 0}}, [], True)["s"] == "error"


def test_pull_nodereal_pages_the_internal_list():
    base = "https://n/api"

    def rows(n, start):
        return [
            {"from": "0xa", "to": "0xb", "value": hex(start + k), "traceIndex": start + k} for k in range(n)
        ]

    fake = Fake(
        {
            f"{base}/tx/getDetail?hash={H}": (
                200,
                {
                    "code": 0,
                    "data": {
                        "blockNumber": 1,
                        "fees": 10,
                        "value": "0",
                        "ethereumSpecific": {"receiptsStatus": 1},
                        "vin": {"address": "0xs"},
                        "vout": {"address": "0xc"},
                        "tokenTransfers": [],
                    },
                },
            ),
            f"{base}/tx/getInternalTxList?page=1&pageSize=2&hash={H}": (
                200,
                {"code": 0, "data": {"list": rows(2, 1), "total": 3}},
            ),
            f"{base}/tx/getInternalTxList?page=2&pageSize=2&hash={H}": (
                200,
                {"code": 0, "data": {"list": rows(1, 3), "total": 3}},
            ),
        }
    )
    compact, reason = pull_nodereal(fake.get, base, H, page_size=2)
    assert reason is None and [int(i[2]) for i in compact["i"]] == [1, 2, 3] and not compact["more"]
    assert pull_nodereal(
        Fake({f"{base}/tx/getDetail?hash={H}": (200, {"code": 1, "data": None})}).get, base, H
    )[1].startswith("getDetail")


def test_hash_selection_from_sheets_checks_and_text_copies(tmp_path):
    sheet = tmp_path / "handreview_x.json"
    sheet.write_text(json.dumps({"entries": [{"tx_hash": H.upper()}, {"tx_hash": "0x" + "cd" * 32}]}))
    assert sheet_hashes(sheet) == [H, "0x" + "cd" * 32]
    (tmp_path / "explorer_check_20261009.json").write_text(
        json.dumps(
            {
                "results": [
                    {
                        "tx_hash": "0x" + "01" * 32,
                        "verdict": "partial",
                        "partial": ["explorer has not indexed internal transfers"],
                    },
                    {
                        "tx_hash": "0x" + "02" * 32,
                        "verdict": "partial",
                        "partial": ["explorer lists 1 of 4 internal transfers"],
                    },
                    {
                        "tx_hash": "0x" + "03" * 32,
                        "verdict": "partial",
                        "partial": ["page shows only the first 20 of 22 token transfers"],
                    },
                    {"tx_hash": "0x" + "04" * 32, "verdict": "exact", "partial": []},
                    {"tx_hash": "0x" + "05" * 32, "verdict": "mismatch:fee_exact", "partial": []},
                ]
            }
        )
    )
    assert partial_hashes(tmp_path) == ["0x" + "01" * 32, "0x" + "02" * 32, "0x" + "03" * 32]
    explorer = tmp_path / "explorer"
    explorer.mkdir()
    (explorer / f"{H}.txt").write_text("page")
    assert text_copy_hashes(explorer) == [H]


def test_save_copy_supersedes_a_page_text_and_pull_writes_every_hash(tmp_path):
    explorer = tmp_path / "explorer"
    explorer.mkdir()
    (explorer / f"{H}.txt").write_text("page")
    path = save_copy(explorer, H, {"s": "ok"}, "src")
    assert (
        path.exists()
        and not (explorer / f"{H}.txt").exists()
        and (explorer / "superseded" / f"{H}.txt").read_text() == "page"
    )
    assert json.loads(path.read_text())["compact"] == {"s": "ok"}
    spec = load_chain("ethereum")
    root = "https://x/api/v2/transactions/"
    h2 = "0x" + "cd" * 32
    fake = Fake(
        {
            root + H: (
                200,
                {
                    "status": "ok",
                    "block_number": 1,
                    "fee": {"value": "1"},
                    "from": {"hash": "0xa"},
                    "to": {"hash": "0xb"},
                },
            ),
            root + H + "/token-transfers": (200, {"items": [], "next_page_params": None}),
            root + H + "/internal-transactions": (200, {"items": [], "next_page_params": None}),
            root + h2: (500, "down"),
        }
    )
    result = pull(
        spec,
        [H, h2, "nothash"],
        explorer,
        pause=0,
        log=lambda t: None,
        fetcher=fake,
        kind="blockscout",
        base="https://x",
    )
    assert (
        result["pulled"] == [H]
        and result["failed"] == {h2: "transaction endpoint http 500", "nothash": "not a transaction hash"}
        and fake.closed
    )
    assert json.loads((explorer / f"{H}.json").read_text())["source"] == "https://x (blockscout, fake)"
