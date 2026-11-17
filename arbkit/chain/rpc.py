from __future__ import annotations

import asyncio

import httpx

from mevlab.rpc import JsonRpc, RpcError


class SafeRpc(JsonRpc):
    """Keep authenticated URLs and provider response bodies out of errors."""

    def __init__(self, url, **kwargs):
        super().__init__(url, retries=2, timeout=30, **kwargs)
        self.block_tag = "latest"

    async def _post(self, payload):
        for attempt in range(3):
            try:
                result = await super()._post(payload)
            except httpx.HTTPStatusError as exc:
                raise RpcError(
                    -exc.response.status_code, f"HTTP {exc.response.status_code}; check RPC access in .env"
                ) from None
            except httpx.TransportError:
                raise RpcError(-1, "RPC connection failed; check connectivity and .env") from None
            except ValueError:
                raise RpcError(-1, "RPC returned invalid JSON") from None
            except RpcError as exc:
                raise RpcError(
                    exc.code, "RPC request rejected; check provider access and method support"
                ) from None
            errors = [
                r.get("error")
                for r in (result if isinstance(result, list) else [result])
                if isinstance(r, dict) and r.get("error")
            ]
            if attempt < 2 and any(e.get("code") == 429 for e in errors):
                self.stats["retries"] += 1
                await asyncio.sleep(2**attempt)
                continue
            for e in errors:
                e["message"] = "RPC method unavailable or request rejected"
                e.pop("data", None)
            return result

    def _pin(self, method, params):
        if method == "eth_call" and len(params) > 1 and params[1] == "latest":
            return [params[0], self.block_tag]
        return params

    async def call(self, method, params=None):
        return await super().call(method, self._pin(method, params or []))

    async def batch(self, reqs, lenient=False):
        return await super().batch([(m, self._pin(m, p)) for m, p in reqs], lenient=lenient)
