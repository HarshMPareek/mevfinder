"""Pool metadata (token0/token1/dex) resolution with a JSON cache. Needed to turn amount0/amount1 into
token flows. Balancer legs carry tokens in the event; V4 pool ids come from Initialize events."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from mevlab.decode.signatures import selector
from mevlab.hexutil import addr
from mevlab.rpc import JsonRpc, RpcError


@dataclass(slots=True)
class PoolMeta:
    token0: str
    token1: str
    dex: str | None = None
    factory: str | None = None
    fee: int | None = None
    hooks: str | None = None


class PoolMetaResolver(Protocol):
    async def get(self, pool: str, pool_id: str | None = None) -> PoolMeta | None: ...


class DictPoolMeta:
    def __init__(self, table: dict[str, PoolMeta]):
        self.table = {k.lower(): v for k, v in table.items()}

    async def get(self, pool: str, pool_id: str | None = None) -> PoolMeta | None:
        key = f"{pool}:{pool_id}".lower() if pool_id else pool.lower()
        return self.table.get(key)


class RpcPoolMeta:
    """Resolves token0()/token1()/factory() via eth_call; caches to a JSON file."""

    SEL_TOKEN0 = selector("token0()")
    SEL_TOKEN1 = selector("token1()")
    SEL_FACTORY = selector("factory()")

    def __init__(
        self,
        rpc: JsonRpc,
        cache_path: Path | None = None,
        factory_to_dex: dict[str, str] | None = None,
    ):
        self.rpc = rpc
        self.cache_path = cache_path
        self.factory_to_dex = {k.lower(): v for k, v in (factory_to_dex or {}).items()}
        self.table: dict[str, PoolMeta] = {}
        self.misses: set[str] = set()
        self._new_since_save = 0
        self._pending: list[tuple[str, PoolMeta]] = []  # entries not yet appended to the jsonl cache
        if cache_path:
            self._load_cache(cache_path)

    # --- cache: compact JSON lines, append-only checkpoints (a million V4 pools must not be rewritten) ---
    def _load_cache(self, path: Path) -> None:
        jl = path.with_suffix(".jsonl")
        if jl.exists():
            with jl.open() as f:
                for line in f:
                    try:
                        k, v = json.loads(line)
                    except ValueError:
                        continue
                    self.table[k] = PoolMeta(*v)
        elif path.exists():  # legacy pretty JSON
            raw = json.loads(path.read_text())
            self.table = {k: PoolMeta(**v) for k, v in raw.items()}
            self.save()

    def save(self) -> None:
        """Append pending entries to the jsonl cache (cheap); full rewrite only when no jsonl exists yet."""
        if not self.cache_path:
            return
        jl = self.cache_path.with_suffix(".jsonl")
        jl.parent.mkdir(parents=True, exist_ok=True)
        if not jl.exists():
            with jl.open("w") as f:
                for k, v in self.table.items():
                    f.write(json.dumps([k, [v.token0, v.token1, v.dex, v.factory, v.fee, v.hooks]]) + "\n")
            self._pending.clear()
            return
        if self._pending:
            with jl.open("a") as f:
                for k, v in self._pending:
                    f.write(json.dumps([k, [v.token0, v.token1, v.dex, v.factory, v.fee, v.hooks]]) + "\n")
            self._pending.clear()

    def _put(self, key: str, meta: PoolMeta) -> None:
        if key in self.table and self.table[key] == meta:
            return  # already cached (re-index over the same range): no duplicate line
        self.table[key] = meta
        self._pending.append((key, meta))
        self._new_since_save += 1
        if self.cache_path and self._new_since_save >= 500:
            self.save()
            self._new_since_save = 0

    async def _call_addr(self, to: str, sel: str) -> str | None:
        try:
            res = await self.rpc.eth_call(to, sel)
        except RpcError:
            return None
        if not res or res == "0x" or len(res) < 66:
            return None
        return addr(res[-40:])

    async def get(self, pool: str, pool_id: str | None = None) -> PoolMeta | None:
        key = f"{pool}:{pool_id}".lower() if pool_id else pool.lower()
        if key in self.table:
            return self.table[key]
        if key in self.misses:
            return None
        if pool_id:
            return await self._lookup_v4(pool.lower(), pool_id.lower(), key)
        t0, t1, fac = await self._resolve_batch(pool)
        if not t0 or not t1:
            self.misses.add(key)
            return None
        meta = PoolMeta(t0, t1, dex=self.factory_to_dex.get(fac or ""), factory=fac)
        self._put(key, meta)
        return meta

    async def _lookup_v4(self, manager: str, pool_id: str, key: str) -> PoolMeta | None:
        """A singleton pool (one manager, many pool ids) is known only from its manager's Initialize event, which a
        registry-aware subclass reads (arbkit's HistoricalPools). Without one it is a miss."""
        self.misses.add(key)
        return None

    async def _resolve_batch(self, pool: str) -> tuple[str | None, str | None, str | None]:
        """token0(), token1(), factory() in one JSON-RPC batch; falls back to single calls."""
        try:
            res = await self.rpc.batch(
                [
                    ("eth_call", [{"to": pool, "data": self.SEL_TOKEN0}, "latest"]),
                    ("eth_call", [{"to": pool, "data": self.SEL_TOKEN1}, "latest"]),
                    ("eth_call", [{"to": pool, "data": self.SEL_FACTORY}, "latest"]),
                ],
                lenient=True,
            )
        except RpcError:
            t0 = await self._call_addr(pool, self.SEL_TOKEN0)
            t1 = await self._call_addr(pool, self.SEL_TOKEN1)
            return t0, t1, await self._call_addr(pool, self.SEL_FACTORY)
        out = []
        for r in res:
            out.append(addr(r[-40:]) if r and r != "0x" and len(r) >= 66 else None)
        return out[0], out[1], out[2]
