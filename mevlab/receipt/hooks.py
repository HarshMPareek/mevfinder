"""Uniswap V4 hooks that keep part of a swap. The Swap event states the pool's delta net of the hook's cut, so the
swapper paid more (or received less) than the event says. These corrections run on the decoded legs before anything
reads their amounts."""

from __future__ import annotations

from mevlab.decode.signatures import ERC6909_TRANSFER_TOPIC, HOOK_FEE_EVENTS
from mevlab.hexutil import addr, to_int


def apply_hook_claims(legs, logs, group: set, hooks: set | None = None) -> int:
    """C-CLS-16 at event level. A V4 hook that keeps part of a swap mints itself an ERC-6909 claim in that
    currency (PoolManager Transfer(caller, 0x0, hook_holding, id=currency, amount)); the Swap event carries
    the pool's delta net of that claim, so the swapper paid more (input side) or received less (output side)
    than the event says. Attribute each small mint (≤ 5 % of the leg) to a non-party recipient to the
    nearest V4 leg of the same PoolManager in that currency and correct the leg's amount. A mint whose
    `caller` is the party itself is the operator taking its result to a collector as a claim (Ethereum
    EIP-7702 searchers paying their vault), not a fee. Returns the number applied."""
    party = {a for a in group if a}
    party |= {x.sender for x in legs if x.sender} | {x.recipient for x in legs if x.recipient}
    v4 = [x for x in legs if x.kind == "clmm_v4_singleton"]
    if not v4:
        return 0
    zero = "0x" + "0" * 40
    applied = 0
    for lg in logs:
        topics = lg.get("topics") or []
        if len(topics) != 4 or str(topics[0]).lower() != ERC6909_TRANSFER_TOPIC:
            continue
        frm = addr("0x" + str(topics[1])[-40:])
        if frm != zero:
            continue  # only mints
        to = addr("0x" + str(topics[2])[-40:])
        if to in party:
            continue  # the party's own claims (profit kept as claims) are not a fee
        cur = addr("0x" + str(topics[3])[-40:])
        data = str(lg.get("data") or "0x")[2:]
        if len(data) < 128:
            continue
        caller = addr("0x" + data[:64][-40:])  # ERC-6909 Transfer(caller, from, to, id, amount): who minted
        if caller in party:
            continue  # the party minted the claim itself (profit taken to its collector): a payout, not a fee
        amount = int(data[64:128], 16)
        pm = addr(lg["address"])
        idx = to_int(lg.get("logIndex", 0))
        cands = [x for x in v4 if x.pool == pm and cur in (x.token_in, x.token_out)]
        if not cands or amount <= 0:
            continue
        leg = min(cands, key=lambda x: abs(x.log_index - idx))
        if cur == leg.token_in and 0 < amount <= leg.amount_in // 20:
            leg.amount_in += amount  # the swapper paid this on top of the pool's delta
            applied += 1
        elif cur == leg.token_out and 0 < amount <= leg.amount_out // 20:
            leg.amount_out -= amount  # the swapper received this much less
            applied += 1
    return applied


def apply_hook_cuts(legs, logs, transfers, party: set, pools: set, hooks_by_key: dict) -> list[str]:
    """C-CLS-30 at event level. A V4 hook (or its fee splitter) that keeps part of a swap is paid by the
    PoolManager directly, inside the Swap event's delta: native ETH by CALL (no log at all) or the
    output ERC-20 by transfer. Two sources of evidence, in this order, never both for one (leg, currency):
      1. the hook's own fee event (rules.HOOK_FEE_EVENTS), exact to the wei;
      2. an ERC-20 Transfer from the PoolManager to the pool's registered hook in the leg's currency,
         capped at 50 % of the leg.
    Nothing else is netted: a transfer from the PoolManager to any other address cannot be told apart,
    inside one transaction, from a bot taking its profit slice straight to its collector (a V4 take to a
    third party), so an unregistered fee recipient without an event is a known gap, not a guess. A hooked
    leg with native output and neither kind of evidence is flagged: the trace pass decides.
    Returns the reasons to append."""
    v4 = [x for x in legs if x.kind == "clmm_v4_singleton"]
    if not v4:
        return []
    zero = "0x" + "0" * 40
    pm_addrs = {x.pool for x in v4}
    token_contracts = {t.token for t in transfers}
    done: set[tuple[int, str]] = set()
    notes: list[str] = []

    def apply(leg, cur: str, fee: int, cap_div: int, tag: str) -> bool:
        if fee <= 0:
            return False
        if cur == leg.token_out and fee <= leg.amount_out // cap_div:
            leg.amount_out -= fee
        elif cur == leg.token_in and fee <= leg.amount_in // cap_div:
            leg.amount_in += fee
        else:
            return False
        done.add((id(leg), cur))
        notes.append(f"{tag}:{fee}")
        return True

    # 1. fee events
    for lg in logs:
        topics = lg.get("topics") or []
        if not topics:
            continue
        kind = HOOK_FEE_EVENTS.get(str(topics[0]).lower())
        if kind is None:
            continue
        data = str(lg.get("data") or "0x")[2:]
        words = [data[i : i + 64] for i in range(0, len(data), 64)]
        idx = to_int(lg.get("logIndex", 0))
        if kind == "pons" and len(topics) >= 2 and len(words) >= 3:
            pid = str(topics[1]).lower()
            cur = addr("0x" + words[0][-40:])
            fee = int(words[1], 16) + int(words[2], 16)
            cands = [x for x in v4 if (x.pool_id or "").lower() == pid]
        elif kind == "splitter" and len(topics) >= 2 and len(words) >= 1:
            cur = addr("0x" + str(topics[1])[-40:])
            fee = int(words[0], 16)
            cands = [x for x in v4 if cur in (x.token_in, x.token_out)]
        else:
            continue
        cands = [x for x in cands if (id(x), cur) not in done]
        if not cands:
            continue
        leg = min(cands, key=lambda x: abs(x.log_index - idx))
        apply(leg, cur, fee, 2, "v4_hook_fee_event")
    # 2. PoolManager -> registered hook transfers in a leg's currency
    hook_addrs = {h for h in hooks_by_key.values() if h and h != zero}
    for t in transfers:
        if t.sender not in pm_addrs or t.receiver not in hook_addrs or t.receiver in party:
            continue
        if t.receiver in pools or t.receiver in token_contracts:
            continue
        cands = [
            x
            for x in v4
            if x.pool == t.sender
            and t.token in (x.token_in, x.token_out)
            and hooks_by_key.get(x.key) == t.receiver
            and (id(x), t.token) not in done
        ]
        if not cands:
            continue
        leg = min(cands, key=lambda x: abs(x.log_index - t.log_index))
        apply(leg, t.token, t.value, 2, "v4_hook_paid_by_pm")
    # 3. hooked legs with native output and no evidence either way
    unverified = sum(
        1
        for x in v4
        if hooks_by_key.get(x.key) not in (None, zero) and x.token_out == zero and (id(x), zero) not in done
    )
    if unverified:
        notes.append(f"hooked_v4_native_unverified:{unverified}")
    return notes
