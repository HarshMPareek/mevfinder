"""The JSON API behind the site: one function per endpoint, each returning (payload, status). No HTTP here.

    /api/config                         public or private server
    /api/learn, /api/learn/<slug>       Learn articles
    /api/chains                         every chain's summary over the site's window
    /api/<chain>/overview               summary, daily figures, top bots, recent rows, economics (window=24h|3d|7d|all)
    /api/<chain>/txs                    rows (type, min_usd, sort, limit, offset, window)
    /api/<chain>/blocks                 blocks (sort, limit, offset, window)
    /api/<chain>/block/<n>              one block's rows
    /api/<chain>/tx/<hash>              one transaction (public view on the public site)
    /api/<chain>/bot/<address>          one operator's rows
    /api/<chain>/operators              operators ranked (sort, limit, offset, window)
    /api/<chain>/daily | economics | builders | families | live
    private only: search, scan, jobs, job/<id>, validation, block/<n>/proof, tx/<hash>/logs | evidence

A chain is backed either by saved reports (site.reports.ChainData) or by a live store (site.live.LiveChainData);
endpoints that page and window rows exist in full only on live stores.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from arbkit.chain.settings import Settings
from arbkit.site.jobs import locate, parse_blocks_text
from arbkit.site.learn import learn_articles
from arbkit.site.store import Store
from arbkit.site.views import public_figures
from arbkit.site.windows import chain_summary, window_hours

PRIVATE_ENDPOINTS = {"scan", "jobs", "job", "validation"}
MAX_OFFSET = 2_000_000  # deepest row a page may start at (7 days of the busiest chain fit)
ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")


@dataclass
class Download:
    """A JSON file the browser saves (Content-Disposition: attachment)."""

    payload: Any
    filename: str


Answer = tuple[Any, int]


def _found(value, missing: str) -> Answer:
    return (value, 200) if value else ({"error": missing}, 404)


def answer(store: Store, parts: list[str], query: dict[str, str]) -> Answer | Download:
    """The answer to /api/<parts...>?<query>."""
    public = store.public
    try:
        if parts == ["config"]:
            return {"public": public}, 200
        if parts == ["learn"]:
            return {
                "articles": [{k: v for k, v in a.items() if k != "markdown"} for a in learn_articles()]
            }, 200
        if len(parts) == 2 and parts[0] == "learn":
            return _found(
                next((a for a in learn_articles() if a["slug"] == parts[1]), None), "no such article"
            )
        if (
            public
            and len(parts) >= 2
            and (parts[1] in PRIVATE_ENDPOINTS or parts[-1] in ("proof", "evidence"))
        ):
            return {"error": "not available"}, 404
        if parts == ["chains"]:
            # live stores: the site's window (what every page shows; the all-time scan took 8-13 s on BSC/Base/RH)
            return {
                "chains": [chain_summary(store.get(n)) for n in store.chains],
                "default": store.default_chain,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }, 200
        if not parts:
            return {"error": "unknown endpoint"}, 404
        chain = store.get(parts[0])
        endpoint = CHAIN_ENDPOINTS.get(
            (len(parts), parts[1] if len(parts) > 1 else "", parts[3] if len(parts) == 4 else "")
        )
        if endpoint is None:
            return {"error": "unknown endpoint"}, 404
        return endpoint(store, chain, parts, query)
    except KeyError:
        return {"error": "unknown chain"}, 404
    except ValueError:
        return {"error": "bad request"}, 400


def _paging(query: dict[str, str], default: int, cap: int) -> tuple[int, int]:
    return min(int(query.get("limit", default)), cap), min(int(query.get("offset", 0)), MAX_OFFSET)


def live(store, chain, parts, query) -> Answer:
    if not hasattr(chain, "live"):
        return {"error": "no live store for this chain"}, 404
    return chain.live(store.public), 200


def overview(store, chain, parts, query) -> Answer:
    public = store.public
    chain.refresh()
    # live stores: a rolling window (window=24h|3d|7d, default 7 days; days=N is N x 24 hours; window=all:
    # everything stored, the October sample days included)
    hours = window_hours(query)
    if hours and hasattr(chain, "daily_for"):
        daily = chain.daily_for(None, hours=hours)
        return {
            "summary": chain.summary(hours=hours),
            "daily": public_figures(daily) if public else daily,
            "operators": chain.operators(25, hours=hours),
            "recent": chain.transactions(limit=50, public=public),
            "economics": chain.economics(hours=hours),
        }, 200
    return {
        "summary": chain.summary(),
        "daily": public_figures(chain.daily) if public else chain.daily,
        "operators": chain.operators(),
        "recent": chain.transactions(limit=50, public=public),
        "economics": chain.economics(),
    }, 200


def transactions(store, chain, parts, query) -> Answer:
    public = store.public
    kinds = set(query["type"].split(",")) if query.get("type") else None
    min_usd = float(query["min_usd"]) if query.get("min_usd") else None
    limit, offset = _paging(query, 200, 100 if public else 2000)
    if not hasattr(chain, "transactions_total"):  # saved reports: one list, newest first
        return {"rows": chain.transactions(limit, kinds, min_usd, public=public, sort=query.get("sort"))}, 200
    hours = window_hours(query, None)
    rows = chain.transactions(
        limit, kinds, min_usd, public=public, sort=query.get("sort"), offset=offset, hours=hours
    )
    total = chain.transactions_total(kinds, min_usd, public=public, hours=hours)
    return {"rows": rows, "offset": offset, "limit": limit, "total": total}, 200


def daily(store, chain, parts, query) -> Answer:
    chain.refresh()
    return (public_figures(chain.daily) if store.public else chain.daily), 200


def bot(store, chain, parts, query) -> Answer:
    if not ADDRESS.fullmatch(parts[2] or ""):
        return {"error": "unknown endpoint"}, 404
    if not hasattr(chain, "operators_ranked"):
        return chain.bot(parts[2], public=store.public), 200
    limit, offset = _paging(query, 50, 100)
    page = chain.bot(parts[2], public=store.public, limit=limit, offset=offset, sort=query.get("sort"))
    return {**page, "offset": offset, "limit": limit}, 200


def operators(store, chain, parts, query) -> Answer:
    limit, offset = _paging(query, 25, 200)
    if not hasattr(chain, "operators_ranked"):
        return {"operators": chain.operators(limit)}, 200
    ranked = chain.operators_ranked(window_hours(query, None), query.get("sort", "gross"))
    return {
        "operators": ranked[offset : offset + limit],
        "total": len(ranked),
        "offset": offset,
        "limit": limit,
    }, 200


def families(store, chain, parts, query) -> Answer:
    result = chain.families(store.public)
    return (public_figures(result) if store.public else result), 200


def search(store, chain, parts, query) -> Answer:
    found = locate(Settings.load(chain.name), query.get("q", ""))
    if found["kind"] == "blocks":
        found["scanned"] = [n for n in found["blocks"] if chain.has_block(n)]
    elif found["kind"] == "time":
        found["scanned"] = chain.has_block(found["block"])
    elif found["kind"] == "transaction":
        found["known"] = chain.get_row(found["hash"]) is not None
    return found, 200


def scan(store, chain, parts, query) -> Answer:
    blocks = parse_blocks_text(query["blocks"]) if query.get("blocks") else None
    tx = query.get("tx", "").lower() or None
    mode = query.get("traces", "candidates")
    if mode not in {"candidates", "swaps", "all", "none"}:
        raise ValueError("traces must be candidates, swaps, all or none")
    job = store.jobs.submit(
        chain.name, blocks, tx, mode, label=query.get("label", query.get("blocks") or tx or "")
    )
    return job.to_json(), 202


def job(store, chain, parts, query) -> Answer:
    found = store.jobs.get(parts[2])
    return (found.to_json(), 200) if found else ({"error": "unknown job"}, 404)


def block_proof(store, chain, parts, query) -> Answer | Download:
    proof = chain.proof(int(parts[2]))
    if not proof:
        return {"error": "block not scanned"}, 404
    return Download(proof, f"{chain.name}_block_{parts[2]}_proof.json")


def blocks(store, chain, parts, query) -> Answer:
    limit = min(int(query.get("limit", 500)), 100 if store.public else 2000)
    if not hasattr(chain, "blocks_total"):
        return {"blocks": chain.blocks_list(limit)}, 200
    offset, hours = min(int(query.get("offset", 0)), MAX_OFFSET), window_hours(query, None)
    return {
        "blocks": chain.blocks_list(limit, offset, query.get("sort", "latest"), hours),
        "total": chain.blocks_total(hours),
        "offset": offset,
        "limit": limit,
    }, 200


def transaction(store, chain, parts, query) -> Answer:
    tx = chain.public_transaction(parts[2]) if store.public else chain.transaction(parts[2])
    return _found(tx, "transaction not in any report")


# (number of path parts, endpoint, sub-resource) -> handler
CHAIN_ENDPOINTS: dict[tuple[int, str, str], Callable] = {
    (2, "live", ""): live,
    (2, "overview", ""): overview,
    (2, "economics", ""): lambda store, chain, parts, query: (chain.economics(), 200),
    (2, "txs", ""): transactions,
    (2, "daily", ""): daily,
    (3, "bot", ""): bot,
    (2, "operators", ""): operators,
    (2, "builders", ""): lambda store, chain, parts, query: (chain.builders(), 200),
    (2, "families", ""): families,
    (2, "validation", ""): lambda store, chain, parts, query: (chain.validation(), 200),
    (2, "search", ""): search,
    (2, "scan", ""): scan,
    (2, "jobs", ""): lambda store, chain, parts, query: ({"jobs": store.jobs.recent(chain.name)}, 200),
    (3, "job", ""): job,
    (4, "block", "proof"): block_proof,
    (4, "tx", "logs"): lambda store, chain, parts, query: _found(
        chain.raw_logs(parts[2]), "transaction not in any report"
    ),
    (4, "tx", "evidence"): lambda store, chain, parts, query: _found(
        chain.evidence(parts[2]), "transaction not in any report"
    ),
    (2, "blocks", ""): blocks,
    (3, "block", ""): lambda store, chain, parts, query: _found(
        chain.block(int(parts[2]), store.public), "block not scanned"
    ),
    (3, "tx", ""): transaction,
}
