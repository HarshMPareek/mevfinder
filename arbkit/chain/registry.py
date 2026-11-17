"""On-chain verification of a chain's DEX registry: bytecode present, probe call answers, decoder exists.

Writes chains/<chain>/dex.verified.json. A registry entry without an address (status todo) is reported, not
failed. This does not prove a factory is the canonical deployment; it proves the address has code that answers
the registry's probe at the current head.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from eth_abi import encode

from arbkit.chain.rpc import SafeRpc
from mevlab.decode.signatures import event_topic, selector
from mevlab.decode.swaps import SWAP_DECODERS
from mevlab.hexutil import addr
from mevlab.rpc import RpcError


def _probe_data(fn: str, arg) -> str:
    data = selector(fn)
    if arg is not None:
        types = fn[fn.index("(") + 1 : -1].split(",") if "(" in fn else []
        data += encode(types, [int(arg)]).hex()
    return data


def _check_expect(expect: str, result: str) -> bool:
    if not result or result == "0x":
        return False
    if expect == "nonzero":
        return int(result, 16) != 0
    kind, _, value = expect.partition(":")
    if kind == "uint":
        return int(result, 16) == int(value)
    if kind == "address":
        return addr(result[-40:]) == addr(value)
    return False


async def verify_registry(settings, console) -> int:
    spec = settings.chain
    results = []
    failures = 0
    async with SafeRpc(settings.http_url) as rpc:
        if await rpc.chain_id() != spec.chain_id:
            raise ValueError(f"Configured RPC is not {spec.name} (chain ID {spec.chain_id}).")
        for d in spec.dexes:
            entry = {"name": d["name"], "kind": d.get("kind"), "status": d.get("status")}
            address = d.get("factory") or d.get("pool_manager") or d.get("vault") or d.get("core")
            swap_event = d.get("swap_event")
            if swap_event:
                raw = re.search(r"0x[0-9a-fA-F]{64}", swap_event)
                entry["swap_topic"] = (
                    raw.group(0).lower() if raw else event_topic(swap_event.split("#")[0].strip())
                )
                entry["decoder"] = entry["swap_topic"] in SWAP_DECODERS
            if not address:
                entry["check"] = "no_address"
                results.append(entry)
                continue
            entry["address"] = address
            try:
                code = await rpc.get_code(address)
                entry["has_code"] = bool(code) and code != "0x"
            except RpcError as exc:
                entry["has_code"] = None
                entry["error"] = str(exc)
            probe = d.get("probe")
            if probe and entry.get("has_code"):
                try:
                    result = await rpc.eth_call(address, _probe_data(probe["fn"], probe.get("arg")))
                    entry["probe_ok"] = _check_expect(probe["expect"], result)
                except RpcError as exc:
                    entry["probe_ok"] = False
                    entry["probe_error"] = str(exc)
            ok = entry.get("has_code") and (entry.get("probe_ok", True) if probe else True)
            entry["check"] = "ok" if ok else "FAILED"
            if d.get("status") == "in_scope" and not ok:
                failures += 1
            results.append(entry)
    out = {
        "chain": spec.name,
        "chain_id": spec.chain_id,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "entries": results,
        "failures": failures,
    }
    path = spec.directory / "dex.verified.json"
    path.write_text(json.dumps(out, indent=2) + "\n")
    for e in results:
        mark = {
            "ok": "[green]ok[/green]",
            "FAILED": "[red]FAILED[/red]",
            "no_address": "[yellow]todo[/yellow]",
        }[e["check"]]
        decoder = (
            ""
            if e.get("decoder") is None
            else (" decoder" if e["decoder"] else " [yellow]no decoder[/yellow]")
        )
        console.print(
            f"{mark:20s} {e['name']:28s} {e.get('kind') or '':18s} {e.get('address') or '':44s}{decoder}"
        )
    console.print(f"Saved {path}; {failures} in-scope entries failed.", markup=False)
    return 1 if failures else 0
