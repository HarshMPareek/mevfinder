"""Block-level tracing: after a few single-transaction traces the whole block is traced once and the rest served from
it (the same frames); any failure of the block trace falls back to single traces; other methods pass through."""

import asyncio

from arbkit.scan.bundle import BlockTraceRpc


def frame(h):
    return {"type": "CALL", "from": "0xa", "to": "0xb", "input": h}


class FakeRpc:
    def __init__(self, block_result=None, block_error=False):
        self.calls, self.block_result, self.block_error = [], block_result, block_error
        self.stats = {"x": 1}

    async def call(self, method, params=None):
        self.calls.append(method)
        if method == "debug_traceBlockByHash":
            if self.block_error:
                raise RuntimeError("no")
            return self.block_result
        if method == "debug_traceTransaction":
            return frame(params[0])
        return "passthrough"


HASHES = [f"0x{i:064x}" for i in range(8)]


def run(rpc):
    wrapped = BlockTraceRpc(rpc, {"hash": "0xblock"})
    out = [asyncio.run(wrapped.call("debug_traceTransaction", [h, {"tracer": "callTracer"}])) for h in HASHES]
    return wrapped, out


def test_block_trace_serves_the_rest_with_the_same_frames():
    rpc = FakeRpc([{"txHash": h, "result": frame(h)} for h in HASHES])
    wrapped, out = run(rpc)
    assert out == [frame(h) for h in HASHES]
    assert rpc.calls.count("debug_traceTransaction") == BlockTraceRpc.SINGLE_LIMIT
    assert rpc.calls.count("debug_traceBlockByHash") == 1
    assert asyncio.run(wrapped.call("eth_chainId", [])) == "passthrough" and wrapped.stats == {"x": 1}


def test_block_trace_failures_fall_back_to_single_traces():
    for rpc in (
        FakeRpc(block_error=True),
        FakeRpc([{"result": frame(h)} for h in HASHES]),  # error / no txHash
        FakeRpc([{"txHash": h, "error": "timeout"} for h in HASHES]),
    ):  # failed frames
        _, out = run(rpc)
        assert out == [frame(h) for h in HASHES]
        assert rpc.calls.count("debug_traceBlockByHash") == 1  # tried once, never again for this block
        assert rpc.calls.count("debug_traceTransaction") == len(HASHES)


def test_a_transaction_missing_from_the_block_trace_is_traced_alone():
    rpc = FakeRpc([{"txHash": h, "result": frame(h)} for h in HASHES[:5]])
    _, out = run(rpc)
    assert out == [frame(h) for h in HASHES]
    assert rpc.calls.count("debug_traceTransaction") == BlockTraceRpc.SINGLE_LIMIT + 3
