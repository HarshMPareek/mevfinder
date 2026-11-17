"""One block from the chain's node: the block with its transactions and receipts (checked to belong together) and
its call traces, cached on disk."""

from __future__ import annotations

from arbkit.files import read_json, write_json
from mevlab.hexutil import to_int
from mevlab.rpc import RpcError


def validate_bundle(block, receipts, number):
    if not isinstance(block, dict) or to_int(block.get("number")) != number:
        raise ValueError(f"Block {number} does not exist or RPC returned the wrong block.")
    if not block.get("hash") or not isinstance(receipts, list):
        raise ValueError(f"Block {number}: missing block hash or receipts.")
    transactions = block.get("transactions", [])
    if any(not isinstance(t, dict) for t in transactions):
        raise ValueError("RPC returned transaction hashes instead of full transactions.")
    if any(not isinstance(r, dict) for r in receipts):
        raise ValueError(f"Block {number}: missing transaction receipt.")
    txs = {t["hash"].lower(): t for t in transactions}
    rcs = {r["transactionHash"].lower(): r for r in receipts}
    if len(txs) != len(transactions) or len(rcs) != len(receipts) or txs.keys() != rcs.keys():
        raise ValueError(
            f"Block {number}: incomplete or duplicate receipts; refusing partial classification."
        )
    for i, tx in enumerate(transactions):
        receipt = rcs[tx["hash"].lower()]
        for obj in (tx, receipt):
            if (
                obj.get("blockHash", "").lower() != block["hash"].lower()
                or to_int(obj.get("blockNumber")) != number
                or to_int(obj.get("transactionIndex")) != i
            ):
                raise ValueError(
                    f"Block {number}: transaction/receipt belongs to another block or position (possible reorg)."
                )
    return rcs


def decode_extra_data(extra: str | None) -> str | None:
    """Printable text a block builder put in extraData (Ethereum builders sign their blocks this way:
    'Titan (titanbuilder.xyz)', 'beaverbuild.org', 'rsync-builder.xyz', 'BuilderNet (Flashbots)' …). None when
    the field is empty or not text (OP Stack, Nitro and BSC validators put binary data there)."""
    if not extra or extra == "0x":
        return None
    try:
        raw = bytes.fromhex(extra[2:])
    except ValueError:
        return None
    text = raw.decode("utf-8", "ignore").strip("\x00 \t\r\n")
    printable = "".join(c for c in text if c.isprintable())
    if len(printable) < 3 or len(printable) < 0.6 * len(raw):
        return None
    return printable[:64]


class BlockTraceRpc:
    """The trace RPC for one block. Tracing one transaction re-executes every transaction before it in the block, so a
    block with many candidates cost 0.2-0.4 s per candidate (Base, 2026-11-04: 24 s a block, the follower fell behind)
    while the node traces the whole block in about a second. After SINGLE_LIMIT single traces the whole block is traced
    once (by hash, so it is the same block) and the remaining candidates are served from it: the same callTracer frames.
    Anything the block trace cannot answer (an error, a node without txHash in block traces, a failed frame) falls back
    to the single-transaction call. Every other method passes through."""

    SINGLE_LIMIT = 3

    def __init__(self, rpc, block):
        self.rpc, self.block_hash = rpc, block["hash"]
        self.singles = 0
        self.traces: dict[str, dict] | None = None
        self.failed = False

    def __getattr__(self, name):
        return getattr(self.rpc, name)

    async def call(self, method, params=None):
        if method != "debug_traceTransaction" or not params:
            return await self.rpc.call(method, params)
        if self.traces is None and not self.failed and self.singles >= self.SINGLE_LIMIT:
            await self._load()
        hit = (self.traces or {}).get(str(params[0]).lower())
        if hit is not None:
            return hit
        self.singles += 1
        return await self.rpc.call(method, params)

    async def _load(self) -> None:
        try:
            out = await self.rpc.call(
                "debug_traceBlockByHash", [self.block_hash, {"tracer": "callTracer", "timeout": "60s"}]
            )
        except Exception:  # noqa: BLE001 - any failure: keep tracing one transaction at a time
            self.failed = True
            return
        traces = {}
        for item in out if isinstance(out, list) else []:
            tx_hash, frame = str((item or {}).get("txHash") or "").lower(), (item or {}).get("result")
            if tx_hash and isinstance(frame, dict) and frame.get("type") and frame.get("from"):
                traces[tx_hash] = frame
        self.traces = traces or None
        self.failed = not traces


async def fetch_trace(trace_rpc, tx_hash, path, refresh):
    if path and path.exists() and not refresh:
        return read_json(path)
    trace = await trace_rpc.call(
        "debug_traceTransaction", [tx_hash, {"tracer": "callTracer", "timeout": "20s"}]
    )
    if not isinstance(trace, dict) or not trace.get("type") or not trace.get("from"):
        raise ValueError("RPC returned an incomplete call trace")
    if path:
        write_json(path, trace)
    return trace


async def download_bundle(rpc, number):
    block = await rpc.get_block(number, True)
    if not block:
        raise ValueError(f"Block {number} was not found.")
    try:
        receipts = await rpc.call("eth_getBlockReceipts", [hex(number)])
    except RpcError as exc:
        if exc.code not in (-32601, -32600, -32602, -32004):
            raise
        receipts = None
    if receipts is None:
        hashes = [tx["hash"] for tx in block["transactions"]]
        try:
            receipts = await rpc.batch([("eth_getTransactionReceipt", [h]) for h in hashes])
        except RpcError:
            receipts = [await rpc.call("eth_getTransactionReceipt", [h]) for h in hashes]
    validate_bundle(block, receipts, number)
    return block, receipts
