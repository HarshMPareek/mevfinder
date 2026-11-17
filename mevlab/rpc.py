"""Minimal async JSON-RPC client with batching and backoff. No web3.py: we control every call."""

from __future__ import annotations

import asyncio
import itertools
import os
import time
from typing import Any

import httpx

RETRYABLE = {429, 500, 502, 503, 504}


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(f"rpc error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


class JsonRpc:
    def __init__(
        self,
        url: str,
        *,
        timeout: float = 30.0,
        max_batch: int = 100,
        retries: int = 8,
        http2: bool = True,
        min_interval: float | None = None,
    ):
        """min_interval: minimum seconds between HTTP requests (public endpoints rate-limit hard).
        Defaults to env RPC_MIN_INTERVAL or 0."""
        self.url = url
        self.max_batch = max_batch
        self.retries = retries
        self.min_interval = (
            float(os.environ.get("RPC_MIN_INTERVAL", "0") or 0) if min_interval is None else min_interval
        )
        self._last_send = 0.0
        self._lock = asyncio.Lock()
        self._ids = itertools.count(1)
        self._client = httpx.AsyncClient(timeout=timeout, http2=http2)
        self.stats = {"calls": 0, "batches": 0, "retries": 0}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> JsonRpc:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        async with self._lock:
            wait = self._last_send + self.min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_send = time.monotonic()

    async def _post(self, payload: Any) -> Any:
        delay = 1.0
        for attempt in range(self.retries + 1):
            try:
                await self._throttle()
                r = await self._client.post(self.url, json=payload)
                if r.status_code in RETRYABLE and attempt < self.retries:
                    raise httpx.HTTPStatusError("retryable", request=r.request, response=r)
                if r.status_code >= 400:
                    # Providers often return a JSON-RPC error body with a 4xx (e.g. publicnode 403
                    # "Archive requests require a personal token"). Surface it as RpcError.
                    try:
                        body = r.json()
                    except ValueError:
                        body = None
                    if isinstance(body, dict) and "error" in body:
                        e = body["error"]
                        raise RpcError(e.get("code", -r.status_code), e.get("message", ""), e.get("data"))
                    if isinstance(body, list) and body and isinstance(body[0], dict) and "error" in body[0]:
                        e = body[0]["error"]
                        raise RpcError(e.get("code", -r.status_code), e.get("message", ""), e.get("data"))
                r.raise_for_status()
                return r.json()
            except (httpx.TransportError, httpx.HTTPStatusError):
                if attempt >= self.retries:
                    raise
                self.stats["retries"] += 1
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)
        raise RuntimeError("unreachable")

    async def call(self, method: str, params: list[Any] | None = None) -> Any:
        self.stats["calls"] += 1
        res = await self._post(
            {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params or []}
        )
        if isinstance(res, dict) and "error" in res:
            e = res["error"]
            raise RpcError(e.get("code", -1), e.get("message", ""), e.get("data"))
        return res["result"]

    async def batch(self, reqs: list[tuple[str, list[Any]]], lenient: bool = False) -> list[Any]:
        """Batched JSON-RPC. With lenient=True, per-item errors become None instead of raising."""
        out: list[Any] = []
        for i in range(0, len(reqs), self.max_batch):
            chunk = reqs[i : i + self.max_batch]
            payload = [
                {"jsonrpc": "2.0", "id": k, "method": m, "params": p} for k, (m, p) in enumerate(chunk)
            ]
            self.stats["batches"] += 1
            self.stats["calls"] += len(chunk)
            res = await self._post(payload)
            if isinstance(res, dict):
                e = res.get("error", {})
                raise RpcError(e.get("code", -1), e.get("message", str(res)))
            by_id = {r["id"]: r for r in res}
            for k in range(len(chunk)):
                r = by_id.get(k)
                if r is None:
                    raise RpcError(-1, f"missing response for batch id {k}")
                if "error" in r:
                    if lenient:
                        out.append(None)
                        continue
                    e = r["error"]
                    raise RpcError(e.get("code", -1), e.get("message", ""), e.get("data"))
                out.append(r["result"])
        return out

    # --- helpers -------------------------------------------------------------
    async def chain_id(self) -> int:
        return int(await self.call("eth_chainId"), 16)

    async def block_number(self) -> int:
        return int(await self.call("eth_blockNumber"), 16)

    async def get_block(self, n: int | str, full_txs: bool = True) -> dict[str, Any]:
        tag = hex(n) if isinstance(n, int) else n
        return await self.call("eth_getBlockByNumber", [tag, full_txs])

    async def get_logs(
        self,
        from_block: int,
        to_block: int,
        address: str | list[str] | None = None,
        topics: list[Any] | None = None,
    ) -> list[dict[str, Any]]:
        f: dict[str, Any] = {"fromBlock": hex(from_block), "toBlock": hex(to_block)}
        if address:
            f["address"] = address
        if topics:
            f["topics"] = topics
        return await self.call("eth_getLogs", [f])

    async def get_code(self, address: str, tag: str = "latest") -> str:
        return await self.call("eth_getCode", [address, tag])

    async def eth_call(self, to: str, data: str, tag: str | int = "latest") -> str:
        t = hex(tag) if isinstance(tag, int) else tag
        return await self.call("eth_call", [{"to": to, "data": data}, t])
