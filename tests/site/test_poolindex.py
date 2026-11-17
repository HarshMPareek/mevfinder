"""The site's pool metadata index: built from the append-only jsonl, updated from the last offset only, looked up per
pool (Base's cache is 3.1 GB: re-reading it per page took 23-29 s and ran the site out of memory)."""

import json

from arbkit.site.poolindex import LazyMap, PoolIndex

MANAGER = "0x" + "44" * 20


def line(key, t0, t1, dex, hooks=None):
    return json.dumps([key, [t0, t1, dex, "0xfactory", 3000, hooks]]) + "\n"


def test_index_is_built_then_only_appended_lines_are_read(tmp_path):
    jl = tmp_path / "pool_meta.jsonl"
    jl.write_text(
        line("0xAA", "0x01", "0x02", "uniswap_v3")
        + line(f"{MANAGER}:0xpool", "0x03", "0x04", "uniswap_v4", "0xHOOK")
    )
    idx = PoolIndex(jl, tmp_path / "pool_index.sqlite", {MANAGER: {"name": "uniswap_v4"}})
    assert idx.update() == 2
    assert idx.get("0xaa") == ("0x01", "0x02", "uniswap_v3", "")
    assert idx.get(f"{MANAGER}:0xpool")[3] == "0xhook"
    assert idx.dex("0xAA") == "uniswap_v3" and idx.dex(MANAGER) == "uniswap_v4" and idx.dex("0xbb") is None
    with jl.open("a") as f:
        f.write(line("0xbb", "0x05", "0x06", "aerodrome"))
        f.write('["0xcc", ["0x07"')  # a line still being written (does not parse)
    assert idx.update() == 1 and idx.dex("0xbb") == "aerodrome" and idx.get("0xcc") is None
    assert idx.update() == 0
    dexes = LazyMap(idx.dex)
    assert dexes.get("0xAA") == "uniswap_v3" and dexes.get("0xzz", "?") == "?" and "0xbb" in dexes


def test_a_rewritten_cache_is_indexed_again(tmp_path):
    jl = tmp_path / "pool_meta.jsonl"
    jl.write_text(line("0xaa", "0x01", "0x02", "uniswap_v3") * 3)
    idx = PoolIndex(jl, tmp_path / "pool_index.sqlite")
    idx.update()
    jl.write_text(line("0xdd", "0x01", "0x02", "sushiswap"))
    assert idx.update() == 1 and idx.dex("0xdd") == "sushiswap"


def test_a_last_line_without_newline_counts_when_complete(tmp_path):
    jl = tmp_path / "pool_meta.jsonl"
    jl.write_text(
        line("0xaa", "0x01", "0x02", "uniswap_v3") + line("0xbb", "0x03", "0x04", "uniswap_v2").rstrip("\n")
    )
    idx = PoolIndex(jl, tmp_path / "pool_index.sqlite")
    assert idx.update() == 2 and idx.dex("0xbb") == "uniswap_v2" and idx.pending_bytes() == 0
