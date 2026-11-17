"""Dashboard server: the chain list names the CLI default chain; ui/ files are revalidated, API responses never cached."""

import json
import re
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from arbkit.chain.settings import DEFAULT_CHAIN
from arbkit.chain.spec import list_chains
from arbkit.site import access
from arbkit.site.http import make_handler
from arbkit.site.store import Store


@pytest.fixture
def served(tmp_path, monkeypatch):
    servers = []

    def start(default):
        if default is None:
            monkeypatch.delenv("ARB_CHAIN", raising=False)
        else:
            monkeypatch.setenv("ARB_CHAIN", default)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Store(tmp_path)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def fetch(url):
    """A request as the site's own pages make it (with the page's API token)."""
    request = urllib.request.Request(url, headers={access.HEADER: access.issue()})
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, dict(response.headers), response.read()


def test_chain_list_names_the_default_chain_from_arb_chain(served):
    status, headers, body = fetch(served("robinhood") + "/api/chains")
    data = json.loads(body)
    assert status == 200
    assert data["default"] == "robinhood"
    assert [c["name"] for c in data["chains"]] == list_chains()
    assert headers["Cache-Control"] == "no-store"


def test_unknown_default_chain_falls_back_to_the_built_in_default(served):
    data = json.loads(fetch(served("nowhere") + "/api/chains")[2])
    assert data["default"] == DEFAULT_CHAIN
    assert data["default"] in list_chains()


def test_ui_files_are_revalidated_on_every_load(served):
    # browsers never keep a stale page or unversioned file; a CDN may hold a page 60 s; versioned assets are immutable
    base = served(None)
    for path in ("/explorer", "/app.js", "/explorer.js", "/style.css"):
        status, headers, _ = fetch(base + path)
        assert status == 200, path
        assert headers["Cache-Control"] == "no-cache", path
    status, headers, body = fetch(base + "/")
    assert status == 200 and headers["Cache-Control"] == "public, max-age=0, s-maxage=60"
    version = re.search(rb"/app\.js\?v=(\d+)", body).group(1).decode()
    status, headers, _ = fetch(base + f"/app.js?v={version}")
    assert status == 200 and headers["Cache-Control"] == "public, max-age=31536000, immutable"


def test_chain_data_rereads_only_the_report_files_that_changed(tmp_path, monkeypatch):
    import arbkit.site.reports as site_reports
    from arbkit.site.reports import ChainData

    def report(block, created):
        return {
            "created_at": created,
            "rows": [],
            "blocks": [{"number": block, "timestamp": 1_700_000_000 + block}],
        }

    (tmp_path / "a.json").write_text(json.dumps(report(1, "2026-10-09T00:00:00")))
    (tmp_path / "b.json").write_text(json.dumps(report(2, "2026-10-09T00:00:01")))
    (tmp_path / "latest.json").write_text("{}")
    reads = []
    real = site_reports.load_report
    monkeypatch.setattr(site_reports, "load_report", lambda path: reads.append(path.name) or real(path))
    data = ChainData("base", tmp_path)
    data.refresh()
    assert sorted(reads) == ["a.json", "b.json"] and sorted(data.rows_by_block) == [1, 2]
    data.refresh()
    assert len(reads) == 2  # nothing changed: nothing re-read
    import os

    (tmp_path / "b.json").write_text(json.dumps(report(3, "2026-10-09T00:00:02")))
    os.utime(tmp_path / "b.json", ns=(2_000_000_000_000_000_000, 2_000_000_000_000_000_000))
    data.refresh()
    assert reads[2:] == ["b.json"] and sorted(data.rows_by_block) == [1, 3]
    (tmp_path / "a.json").unlink()
    data.refresh()
    assert len(reads) == 3 and sorted(data.rows_by_block) == [3]


def test_route_lines_name_each_venue_from_the_pool_metadata_not_the_sorted_dex_list(tmp_path):
    """Ethereum 26137380 #199: venues [V4 pool key, V3 pool], dexes ['uniswap_v3', 'uniswap_v4'] sorted: zipping them swapped the names."""
    from arbkit.chain.spec import load_chain
    from arbkit.verify.review import route_lines, venue_dexes

    spec = load_chain("ethereum")
    manager = next(iter(spec.pool_managers))  # the Uniswap V4 PoolManager
    v4 = f"{manager}:0x" + "04" * 32
    v3 = "0xe0554a476a092703abdb3ef35c80e0d76d32939f"
    weth, usdc = spec.weth, "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    (tmp_path / "pool_meta.jsonl").write_text(
        json.dumps([v3, [usdc, weth, "uniswap_v3", "0x1f98", None, None]]) + "\n" + "not json\n"
    )
    dex_of = venue_dexes(spec, tmp_path)
    assert dex_of[v3] == "uniswap_v3" and dex_of[manager] == "uniswap_v4"
    row = {
        "venues": [v4, v3],
        "dexes": ["uniswap_v3", "uniswap_v4"],
        "arb_class": "atomic_cycle",
        "assessment": "trace_supported",
        "trace": {
            "status": "ok",
            "verdict": "cycle",
            "legs": [
                {
                    "venue": v3,
                    "token_in": usdc,
                    "amount_in": "952736674",
                    "token_out": weth,
                    "amount_out": str(358791511114849843),
                },
                {
                    "venue": v4,
                    "token_in": weth,
                    "amount_in": str(357293388011945021),
                    "token_out": usdc,
                    "amount_out": "952736674",
                },
            ],
        },
    }
    lines = route_lines(spec, row, {usdc: 6, weth: 18}, {usdc: "USDC", weth: "WETH"}, dex_of)
    assert lines[0].endswith("uniswap_v3") and lines[1].endswith("uniswap_v4"), lines


def test_ui_stop_uses_the_pid_file_and_tolerates_a_stale_one(tmp_path, monkeypatch):
    import subprocess
    import sys

    from arbkit.site.process import process_alive, read_pidfile, stop, write_pidfile

    pidfile = tmp_path / "ui.pid"
    assert read_pidfile(pidfile) is None
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    write_pidfile(pidfile, child.pid, 8787)
    assert read_pidfile(pidfile) == (child.pid, 8787) and process_alive(child.pid)
    monkeypatch.setattr("arbkit.site.process.running_servers", lambda: [])
    assert stop(None, pidfile, wait_s=5.0) == 0
    child.wait(timeout=5)
    assert not process_alive(child.pid) and not pidfile.exists()
    pidfile.write_text("999999999 8787\n")  # stale file: nothing to kill, file removed
    assert stop(None, pidfile) == 0 and not pidfile.exists()


def test_server_process_match_ignores_shells_that_mention_the_command():
    from arbkit.site.process import SERVER_COMMAND

    assert SERVER_COMMAND.match(".venv/bin/python -m arbkit ui --port 8787")
    assert SERVER_COMMAND.match("/usr/bin/python3.14 -m arbkit ui")
    assert not SERVER_COMMAND.match("/bin/zsh -c ./arb ui --stop; pytest tests/test_ui_server.py arbkit.ui")
    assert not SERVER_COMMAND.match(".venv/bin/python -m arbkit scan 1")


def _row(index, assessment="not_arb", mev_type="none", **extra):
    row = {
        "block_number": 100,
        "tx_index": index,
        "tx_hash": f"0x{index:064x}",
        "tx_from": "0xsender",
        "tx_to": "0xbot",
        "assessment": assessment,
        "arb_class": "atomic_cycle" if assessment == "trace_supported" else "single_swap",
        "mev_type": mev_type,
        "fees": {"total_fee_eth": "0.001", "priority_fee_per_gas_gwei": "1"},
        "reasons": ["secret_rule_reason"],
        "trace": {"verdict": "closed_positive_cycle", "notes": ["secret_note"], "venues": ["0xpool"]},
    }
    row.update(extra)
    return row


def _cycle(index, trigger=None):
    return _row(
        index,
        "trace_supported",
        "atomic_backrun" if trigger is not None else "atomic_mid_block",
        trigger_type="backrun" if trigger is not None else "mid_block",
        trigger_tx_hash=trigger,
        gross_usd=10.0,
        net_usd=2.5,
        fee_usd=1.5,
        builder_tip_usd=6.0,
        kickback_usd=0.0,
        native_usd=2000.0,
        economics={
            "total_fee_wei": str(750_000_000_000_000),
            "base_fee_wei": str(250_000_000_000_000),
            "priority_fee_wei": str(500_000_000_000_000),
            "l1_fee_wei": "0",
        },
    )


def _chain_with(tmp_path, monkeypatch, rows):
    import arbkit.site.reports as site_reports

    monkeypatch.setattr(site_reports, "aggregate_daily", lambda spec, reports, cache=None: {"days": []})
    (tmp_path / "r.json").write_text(
        json.dumps(
            {
                "created_at": "2026-10-30T00:00:00",
                "rows": rows,
                "blocks": [{"number": 100, "timestamp": 1_790_000_000}],
            }
        )
    )
    return site_reports.ChainData("ethereum", tmp_path)


def test_cost_split_puts_the_base_fee_priority_fee_tip_and_net_in_separate_buckets():
    from arbkit.value.econ import cost_split

    split = cost_split("ethereum", _cycle(3))
    assert split["chain_fee"] == pytest.approx(0.5) and split["priority"] == pytest.approx(1.0)
    assert split["tip"] == 6.0 and split["net"] == 2.5 and split["l1_fee"] == 0
    # Arbitrum's L1 share is inside the gas price: never split out twice
    arb = _cycle(3)
    arb["economics"]["l1_fee_wei"] = str(300_000_000_000_000)
    assert cost_split("arbitrum", arb)["l1_fee"] == 0
    assert cost_split("op-stack", arb)["l1_fee"] == pytest.approx(0.6)


def test_public_view_carries_labels_and_money_but_no_reasons_verdicts_or_rejected_rows():
    from arbkit.site.views import public_view

    view = public_view(_cycle(5, trigger="0x" + "0" * 63 + "4"), 1, "ethereum")
    text = json.dumps(view)
    assert view["kind"] == "arbitrage" and view["net_usd"] == 2.5 and view["costs"]["tip"] == 6.0
    assert (
        "secret" not in text and "verdict" not in text and "assessment" not in text and "0xpool" not in text
    )
    review = public_view(_row(6, "needs_review", "candidate_cycle", gross_usd=99.0), 1, "ethereum")
    assert review["kind"] == "other" and "gross_usd" not in review and "candidate" not in json.dumps(review)


def test_block_marks_the_trigger_of_a_same_block_backrun_and_the_backrun_points_to_its_index(
    tmp_path, monkeypatch
):
    data = _chain_with(
        tmp_path, monkeypatch, [_row(0), _row(1), _cycle(2, trigger="0x" + "0" * 63 + "1"), _cycle(3)]
    )
    block = data.block(100, public=True)
    kinds = [r["kind"] for r in block["rows"]]
    assert kinds == ["other", "trigger", "arbitrage", "arbitrage"]
    assert block["rows"][2]["trigger_tx_index"] == 1 and "trigger_tx_index" not in block["rows"][3]
    econ = data.economics()
    assert econ["cycles"] == 2 and econ["totals"]["gross"] == 20.0 and econ["keep_share"] == 0.25
    assert econ["totals"]["tip"] == 12.0 and econ["chain_fee_label"] == "Base fee (burned)"


def test_public_server_refuses_proofs_evidence_scans_and_validation_and_says_so_in_config(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Store(tmp_path, public=True)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert json.loads(fetch(base + "/api/config")[2]) == {"public": True}
        for path in (
            "/api/base/scan?blocks=1",
            "/api/base/jobs",
            "/api/base/validation",
            "/api/base/block/1/proof",
            "/api/base/tx/0x" + "0" * 64 + "/evidence",
        ):
            with pytest.raises(urllib.error.HTTPError) as err:
                fetch(base + path)
            assert err.value.code == 404, path
    finally:
        server.shutdown()
        server.server_close()


def test_rate_limiter_refuses_past_the_limit_within_the_window():
    from arbkit.site.store import RateLimiter

    limiter = RateLimiter(2, 60.0)
    assert limiter.allow("a") and limiter.allow("a") and not limiter.allow("a") and limiter.allow("b")


def test_singleton_route_subtracts_the_hook_fee_minted_as_claims_from_the_swap_it_served(
    tmp_path, monkeypatch
):
    """Ethereum 26139154 #7: the hooked pool's Swap event says 0.13597 ETH, the hook mints itself 0.0014141 ETH of claims,
    the bot receives 0.13456: the route shows what was settled, and the hook's cut on the edge."""
    import gzip

    import arbkit.site.reports as site_reports
    from mevlab.decode.signatures import event_topic

    manager, hook, token = (
        "0x000000000004444c5dc75cb358380d2e3de08a90",
        "0x0ee851f1fe2f4bdba79fee78969e329c136ca0cc",
        "0x56c915d92e24255fdc52eb114a31399c432b7cc6",
    )
    pool_a, pool_b, zero = "0x" + "a" * 64, "0x" + "b" * 64, "0x" + "0" * 40
    word = lambda v: (v % 2**256).to_bytes(32, "big").hex()  # noqa: E731
    topic = lambda a: "0x" + a[2:].rjust(64, "0")  # noqa: E731
    swap = event_topic("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)")

    def swap_log(i, pool, a0, a1):
        return {
            "address": manager,
            "logIndex": hex(i),
            "topics": [swap, pool, topic("0x85940d36a1f438d4292dd77365d99cc2d1eae3c7")],
            "data": "0x" + word(a0) + word(a1) + word(0) * 4,
        }

    claim = {
        "address": manager,
        "logIndex": hex(3),
        "topics": [
            event_topic("Transfer(address,address,address,uint256,uint256)"),
            topic(zero),
            topic(hook),
            topic(zero),
        ],
        "data": "0x" + word(int(hook, 16)) + word(1414096266990535),
    }
    txh = "0x" + "7" * 64
    receipt = {
        "transactionHash": txh,
        "logs": [
            swap_log(1, pool_a, -128897777850006752, 10**24),
            swap_log(2, pool_b, 135970794902936150, -(10**24)),
            claim,
        ],
    }
    data = tmp_path / "data" / "ethereum"
    (data / "blocks").mkdir(parents=True)
    with gzip.open(data / "blocks" / "100.json.gz", "wt") as f:
        json.dump({"block": {"transactions": []}, "receipts": [receipt]}, f)
    (data / "pool_meta.jsonl").write_text(
        "\n".join(
            json.dumps([f"{manager}:{p}", [zero, token, "uniswap_v4", manager, 3000, h]])
            for p, h in ((pool_a, zero), (pool_b, hook))
        )
    )
    monkeypatch.setattr(site_reports, "ROOT", tmp_path)
    reports = tmp_path / "reports"
    reports.mkdir()
    legs = site_reports.ChainData("ethereum", reports).legs(
        {
            "block_number": 100,
            "tx_hash": txh,
            "assessment": "trace_supported",
            "mev_type": "atomic_backrun",
            "trace": {"cycle_path": [], "legs": []},
        },
        public=True,
    )
    assert [(leg["symbol_in"], leg["symbol_out"]) for leg in legs] == [
        ("ETH", "0x56c915…"),
        ("0x56c915…", "ETH"),
    ]
    assert legs[1]["amount_out"] == "0.134556698635945615" and legs[1]["hook_fee"] == "0.001414096266990535"
    assert "venue" not in legs[0] and legs[0]["amount_out"] is None  # unknown decimals: no raw integer shown


def test_learn_articles_read_front_matter_newest_first_and_serve_only_plain_slugs(tmp_path):
    from arbkit.site.learn import learn_articles

    (tmp_path / "old-post.md").write_text("---\ntitle: Old\ndate: 2026-01-01\nsummary: first\n---\n# Hello\n")
    (tmp_path / "new-post.md").write_text(
        "---\ntitle: New\ndate: 2026-10-30\n---\nBody {{tx ethereum 0xabc}}\n"
    )
    (tmp_path / "Bad Name.md").write_text("ignored")
    articles = learn_articles(tmp_path)
    assert [a["slug"] for a in articles] == ["new-post", "old-post"]
    assert (
        articles[1]["title"] == "Old"
        and articles[1]["summary"] == "first"
        and articles[1]["markdown"].startswith("# Hello")
    )
    assert articles[0]["markdown"].strip() == "Body {{tx ethereum 0xabc}}"


def test_learn_endpoint_refuses_unknown_and_path_like_slugs(tmp_path, monkeypatch):
    import arbkit.site.learn as site_learn

    monkeypatch.setattr(site_learn, "LEARN_DIR", tmp_path)
    (tmp_path / "hello.md").write_text("---\ntitle: Hi\n---\ntext")
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Store(tmp_path, public=True)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert [a["slug"] for a in json.loads(fetch(base + "/api/learn")[2])["articles"]] == ["hello"]
        assert json.loads(fetch(base + "/api/learn/hello")[2])["title"] == "Hi"
        for path in ("/api/learn/nothing", "/api/learn/..%2F..%2Fsecret"):
            with pytest.raises(urllib.error.HTTPError) as err:
                fetch(base + path)
            assert err.value.code == 404, path
    finally:
        server.shutdown()
        server.server_close()


def test_raw_logs_return_the_receipt_as_on_chain_with_known_event_names(tmp_path, monkeypatch):
    import gzip

    import arbkit.site.reports as site_reports
    from mevlab.decode.signatures import event_topic

    txh = "0x" + "9" * 64
    data = tmp_path / "data" / "ethereum" / "blocks"
    data.mkdir(parents=True)
    receipt = {
        "transactionHash": txh,
        "status": "0x1",
        "gasUsed": "0x5208",
        "logs": [
            {
                "logIndex": "0x2",
                "address": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
                "topics": [event_topic("Deposit(address,uint256)"), "0x" + "0" * 64],
                "data": "0x01",
            },
            {"logIndex": "0x3", "address": "0x" + "1" * 40, "topics": ["0x" + "ab" * 32], "data": "0x"},
        ],
    }
    with gzip.open(data / "100.json.gz", "wt") as f:
        json.dump(
            {
                "block": {
                    "transactions": [
                        {"hash": txh, "from": "0xfrom", "to": "0xto", "value": "0x0", "input": "0x12345678aa"}
                    ]
                },
                "receipts": [receipt],
            },
            f,
        )
    monkeypatch.setattr(site_reports, "ROOT", tmp_path)
    monkeypatch.setattr(site_reports, "aggregate_daily", lambda spec, reports, cache=None: {"days": []})
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "r.json").write_text(
        json.dumps(
            {
                "created_at": "2026-10-30T00:00:00",
                "rows": [_row(0, tx_hash=txh)],
                "blocks": [{"number": 100, "timestamp": 1_790_000_000}],
            }
        )
    )
    raw = site_reports.ChainData("ethereum", reports).raw_logs(txh)
    assert (
        raw["available"] and raw["status"] == 1 and raw["gas_used"] == 21000 and raw["input"] == "0x12345678"
    )
    assert [(lg["index"], lg["event"]) for lg in raw["logs"]] == [(2, "WETH Deposit"), (3, "")]
    assert raw["logs"][0]["address"] == "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
