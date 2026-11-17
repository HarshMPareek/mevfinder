"""Sandwich recall: direction from a pool's own transfers, and the direction-agnostic bracket screen."""

import gzip
import json

from arbkit.chain.spec import load_chain
from arbkit.scan.sandwich import find_sandwiches, operator_result
from arbkit.verify.brackets import bracket_screen, merged_report, venues_of
from tests.mevlab.test_receipt import BOT, EOA, POOL_A, USDC, WETH, transfer
from tests.scan.test_chains import _row

SPEC = load_chain("ethereum")
VICTIM = "0x" + "e1" * 20
FRONT, VICTIM_TX, BACK = "0x" + "f1" * 32, "0x" + "f2" * 32, "0x" + "f3" * 32
UNKNOWN_EVENT = {"address": POOL_A, "logIndex": "0x0", "topics": ["0x" + "c2" * 32], "data": "0x"}


def receipts_without_swap_events():
    """A DODO-style pool: no decoded swap event, only the transfers (Ethereum 26139154 #86-#88)."""
    return {
        FRONT: {
            "logs": [
                UNKNOWN_EVENT,
                transfer(WETH, BOT, POOL_A, 10**18, 1),
                transfer(USDC, POOL_A, BOT, 3000 * 10**6, 2),
            ]
        },
        VICTIM_TX: {
            "logs": [
                UNKNOWN_EVENT,
                transfer(WETH, VICTIM, POOL_A, 5 * 10**18, 1),
                transfer(USDC, POOL_A, VICTIM, 14000 * 10**6, 2),
            ]
        },
        BACK: {
            "logs": [
                UNKNOWN_EVENT,
                transfer(USDC, BOT, POOL_A, 3000 * 10**6, 1),
                transfer(WETH, POOL_A, BOT, 102 * 10**16, 2),
            ]
        },
    }


def test_direction_falls_back_to_the_pool_transfers_when_the_swap_event_is_unknown():
    rows = [
        _row(1, FRONT, EOA, BOT, [POOL_A]),
        _row(2, VICTIM_TX, VICTIM, "0x" + "77" * 20, [POOL_A]),
        _row(3, BACK, EOA, BOT, [POOL_A]),
    ]
    receipts = receipts_without_swap_events()
    bundle = find_sandwiches(rows, receipts)[0]
    assert bundle["pool"] == POOL_A and bundle["victims"] == [VICTIM_TX]
    result = operator_result(bundle, receipts, WETH, None, EOA)
    assert result["profit_token"] == WETH and result["profit_raw"] == str(2 * 10**16)
    # the victim trading against the front is no sandwich
    receipts[VICTIM_TX]["logs"][1:] = [
        transfer(USDC, VICTIM, POOL_A, 14000 * 10**6, 1),
        transfer(WETH, POOL_A, VICTIM, 5 * 10**18, 2),
    ]
    assert find_sandwiches(rows, receipts) == []


def test_bracket_screen_sees_the_undecoded_venue_and_flags_an_uncounted_round_trip(tmp_path):
    receipts = receipts_without_swap_events()
    assert venues_of(receipts[FRONT]["logs"]) == {POOL_A}
    block_receipts = []
    for i, (h, frm, to) in enumerate(
        ((FRONT, EOA, BOT), (VICTIM_TX, VICTIM, "0x" + "77" * 20), (BACK, EOA, BOT))
    ):
        block_receipts.append(
            {
                "transactionHash": h,
                "transactionIndex": hex(i),
                "status": "0x1",
                "from": frm,
                "to": to,
                "logs": receipts[h]["logs"],
            }
        )
    (tmp_path / "blocks").mkdir()
    with gzip.open(tmp_path / "blocks" / "16.json.gz", "wt") as f:
        json.dump({"block": {"number": "0x10"}, "receipts": block_receipts}, f)
    rows = [
        _row(0, FRONT, EOA, BOT, [POOL_A]),
        _row(1, VICTIM_TX, VICTIM, "0x" + "77" * 20, [POOL_A]),
        _row(2, BACK, EOA, BOT, [POOL_A]),
    ]
    report = {"created_at": "2026-10-09T00:00:00", "blocks": [{"number": 16, "timestamp": 1}], "rows": rows}
    result = bracket_screen(SPEC, report, tmp_path)
    assert (
        result["screened"] == 3
        and result["brackets"] == 1
        and result["kinds"] == {"uncounted round trip with gain": 1}
    )
    suspect = result["suspects"][0]
    assert (
        suspect["back"] == BACK
        and suspect["nets"][WETH]["raw"] == str(2 * 10**16)
        and suspect["share"] < 0.25
    )
    rows[2]["sandwich"] = {"role": "back"}
    assert bracket_screen(SPEC, report, tmp_path)["kinds"] == {"counted sandwich": 1}
    rows[2]["sandwich"] = None
    rows[2]["arb_class"] = "lp_action"
    assert bracket_screen(SPEC, report, tmp_path)["kinds"] == {"jit pair": 1}


def test_merged_report_keeps_the_newest_classification_per_block(tmp_path):
    def report(block, created, cls):
        return {
            "created_at": created,
            "blocks": [{"number": block, "timestamp": block}],
            "rows": [{"tx_hash": f"0x{block:064x}", "block_number": block, "arb_class": cls}],
        }

    (tmp_path / "a.json").write_text(json.dumps(report(1, "2026-10-09T00:00:00", "old")))
    (tmp_path / "b.json").write_text(json.dumps(report(1, "2026-10-09T00:00:01", "new")))
    (tmp_path / "c.json").write_text(json.dumps(report(2, "2026-10-08T00:00:00", "other")))
    merged = merged_report(SPEC, tmp_path)
    assert [b["number"] for b in merged["blocks"]] == [1, 2]
    assert [r["arb_class"] for r in merged["rows"]] == ["new", "other"] and merged[
        "created_at"
    ] == "2026-10-09T00:00:01"
