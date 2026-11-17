"""Who the operator is and what it made: the group of addresses acting for the transaction's sender (callee,
executors, collectors), its flash loans, and its net result per asset - closed in one asset or not."""

from __future__ import annotations

from mevlab.trace.model import CLOSURE_TOL, EXACT_DUST_SHARE, FlashLoan, Movement
from mevlab.trace.movements import terminal_receivers


def pass_through_chain(
    start: str, movs: list[Movement], infra: set[str], group: set[str], term: set[str]
) -> tuple[str | None, set[str]]:
    """Follow an asset a venue paid to `start` through addresses that merely forward it (one outgoing movement
    carrying everything they received) to the terminal receiver. Returns (receiver, forwarders)."""
    r, chain, seen = start, set(), set()
    while r not in term and r not in group and r not in infra and r not in seen:
        seen.add(r)
        fwd = [x for x in movs if x.sender == r]
        got = [x for x in movs if x.receiver == r]
        if (
            len(fwd) != 1
            or any(x.token != fwd[0].token for x in got)
            or sum(x.amount for x in got) != fwd[0].amount
        ):
            return None, set()
        chain.add(r)
        r = fwd[0].receiver
    return (r, chain) if r in term else (None, set())


def party_group(
    seeds: list[str],
    movs: list[Movement],
    infra: set[str],
    stop: set[str] | None = None,
    never: set[str] | None = None,
) -> set[str]:
    """The operator: the seeds plus every non-venue address they exchange value with, transitively,
    stopping at terminal receivers (those are paid, not part of the machine) and at `stop` addresses
    (flash-loan lenders). Helper/executor contracts and the funding EOA fold into the group; their
    internal transfers then cancel out. Token contracts, hooks, pool managers and known payees never fold in."""
    term = terminal_receivers(movs) | (stop or set()) | ((never or set()) - set(seeds))
    group = {s for s in seeds if s and s not in infra}
    grew = True
    while grew:
        grew = False
        for m in movs:
            a, b = m.sender, m.receiver
            if a in infra or b in infra:
                continue
            if (a in group) != (b in group):
                other = b if a in group else a
                if other in term and other not in seeds:
                    continue
                group.add(other)
                grew = True
    return group


def flash_lenders(
    group: set[str],
    seeds: list[str],
    movs: list[Movement],
    infra: set[str],
) -> list[FlashLoan]:
    """Members of the group (other than the seeds) that first lend a token to the rest of the group and are
    repaid at least the principal in the same token later, touching nothing else: flash-loan lenders."""
    out: list[FlashLoan] = []
    for cand in sorted(group - set(seeds)):
        rest = group - {cand}
        sent: dict[str, int] = {}
        got: dict[str, int] = {}
        first_dir: str | None = None
        touches_other = False
        for m in movs:
            if m.sender == cand:
                if m.receiver in rest:
                    sent[m.token] = sent.get(m.token, 0) + m.amount
                    first_dir = first_dir or "lend"
                else:
                    touches_other = True
            elif m.receiver == cand:
                if m.sender in rest:
                    got[m.token] = got.get(m.token, 0) + m.amount
                    first_dir = first_dir or "borrow"
                else:
                    touches_other = True
        if touches_other or first_dir != "lend" or len(sent) != 1:
            continue
        tok = next(iter(sent))
        if set(got) - {tok} or got.get(tok, 0) < sent[tok]:
            continue
        out.append(FlashLoan(cand, tok, sent[tok], got.get(tok, 0)))
    return out


def party_view(
    group: set[str],
    movs: list[Movement],
    infra: set[str],
) -> tuple[dict[str, int], dict[str, int], dict[str, int], str | None]:
    """What the group took from and gave to the venues.
    venue_net: per token, received from venues minus sent to venues (the gain before distribution);
    venue_flow: per token, the larger of sent-to / received-from venues (closure tolerance base);
    payouts: what the group sent to addresses outside it (netted per counterparty and token);
    entry: the first token the group sent to a venue."""
    net: dict[str, int] = {}
    sent: dict[str, int] = {}
    got: dict[str, int] = {}
    pair: dict[tuple[str, str], int] = {}
    entry: str | None = None
    for m in movs:
        if m.sender in group and m.receiver in group:
            continue
        if m.sender in group:
            if m.receiver in infra:
                net[m.token] = net.get(m.token, 0) - m.amount
                sent[m.token] = sent.get(m.token, 0) + m.amount
                entry = entry or m.token
            else:
                pair[(m.token, m.receiver)] = pair.get((m.token, m.receiver), 0) + m.amount
        elif m.receiver in group:
            if m.sender in infra:
                net[m.token] = net.get(m.token, 0) + m.amount
                got[m.token] = got.get(m.token, 0) + m.amount
            else:
                pair[(m.token, m.sender)] = pair.get((m.token, m.sender), 0) - m.amount
    fl = {t: max(sent.get(t, 0), got.get(t, 0)) for t in set(sent) | set(got)}
    pay: dict[str, dict[str, int]] = {}
    for (tok, who), v in pair.items():
        if v > 0:
            pay.setdefault(tok, {})
            pay[tok][who] = pay[tok].get(who, 0) + v
    return net, fl, pay, entry


def closed_gain(
    net: dict[str, int], fl: dict[str, int], entry: str | None, *, exact: bool = False
) -> tuple[str, int, list[str]] | None:
    """(profit token, gain, positive residual tokens) if the party is closed: the profit token is the entry
    token when it ends positive beyond the tolerance (else the token with the largest gain relative to its
    own flow), no other
    token ends significantly negative; positive residuals beyond the tolerance are reported, not fatal
    (a second cycle in another token is also possible). With receipt-backed amounts, every negative
    residual fails closure; the legacy percentage tolerance only applies without receipts."""
    pos = {t: v for t, v in net.items() if v > 0}
    if not pos:
        return None
    if entry in pos and pos[entry] > CLOSURE_TOL * fl.get(entry, 0):
        tok = entry
    else:
        # a token the group only ever received (its gain is its whole flow) is a pass-through residue, never the
        # profit token while a traded token ends positive: 1.2e-9 ETHFI of dust had outranked +0.0121 USDC and was
        # priced at $2.33 from a leg (Arbitrum 512984150 #15)
        traded = [t for t in pos if pos[t] < fl.get(t, 0)]
        tok = max(traded or list(pos), key=lambda t: pos[t] / max(fl.get(t, 0), 1))
    residual: list[str] = []
    for t, v in net.items():
        if t == tok:
            continue
        if v < 0:
            dust = exact and -v <= fl.get(t, 0) * EXACT_DUST_SHARE
            if (exact and not dust) or (not exact and -v > CLOSURE_TOL * fl.get(t, 0)):
                return None
            if dust:
                residual.append(f"dust:{t}")
        if v > 0 and v > CLOSURE_TOL * fl.get(t, 0):
            residual.append(t)
    return tok, pos[tok], residual
