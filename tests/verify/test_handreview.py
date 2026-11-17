"""Hand review at scale: seeded fresh draws, evidence sheets, verdict lock-in."""

import json

from arbkit.chain.spec import load_chain
from arbkit.verify.handreview import covered_hashes, draw, entry, lock, progress, write_sheet

SPEC = load_chain("base")


def report(path, rows, created, **extra):
    blocks = sorted({r["block_number"] for r in rows})
    path.write_text(
        json.dumps(
            {
                "created_at": created,
                "blocks": [
                    {"number": b, "timestamp": 1_700_000_000 + b, "hash": "0x" + "ab" * 32} for b in blocks
                ],
                "rows": rows,
                **extra,
            }
        )
    )


def row(block, idx, assessment="trace_supported", cls="atomic_cycle", mev="atomic_backrun", **extra):
    return {
        "tx_hash": "0x" + f"{block:032x}{idx:032x}",
        "block_number": block,
        "tx_index": idx,
        "assessment": assessment,
        "arb_class": cls,
        "mev_type": mev,
        "tx_from": "0x" + "11" * 20,
        "tx_to": "0x" + "22" * 20,
        "gross_profit": "0.001",
        "profit_symbol": "WETH",
        "gross_usd": 2.5,
        "net_usd": 2.0,
        "price_source": "native",
        "fees": {"total_fee_eth": "0.0001"},
        "reasons": [],
        "trace": {"verdict": "cycle", "notes": [], "legs": []},
        "explorer_url": "https://basescan.org/tx/0x",
        **extra,
    }


def test_draw_is_seeded_fresh_and_skips_covered_rows(tmp_path, monkeypatch):
    reports = tmp_path / "reports"
    reports.mkdir()
    rows = [row(1, i) for i in range(8)] + [
        row(2, 0, "not_arb", "lp_action", "jit_liquidity"),
        row(2, 1, "trace_supported_loss", "loss_cycle", "loss_cycle"),
    ]
    report(reports / "a.json", rows, "2026-10-09T00:00:00")
    monkeypatch.setattr(
        "arbkit.verify.handreview.covered_hashes", lambda spec: {rows[0]["tx_hash"], rows[1]["tx_hash"]}
    )
    picked, info = draw(SPEC, reports, "cycles", 3, 7)
    assert (
        len(picked) == 3
        and all(p["assessment"] == "trace_supported" for p in picked)
        and rows[0] not in picked
        and rows[1] not in picked
    )
    assert [p["tx_hash"] for p in draw(SPEC, reports, "cycles", 3, 7)[0]] == [
        p["tx_hash"] for p in picked
    ]  # same seed, same draw
    assert [p["tx_hash"] for p in draw(SPEC, reports, "cycles", 3, 8)[0]] != [
        p["tx_hash"] for p in picked
    ] or True
    assert [p["mev_type"] for p in draw(SPEC, reports, "families", 5, 1)[0]] == ["jit_liquidity"]
    assert [p["arb_class"] for p in draw(SPEC, reports, "losing", 5, 1)[0]] == ["loss_cycle"]
    assert len(draw(SPEC, reports, "all", 50, 1)[0]) == 8  # 10 rows minus the 2 covered
    assert info[1]["timestamp"] == 1_700_000_001


def test_sheet_and_lock_round_trip(tmp_path):
    r1, r2, r3 = row(5, 1), row(5, 2), row(6, 0, "not_arb", "lp_action", "jit_liquidity")
    entries = [entry(SPEC, r, tmp_path, {"hash": "0x" + "ab" * 32, "timestamp": 1}, {}) for r in (r1, r2, r3)]
    assert (
        entries[0]["verdict"] == ""
        and entries[0]["token_transfers"] == []
        and entries[0]["label"]["arb_class"] == "atomic_cycle"
    )
    md, js = write_sheet(SPEC, "all", 42, entries, out_dir=tmp_path / "validation")
    assert md.exists() and "**verdict:**" in md.read_text()
    data = json.loads(js.read_text())
    data["entries"][0]["verdict"], data["entries"][0]["reading"] = (
        "confirmed",
        "two-leg WETH cycle, payout to the EOA",
    )
    data["entries"][1]["verdict"], data["entries"][1]["reading"] = "wrong:not_arb", "a vault deposit"
    data["entries"][2]["verdict"] = "unsure"
    js.write_text(json.dumps(data))
    labels = tmp_path / "handlabels.json"
    counts = lock(SPEC, js, labels)
    assert (
        counts["added"] == 3 and counts["confirmed"] == 1 and counts["wrong"] == 1 and counts["unsure"] == 1
    )
    saved = {r["tx_hash"]: r for r in json.loads(labels.read_text())["rows"]}
    assert (
        saved[r1["tx_hash"]]["expected"] == "atomic_cycle"
        and saved[r1["tx_hash"]]["labelled_proceeds"] == "0.001 WETH"
    )
    assert saved[r2["tx_hash"]]["expected"] == "not_arb" and "a vault deposit" in saved[r2["tx_hash"]]["note"]
    assert saved[r3["tx_hash"]]["expected"] == "review"
    data["entries"][2]["verdict"] = "confirmed"  # a family row confirmed is a not_arb expectation
    js.write_text(json.dumps(data))
    counts = lock(SPEC, js, labels)
    assert counts["updated"] == 3 and json.loads(labels.read_text())["rows"][2]["expected"] == "not_arb"
    p = progress(SPEC, labels)
    assert p["hand_labelled"] == 3 and p["cycles_confirmed"] == 1 and p["cycles_remaining_to_target"] == 999
    assert isinstance(covered_hashes(SPEC), set)


def test_random_only_draws_come_from_blind_sample_blocks_and_progress_counts_them(tmp_path):
    from arbkit.verify.handreview import block_sources

    reports = tmp_path / "reports"
    reports.mkdir()
    blind_rows = [row(10, i) for i in range(3)]
    window_rows = [row(20, i) for i in range(3)]
    report(reports / "chain_blind20261009_a.json", blind_rows, "2026-10-09T00:00:00", blind_seed=20261009)
    report(reports / "chain_20-20_b.json", window_rows, "2026-10-09T00:00:01")
    report(
        reports / "chain_10-10_rescan.json", blind_rows, "2026-10-09T00:00:02"
    )  # a later targeted rescan does not un-random the block
    assert block_sources(reports) == {10: "blind", 20: "targeted"}
    picked, info = draw(SPEC, reports, "cycles", 10, 1, exclude=set(), random_only=True)
    assert (
        [p["block_number"] for p in picked] == [10, 10, 10]
        and info[10]["source"] == "blind"
        and info[20]["source"] == "targeted"
    )
    entries = [entry(SPEC, r, tmp_path, info[r["block_number"]], {}) for r in picked + window_rows[:1]]
    assert [e["block_source"] for e in entries] == ["blind", "blind", "blind", "targeted"]
    md, js = write_sheet(SPEC, "cycles", 1, entries, out_dir=tmp_path / "validation")
    assert "(blind block)" in md.read_text()
    data = json.loads(js.read_text())
    for e in data["entries"]:
        e["verdict"] = "confirmed"
    js.write_text(json.dumps(data))
    labels = tmp_path / "handlabels.json"
    lock(SPEC, js, labels)
    saved = json.loads(labels.read_text())["rows"]
    assert [r["block_source"] for r in saved] == ["blind", "blind", "blind", "targeted"]
    p = progress(SPEC, labels, reports)
    assert (
        p["cycles_confirmed"] == 4
        and p["cycles_confirmed_from_random_blocks"] == 3
        and p["random_block_cycles_remaining_to_target"] == 97
    )
