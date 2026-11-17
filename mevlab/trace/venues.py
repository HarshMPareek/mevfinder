"""Which addresses traded: a venue is an address other than the operator that takes one asset and gives a different
one. Its legs are the (in, out) pairs in trace order; pass-through helpers, hook fee takers and RFQ makers are told
apart here."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from mevlab.decode.signatures import ERC6909_TRANSFER_TOPIC, HOOK_FEE_EVENTS, SINGLETON_SWAP_TOPICS
from mevlab.hexutil import addr
from mevlab.trace.model import Leg, Movement
from mevlab.trace.movements import nets, terminal_receivers


def venues_of(movs: list[Movement], exclude: set[str], called: set[str] | None = None) -> list[str]:
    """Swap counterparties: addresses that received one asset and sent a different one, did not merely pass
    everything through (some asset nets non-zero), and — when `called` is given — were called in the trace
    (a contract; an EOA whose tokens moved by allowance is an order's maker, not a venue). In order of first
    appearance."""
    net = nets(movs)
    recv: dict[str, set[str]] = {}
    sent: dict[str, set[str]] = {}
    for m in movs:
        recv.setdefault(m.receiver, set()).add(m.token)
        sent.setdefault(m.sender, set()).add(m.token)
    seen: list[str] = []
    for m in movs:
        for a in (m.receiver, m.sender):
            if a in exclude or a in seen or (called is not None and a not in called):
                continue
            r, s_ = recv.get(a, set()), sent.get(a, set())
            if r and s_ and (r | s_) != r & s_ or (r and s_ and len(r | s_) > 1):
                if any(v != 0 for v in net.get(a, {}).values()):
                    seen.append(a)
    return seen


def conduits(
    movs: list[Movement], venues: list[str], seeds: set[str], hints: set[str], tol_bps: int = 100
) -> set[str]:
    """Venue candidates that are really the operator's helpers: an address that received an asset from a venue
    and forwarded (almost) the same amount of that asset to the operator, and took nothing else. A pool takes
    one asset and gives another; a pass-through helper funds the route from its own inventory and relays the
    output, which would otherwise close a phantom cycle worth the whole output (Ethereum 0x0c1e551a…, 7.45 WETH
    "cycle" that was a sandwich back-leg selling inventory). Event-established venues (`hints`) are never
    conduits; a venue returning more of an asset than it received is a profit source, not a conduit."""
    out: set[str] = set()
    for v in venues:
        if v in hints:
            continue
        ins: dict[str, int] = defaultdict(int)
        ins_from_others: dict[str, int] = defaultdict(
            int
        )  # received from venues / third parties, not the operator
        outs: dict[str, int] = defaultdict(int)
        to_seed: dict[str, int] = defaultdict(int)
        for m in movs:
            if m.receiver == v:
                ins[m.token] += m.amount
                if m.sender not in seeds:
                    ins_from_others[m.token] += m.amount
            if m.sender == v:
                outs[m.token] += m.amount
                if m.receiver in seeds:
                    to_seed[m.token] += m.amount
        # A pass-through: what the operator got back is what the helper received from elsewhere, less at most
        # tol_bps. A pool in a same-pool round trip receives the asset from the operator itself and is not one.
        passthrough = {
            t
            for t, got in ins_from_others.items()
            if got > 0
            and to_seed.get(t, 0) > 0
            and to_seed[t] <= got
            and got - to_seed[t] <= got * tol_bps // 10_000
        }
        takes = {t for t, got in ins.items() if t not in passthrough and got > outs.get(t, 0)}
        if passthrough and not takes:
            out.add(v)
    return out


def legs_of(movs: list[Movement], venues: list[str]) -> list[Leg]:
    """One leg per (in, out) pair at a venue, split in trace order when a side repeats."""
    vset = set(venues)
    legs: list[Leg] = []
    cur: dict[str, Leg] = {}
    for m in movs:
        if m.kind == "receipt_side_effect":
            # a token's tax cut out of a venue's payment is not what the venue paid: the presale 0x1ee37b87's leg had
            # read 24.1 TRII (the tax to the token contract) instead of the 772.1 TRII it sent, and priced TRII 30x
            # too high (Ethereum 26318511 #40)
            continue
        for v, side in ((m.receiver, "in"), (m.sender, "out")):
            if v not in vset:
                continue
            lg = cur.get(v)
            tok_attr, amt_attr = ("token_in", "amount_in") if side == "in" else ("token_out", "amount_out")
            other_attr = "token_out" if side == "in" else "token_in"
            if lg is not None:
                have = getattr(lg, tok_attr)
                if have is not None and (have != m.token or getattr(lg, other_attr) is not None):
                    lg = None  # a repeated side after a complete pair, or a new asset on this side: new leg
            if lg is None:
                lg = Leg(venue=v, order=m.order)
                cur[v] = lg
                legs.append(lg)
            setattr(lg, tok_attr, m.token)
            setattr(lg, amt_attr, getattr(lg, amt_attr) + m.amount)
    return sorted(legs, key=lambda x: x.order)


def singleton_legs(movs: list[Movement], singletons: set[str]) -> list[Leg]:
    """A singleton manager (Uniswap V4, Ekubo, PancakeSwap Infinity) hosts every pool of a route and settles
    only the net under flash accounting: takes precede settles, so movement order cannot pair its legs. The
    manager is represented by its net flows instead: one leg per (token it netted in, token it netted out)."""
    legs: list[Leg] = []
    for v in singletons:
        net: dict[str, int] = defaultdict(int)
        first: int | None = None
        for m in movs:
            if v in (m.sender, m.receiver):
                first = m.order if first is None else first
            if m.receiver == v:
                net[m.token] += m.amount
            if m.sender == v:
                net[m.token] -= m.amount
        ins = {t: a for t, a in net.items() if a > 0}
        outs = {t: -a for t, a in net.items() if a < 0}
        for tok_in, amt_in in ins.items():
            for tok_out, amt_out in outs.items():
                lg = Leg(venue=v, order=first or 0)
                lg.token_in, lg.amount_in, lg.token_out, lg.amount_out = tok_in, amt_in, tok_out, amt_out
                legs.append(lg)
    return legs


def hook_fee_recipients(
    movs: list[Movement], logs: list[dict[str, Any]], infra: set[str], weth: str
) -> set[str]:
    """Hook fee events (rules.HOOK_FEE_EVENTS) state the fee kept per currency. The terminal receivers a manager
    paid in that currency whose receipts add up to the fee, to the wei, are the hook's fee recipients (a hook's
    splitter pays several); they are the venue's payees, never the operator's collector."""
    zero = "0x" + "0" * 40
    fees: dict[str, int] = defaultdict(int)
    for lg in logs or []:
        topics = lg.get("topics") or []
        kind = HOOK_FEE_EVENTS.get(str(topics[0]).lower()) if topics else None
        if kind is None:
            continue
        data = str(lg.get("data") or "0x")[2:]
        words = [data[i : i + 64] for i in range(0, len(data), 64)]
        if kind == "pons" and len(words) >= 3:
            fees[addr("0x" + words[0][-40:])] += int(words[1], 16) + int(words[2], 16)
        elif kind == "splitter" and len(topics) >= 2 and len(words) >= 1:
            fees[addr("0x" + str(topics[1])[-40:])] += int(words[0], 16)
    if not fees:
        return set()
    term = terminal_receivers(movs)
    out: set[str] = set()
    for cur, fee in fees.items():
        token = weth if cur == zero else cur
        paid: dict[str, int] = defaultdict(int)
        for m in movs:
            if m.sender in infra and m.receiver in term and m.token == token:
                paid[m.receiver] += m.amount
        items = sorted(paid.items(), key=lambda kv: -kv[1])[:8]

        def subset(i: int, remaining: int, chosen: tuple) -> tuple | None:
            if remaining == 0:
                return chosen
            if i >= len(items) or remaining < 0:
                return None
            return subset(i + 1, remaining - items[i][1], chosen + (items[i][0],)) or subset(
                i + 1, remaining, chosen
            )

        found = subset(0, fee, ())
        if found:
            out |= set(found)
    return out


def swap_lockers(logs: list[dict[str, Any]] | None) -> set[str]:
    """The `sender` of every singleton Swap event: the contract that holds the manager's lock (the operator's
    executor, or a router), never a hook."""
    out: set[str] = set()
    for lg in logs or []:
        topics = lg.get("topics") or []
        if len(topics) >= 3 and str(topics[0]).lower() in SINGLETON_SWAP_TOPICS:
            out.add(addr("0x" + str(topics[2])[-40:]))
    return out


def maker_candidates(movs: list[Movement], called: set[str], exclude: set[str], hints: set[str]) -> set[str]:
    """Addresses that received one asset and sent a different one without ever being called: market makers
    whose quote a called settlement contract filled by allowance (RFQ / order-book legs), or users whose orders a
    solver filled. Under the method an uncalled counterparty is not a venue; the row is reported, not counted."""
    recv: dict[str, set[str]] = defaultdict(set)
    sent: dict[str, set[str]] = defaultdict(set)
    for m in movs:
        recv[m.receiver].add(m.token)
        sent[m.sender].add(m.token)
    return {
        a
        for a in set(recv) & set(sent)
        if a not in called and a not in exclude and a not in hints and recv[a] != sent[a]
    }


def foreign_claim_recipients(logs: list[dict[str, Any]] | None, party: set[str]) -> set[str]:
    """Recipients of ERC-6909 claim mints whose `caller` is outside the party: a hook (or its splitter) keeping
    a fee as a claim. The party's own mints are its result taken to a collector."""
    out: set[str] = set()
    zero = "0x" + "0" * 40
    for lg in logs or []:
        topics = lg.get("topics") or []
        if len(topics) != 4 or str(topics[0]).lower() != ERC6909_TRANSFER_TOPIC:
            continue
        if addr("0x" + str(topics[1])[-40:]) != zero:
            continue
        data = str(lg.get("data") or "0x")[2:]
        if len(data) < 128:
            continue
        caller = addr("0x" + data[:64][-40:])
        to = addr("0x" + str(topics[2])[-40:])
        if caller not in party and to not in party:
            out.add(to)
    return out
