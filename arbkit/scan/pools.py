"""Pool metadata for the classifier, including singleton pools (Uniswap V4, PancakeSwap Infinity) resolved from
their managers' Initialize events at the requested block."""

from __future__ import annotations

from eth_abi import decode, encode
from eth_utils import keccak

from mevlab.decode.lifecycle import decode_pool_initialize
from mevlab.decode.signatures import event_topic, selector
from mevlab.hexutil import to_int
from mevlab.pools.meta import PoolMeta, RpcPoolMeta
from mevlab.rpc import RpcError

UNISWAP_V4_INIT = "Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)"


class HistoricalPools(RpcPoolMeta):
    """Resolve singleton (Uniswap V4 / PancakeSwap Infinity) pools at the requested block from Initialize logs.

    `managers` maps each pool manager address to its registry entry, whose `init_event` names the layout.
    """

    def __init__(
        self, rpc, cache_path, factories, managers: dict[str, dict], max_log_range: int | None = None
    ):
        """max_log_range: None = one eth_getLogs from block 0 (archive providers index logs); otherwise only the
        newest `max_log_range` blocks are searched (a local full node scans receipts and limits the range), and
        older pools must come from `seed` or from the Initialize events `learn` sees in classified blocks."""
        super().__init__(rpc, cache_path, factories)
        self.managers = {k.lower(): v for k, v in managers.items()}
        self.max_log_range = max_log_range
        self.topics = {
            m: event_topic(e.get("init_event") or UNISWAP_V4_INIT) for m, e in self.managers.items()
        }

    def _remember(self, manager, log) -> bool:
        entry = self.managers[manager]
        init = decode_pool_initialize(log, entry.get("init_event") or UNISWAP_V4_INIT)
        if not init:
            return False
        meta = PoolMeta(
            init["currency0"],
            init["currency1"],
            entry.get("name", "uniswap_v4"),
            manager,
            init["fee"],
            init["hooks"],
        )
        key = f"{manager}:{init['pool_id']}".lower()
        self.misses.discard(key)
        self._put(key, meta)
        return True

    def learn(self, receipts) -> int:
        """Record every singleton pool initialized in these receipts (pools born while the follower runs)."""
        n = 0
        for receipt in receipts:
            for log in receipt.get("logs") or []:
                manager = str(log.get("address", "")).lower()
                topics = log.get("topics") or []
                if manager in self.topics and topics and str(topics[0]).lower() == self.topics[manager]:
                    n += self._remember(manager, log)
        return n

    async def seed(self, manager: str, start: int, end: int, chunk: int) -> int:
        """Index Initialize events of one manager over [start, end] in chunks of `chunk` blocks (halved on refusal)."""
        n, lo = 0, start
        while lo <= end:
            hi = min(lo + chunk - 1, end)
            try:
                logs = await self.rpc.get_logs(lo, hi, manager, [self.topics[manager]])
            except RpcError:
                if chunk > 500:
                    chunk //= 2
                    continue
                raise
            n += sum(self._remember(manager, log) for log in logs)
            lo = hi + 1
        self.save()
        return n

    async def _lookup_v4(self, manager, pool_id, key):
        entry = self.managers.get(manager)
        if entry is None:
            self.misses.add(key)
            return None
        signature = entry.get("init_event") or UNISWAP_V4_INIT
        end = to_int(self.rpc.block_tag)
        start = 0 if self.max_log_range is None else max(0, end - self.max_log_range + 1)
        try:
            logs = await self.rpc.get_logs(start, end, manager, [event_topic(signature), pool_id])
        except RpcError:
            self.misses.add(key)
            return None
        for log in logs:
            init = decode_pool_initialize(log, signature)
            if init and init["pool_id"].lower() == pool_id:
                meta = PoolMeta(
                    init["currency0"],
                    init["currency1"],
                    entry.get("name", "uniswap_v4"),
                    manager,
                    init["fee"],
                    init["hooks"],
                )
                self._put(key, meta)
                return meta
        meta = await self._position_manager_key(manager, entry, pool_id)
        if meta is not None:
            self._put(key, meta)
            return meta
        self.misses.add(key)
        return None

    async def _position_manager_key(self, manager, entry, pool_id) -> PoolMeta | None:
        """A pool whose Initialize log the node no longer holds (pruned receipts, born before the seed horizon): Uniswap
        V4's PositionManager stores `poolKeys(bytes25 poolId)` for every pool a position was minted in through it. Pool
        keys never change, so it is asked at the head, and the answer is accepted only when keccak256(abi.encode(key))
        reproduces the full 32-byte pool id. BSC 130965775 #4 (a V4 USDT loop, 1,562 BSC rows with unresolved V4 legs)
        had stayed "unknown" for want of the two pools' tokens."""
        position_manager = entry.get("position_manager")
        if not position_manager or not pool_id:
            return None
        pinned = self.rpc.block_tag
        self.rpc.block_tag = "latest"
        try:
            raw = await self.rpc.eth_call(
                position_manager, selector("poolKeys(bytes25)") + pool_id[2:52] + "0" * 14
            )
        except RpcError:
            return None
        finally:
            self.rpc.block_tag = pinned
        if not isinstance(raw, str) or len(raw) < 2 + 64 * 5:
            return None
        try:
            c0, c1, fee, spacing, hooks = decode(
                ["address", "address", "uint24", "int24", "address"], bytes.fromhex(raw[2 : 2 + 64 * 5])
            )
        except Exception:
            return None
        if int(c1, 16) == 0:
            return None  # unknown to the PositionManager
        if (
            "0x"
            + keccak(
                encode(["address", "address", "uint24", "int24", "address"], [c0, c1, fee, spacing, hooks])
            ).hex()
            != pool_id.lower()
        ):
            return None
        return PoolMeta(c0.lower(), c1.lower(), entry.get("name", "uniswap_v4"), manager, fee, hooks.lower())
