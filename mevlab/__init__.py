"""mevlab: the chain-agnostic classifier core.

One transaction goes through these stages; each package reads one kind of evidence:

    decode/    receipt logs        -> swap legs, token transfers, pool events            (what the logs say)
    pools/     pool metadata, prices -> tokens of a pool, pre-trade prices, invariants    (what the pools are)
    receipt/   one receipt          -> ArbLabel: legs, path, who gained, a class          (the event layer)
    trace/     one call trace       -> TraceVerdict: venues, operator, closed gain, route (the trace layer)
    block/     the block's order    -> trigger and position (backrun, top of block, ...)

The event layer proposes, the trace layer verifies: a cycle is counted only when the call trace shows a closed chain in
one asset through distinct called venues. Chain-specific behaviour (fees, ordering, registries) lives in `arbkit` and
`chains/<chain>/`, never here.
"""
