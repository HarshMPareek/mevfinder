"""Block-level sandwich detection from receipts: the same operator brackets another party's swap on one pool.

Front and back are sent by the same wallet (the operator is the contract it calls when both legs call the same
one). Two wallets driving one contract are two users of a router: of 12 brackets rejected on 2026-10-09, 9 were
KyberSwap, Uniswap V2 Router and other public routers used by unrelated wallets, and none of the 41 confirmed
sandwiches had different wallets. The victim swaps the same pool between them within SANDWICH_MAX_GAP indices. The bundle is confirmed only when the pool's Swap logs
show the front trading in the victim's direction and the back trading against it; a pool whose swap event is not
decoded gets its direction from its own token transfers (`_transfer_direction`; found by the bracket screen on a
DODO USDT/USDC sandwich, Ethereum 26139154).

The legs must be swaps: a liquidity action (add → victim swap → remove is JIT liquidity, another family) or a
counted cycle (a backrun after the victim is a backrun; counting it again as a sandwich would double count) is
never a sandwich leg. The bundle must also be a round trip (`round_trip_verdict`): over front + back no token's
net change may exceed ROUND_TRIP_MAX_SHARE of that token's volume through the operator. A bot that spends
stablecoin inventory at the top of the block and sells WETH a few positions later, or buys a token and keeps
most of it, brackets a victim by coincidence; its result is a position change, not a sandwich profit.

The operator's result is measured from receipt Transfer logs (and WETH deposit/withdraw) over the front and
back transactions, plus native value from the call traces when the trace pass fetched them. It is reported in
the token with the largest raw gain here; the valuation stage (`pricing.value_sandwich`) then sums every priced
net change and shows the largest priced gain as the profit token. Ownership of the wallet/contract pair is
inferred from the bracket, not proven.
"""

from __future__ import annotations

from collections import defaultdict

from mevlab.decode.signatures import (
    SIG_INFINITY_CL_SWAP,
    SIG_V4_SWAP,
    TRANSFER_TOPIC,
    WETH_DEPOSIT_TOPIC,
    WETH_WITHDRAWAL_TOPIC,
    event_topic,
)
from mevlab.hexutil import addr
from mevlab.trace.cashflows import native_flows

V2_SWAP_TOPIC = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
V3_SWAP_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
PANCAKE_V3_SWAP_TOPIC = "0x19b47279256b2a23a1665c810c8d55a1758940ee09377d4f8d26497a3577dc83"
V4_SWAP_TOPIC = event_topic(SIG_V4_SWAP)
# PancakeSwap Infinity CL (BSC, Base): Uniswap V4's layout and sign (user perspective) plus protocolFee. Missing here,
# a victim on an Infinity pool was never confirmed: 46 of the 307 BSC 0x070f82ae sandwiches (2026-11-08).
INFINITY_CL_SWAP_TOPIC = event_topic(SIG_INFINITY_CL_SWAP)
SOLIDLY_SWAP_TOPIC = "0xb3e2773606abfd36b5bd91394b3a54d1398336c65005baf7bf7a05efeffaf75b"
DIRECTIONAL_TOPICS = {
    V2_SWAP_TOPIC,
    V3_SWAP_TOPIC,
    PANCAKE_V3_SWAP_TOPIC,
    V4_SWAP_TOPIC,
    INFINITY_CL_SWAP_TOPIC,
    SOLIDLY_SWAP_TOPIC,
}
SANDWICH_MAX_GAP = 6
ROUND_TRIP_MAX_SHARE = 0.25  # |net change| / volume of a token through the operator over front + back
# Calibration (Ethereum 2026-10-09, 48 confirmed brackets): the 39 round trips peak at 0.19 (most < 0.1); the 8
# position changes sit at 1.00; a Base inventory sale that bracketed a victim measured 0.497.
LEG_CLASSES_EXCLUDED = {"lp_action"}
LEG_ASSESSMENTS_EXCLUDED = {"trace_supported", "trace_supported_loss"}


def can_be_leg(row: dict) -> bool:
    """Front/back candidates: swap rows that are neither liquidity actions nor counted cycles."""
    return (
        bool(row.get("venues"))
        and not row["fees"]["is_system"]
        and row.get("arb_class") not in LEG_CLASSES_EXCLUDED
        and row.get("assessment") not in LEG_ASSESSMENTS_EXCLUDED
    )


def _int(word: str, signed: bool = True) -> int:
    value = int(word, 16)
    return value - (1 << 256) if signed and value >= (1 << 255) else value


def swap_direction(topic0: str, data_hex: str) -> int:
    """+1 when token0 went into the pool, -1 when token0 came out, 0 when unknown."""
    data = data_hex[2:] if data_hex.startswith("0x") else data_hex
    if len(data) < 128:
        return 0
    if topic0 in (V3_SWAP_TOPIC, PANCAKE_V3_SWAP_TOPIC):
        a0 = _int(data[:64])
        return (a0 > 0) - (a0 < 0)
    if topic0 in (
        V4_SWAP_TOPIC,
        INFINITY_CL_SWAP_TOPIC,
    ):  # user perspective: negative amount0 = token0 into the pool
        a0 = _int(data[:64])
        return (a0 < 0) - (a0 > 0)
    if topic0 == V2_SWAP_TOPIC:
        a0in, a1in = _int(data[:64], False), _int(data[64:128], False)
        return 1 if a0in > 0 else (-1 if a1in > 0 else 0)
    if topic0 == SOLIDLY_SWAP_TOPIC:
        a0in, a1in = _int(data[:64], False), _int(data[64:128], False)
        return 1 if a0in > 0 else (-1 if a1in > 0 else 0)
    return 0


def _pool_directions(logs: list[dict], pool_key: str) -> list[int]:
    pool_addr, _, pool_id = pool_key.partition(":")
    out = []
    for log in logs:
        topics = log.get("topics") or []
        if (
            not topics
            or addr(log["address"]) != pool_addr.lower()
            or topics[0].lower() not in DIRECTIONAL_TOPICS
        ):
            continue
        if pool_id and (len(topics) < 2 or topics[1].lower() != pool_id.lower()):
            continue
        out.append(swap_direction(topics[0].lower(), log.get("data", "0x")))
    if not out and not pool_id:
        direction = _transfer_direction(logs, pool_addr.lower())
        if direction:
            out.append(direction)
    return out


def _transfer_direction(logs: list[dict], pool_addr: str) -> int:
    """Direction from the pool's own ERC-20 flows when its swap event is not one of DIRECTIONAL_TOPICS (DODO PMM,
    Curve, Balancer and other venues the receipts show only as transfers): +1 when the pool received the
    lower-addressed of its two tokens, -1 when it sent it, 0 unless exactly one token came in and one went out."""
    received, sent = set(), set()
    for token, sender, receiver in _transfers(logs):
        if receiver == pool_addr:
            received.add(token)
        if sender == pool_addr:
            sent.add(token)
    if len(received) != 1 or len(sent) != 1 or received == sent:
        return 0
    return 1 if min(received | sent) in received else -1


def find_sandwiches(rows: list[dict], receipts_by_hash: dict[str, dict]) -> list[dict]:
    """Confirmed sandwiches in one block from the rows' event venues and the receipts' Swap logs."""
    swappers = sorted(
        (r for r in rows if r.get("venues") and not r["fees"]["is_system"]), key=lambda r: r["tx_index"]
    )
    out = []
    taken: set[str] = set()
    for i, front in enumerate(swappers):
        if front["tx_hash"] in taken or not can_be_leg(front):
            continue
        for k in range(i + 2, min(i + SANDWICH_MAX_GAP + 1, len(swappers))):
            back = swappers[k]
            if back["tx_hash"] in taken or not can_be_leg(back):
                continue
            if not (front["tx_from"] and front["tx_from"] == back["tx_from"]):
                continue
            same_contract = bool(front["tx_to"]) and front["tx_to"] == back["tx_to"]
            shared = set(front["venues"]) & set(back["venues"])
            if not shared:
                continue
            victims = [
                v
                for v in swappers[i + 1 : k]
                if v["tx_from"] != front["tx_from"]
                and v["tx_to"] != front["tx_to"]
                and set(v["venues"]) & shared
            ]
            if not victims:
                continue
            confirmed_pool = None
            for pool in sorted(shared):
                da = _pool_directions(receipts_by_hash[front["tx_hash"]].get("logs", []), pool)
                dc = _pool_directions(receipts_by_hash[back["tx_hash"]].get("logs", []), pool)
                dv = [
                    d
                    for v in victims
                    for d in _pool_directions(receipts_by_hash[v["tx_hash"]].get("logs", []), pool)
                ]
                if da and dv and dc and da[0] != 0 and da[0] == dv[0] and dc[0] == -da[0]:
                    confirmed_pool = pool
                    break
            if confirmed_pool is None:
                continue
            operator = front["tx_to"] if same_contract else front["tx_from"]
            bundle = {
                "block_number": front["block_number"],
                "bundle_id": f"{front['block_number']}:{front['tx_index']}",
                "front": front["tx_hash"],
                "back": back["tx_hash"],
                "victims": [v["tx_hash"] for v in victims if set(v["venues"]) & {confirmed_pool}]
                or [victims[0]["tx_hash"]],
                "pool": confirmed_pool,
                "operator": operator,
                "operator_kind": "contract" if same_contract else "wallet",
            }
            out.append(bundle)
            taken.update({front["tx_hash"], back["tx_hash"], *bundle["victims"]})
            break
    return out


def lost_cycle(row: dict) -> bool:
    """A cycle that lost: a losing cycle the trace confirmed (`trace_supported_loss`), or a cycle-shaped route the event
    layer saw lose (`attempted_cycle` with `cycle_lost`; it stays there when the operator also gained a little of
    another token, `beneficiary_mixed_sign`). Ethereum 26334741: #0 lost 12.26 WETH through two USDC/WETH pools as an
    attempted cycle, #1 a Safe bought XAUt with $889,699 USDC through one of them, #2 reversed the route for +15.95 WETH:
    a $8,441 sandwich (the bot kept ~$177 after an $8,260 builder tip) that had counted as a $36,491 atomic backrun."""
    if row.get("assessment") == "trace_supported_loss":
        return True
    return row.get("arb_class") == "attempted_cycle" and "cycle_lost" in (row.get("reasons") or [])


HOLDER_DUST_SHARE = 0.01  # a holder's gain in a front leg up to 1 % of the token's volume is rounding dust


def holder_paid(
    logs: list[dict], holder: str, pools: set[str], trace: dict | None = None, weth: str | None = None
) -> bool:
    """True when `holder` paid for this transaction's trades: it swapped with one of `pools` and its token nets over
    the receipt are a loss (at least one token down, none up beyond rounding dust). This is what makes another
    sender's transaction the operator's own front leg: only the operator's code can spend the operator's tokens.

    Native value is the wrapped token, from the call trace when there is one: a front that pays BNB into a V4-style
    pool shows only its WBNB proceeds in the receipt (BSC 131709373 #14: BNB -> USDT on the V4 pool manager, USDT ->
    0.1055 WBNB on a V2 pair), which alone reads as a pure gain."""
    holder = holder.lower()
    net: dict[str, int] = defaultdict(int)
    moved: dict[str, int] = defaultdict(int)
    traded_pool = False
    for log in logs:
        topics = log.get("topics") or []
        if not topics or topics[0].lower() != TRANSFER_TOPIC or len(topics) != 3:
            continue
        sender, receiver = addr("0x" + topics[1][-40:]), addr("0x" + topics[2][-40:])
        if holder not in (sender, receiver) or sender == receiver:
            continue
        data = log.get("data", "0x")
        value = int(data[:66], 16) if data not in ("0x", "") else 0
        token = addr(log["address"])
        net[token] += value if receiver == holder else -value
        moved[token] += value
        traded_pool = traded_pool or (receiver if sender == holder else sender) in pools
    if trace is not None and weth:
        weth = weth.lower()
        for sender, receiver, value in native_flows(trace):
            if (
                holder not in (sender, receiver)
                or sender == receiver
                or weth in (sender, receiver)
                or not value
            ):
                continue  # a wrap or unwrap only moves value between the holder's BNB and its WBNB
            net[weth] += value if receiver == holder else -value
            moved[weth] += value
            traded_pool = traded_pool or (receiver if sender == holder else sender) in pools
    if not traded_pool:
        return False
    losses = [t for t, v in net.items() if v < 0]
    gains = [t for t, v in net.items() if v > 0 and v > HOLDER_DUST_SHARE * moved[t]]
    return bool(losses) and not gains


def find_cyclic_sandwiches(
    rows: list[dict],
    receipts_by_hash: dict[str, dict],
    traces_by_hash: dict[str, dict] | None = None,
    weth: str | None = None,
) -> list[dict]:
    """Sandwiches whose legs are cycles: the operator holds none of the token, so its front leg sources it from a
    second pool and dumps it into the victim's pool (a cycle that loses), and its back leg reverses the route after
    the victim (a cycle that wins about the same back). Each leg alone reads as a cycle; together they are one
    sandwich whose profit is the victim's slippage. Ethereum 26317113: #6 lost 0.2162 WETH (WETH -> 4,709.5 POWR on
    V2 0x3c442bab -> 0.1515 WETH on V3 0xe3fe800b), #7 a user sold 6,901 POWR into the V3 pool, #8 won 0.21647 WETH
    the other way: a $0.57 sandwich that had counted as a $490.65 backrun (and again at 26317706 #19/#20/#21).

    Front: a cycle that lost (`lost_cycle`); back: a counted cycle within SANDWICH_MAX_GAP positions, sent by
    the same wallet to the same contract; a third party between them swaps a pool both legs trade; on that pool the
    front trades in the victim's direction and the back against it (the same Swap-log test as `find_sandwiches`).

    Holder link: the front may come from another wallet and another contract when it spends the back's operator's
    tokens (`holder_paid`: the back's contract trades a shared pool in the front and only loses there). BSC bot
    0x070f82ae sent its fronts from wallet 0x71817680 through contract 0x204b2c3b, both legs disguised as ERC-20
    `transfer` calls, and its backs from wallet 0xd2740fa4 straight to 0x070f82ae: block 131994371 #11 cost the bot
    0.478 WBNB through USD1/WBNB pools 0x4a321860 and 0x3d7c3190, a victim sold USD1 into 0x3d7c3190 at #12, and #13
    reversed the route for +0.486 WBNB. #13 had counted as a $338 backrun; the pair kept $2.98. Over 3 days 307 such
    pairs had counted $28,127 of backrun gross against $92 the bot actually kept."""
    ordered = sorted((r for r in rows if not r["fees"]["is_system"]), key=lambda r: r["tx_index"])
    out = []
    taken: set[str] = set()
    for i, front in enumerate(ordered):
        if (
            not front.get("venues")
            or front["tx_hash"] in taken
            or front.get("assessment") == "trace_supported"
        ):
            continue
        for back in ordered[i + 1 :]:
            if back["tx_index"] - front["tx_index"] > SANDWICH_MAX_GAP:
                break
            if (
                back.get("assessment") != "trace_supported"
                or back["tx_hash"] in taken
                or not back.get("tx_to")
            ):
                continue
            shared = set(front["venues"]) & set(back.get("venues") or [])
            if not shared:
                continue
            same_pair = (
                bool(front["tx_from"])
                and back["tx_from"] == front["tx_from"]
                and bool(front["tx_to"])
                and back["tx_to"] == front["tx_to"]
            )
            holder = str(back["tx_to"]).lower()
            if same_pair:
                if not lost_cycle(front):
                    continue
                link = "same_sender"
            elif holder_paid(
                receipts_by_hash[front["tx_hash"]].get("logs", []),
                holder,
                {str(v).split(":")[0].lower() for v in shared},
                (traces_by_hash or {}).get(front["tx_hash"]),
                weth,
            ):
                link = "holder"
            else:
                continue
            party = {
                str(x).lower()
                for x in (front["tx_from"], front["tx_to"], back["tx_from"], back["tx_to"])
                if x
            }
            between = [v for v in ordered if front["tx_index"] < v["tx_index"] < back["tx_index"]]
            victims = [
                v
                for v in between
                if str(v["tx_from"] or "").lower() not in party
                and str(v["tx_to"] or "").lower() not in party
                and set(v.get("venues") or []) & shared
            ]
            if not victims:
                continue
            confirmed_pool = None
            for pool in sorted(shared):
                da = _pool_directions(receipts_by_hash[front["tx_hash"]].get("logs", []), pool)
                dc = _pool_directions(receipts_by_hash[back["tx_hash"]].get("logs", []), pool)
                dv = [
                    d
                    for v in victims
                    for d in _pool_directions(receipts_by_hash[v["tx_hash"]].get("logs", []), pool)
                ]
                if da and dv and dc and da[0] != 0 and da[0] == dv[0] and dc[0] == -da[0]:
                    confirmed_pool = pool
                    break
            if confirmed_pool is None:
                continue
            bundle = {
                "block_number": front["block_number"],
                "bundle_id": f"{front['block_number']}:{front['tx_index']}",
                "front": front["tx_hash"],
                "back": back["tx_hash"],
                "victims": [v["tx_hash"] for v in victims if confirmed_pool in set(v.get("venues") or [])]
                or [victims[0]["tx_hash"]],
                "pool": confirmed_pool,
                "operator": holder,
                "operator_kind": "contract",
                "kind": "cyclic",
                "link": link,
                "party_extra": sorted(party - {holder}),
            }
            out.append(bundle)
            taken.update({front["tx_hash"], back["tx_hash"]})
            break
    return out


def _transfers(logs: list[dict]):
    for log in logs:
        topics = log.get("topics") or []
        if topics and topics[0].lower() == TRANSFER_TOPIC and len(topics) == 3:
            yield addr(log["address"]), addr("0x" + topics[1][-40:]), addr("0x" + topics[2][-40:])


def infer_holder(
    bundle: dict, receipts_by_hash: dict[str, dict], party: set[str], venues: set[str]
) -> str | None:
    """The account whose tokens the legs actually moved when it is neither the sender nor the called contract.

    A bot that trades through a forwarder keeps its inventory in a holding contract: that address receives a
    token in the front and sends it back in the back (or the reverse). Pools do the same, so decoded venue
    addresses are excluded; the holder is inferred only when exactly one address fits."""
    front = list(_transfers(receipts_by_hash[bundle["front"]].get("logs", [])))
    back = list(_transfers(receipts_by_hash[bundle["back"]].get("logs", [])))
    skip = party | venues | {bundle["pool"].split(":")[0].lower()}
    received_front = {r for _, _, r in front} - skip
    sent_front = {s for _, s, _ in front} - skip
    received_back = {r for _, _, r in back} - skip
    sent_back = {s for _, s, _ in back} - skip
    holders = (received_front & sent_back) | (sent_front & received_back)
    return next(iter(holders)) if len(holders) == 1 else None


def operator_result(
    bundle: dict,
    receipts_by_hash: dict[str, dict],
    weth: str,
    traces_by_hash: dict[str, dict] | None = None,
    wallet: str | None = None,
    venues: set[str] | None = None,
    producer: str | None = None,
    builders: set[str] | None = None,
) -> dict:
    """Net token changes of the operator over front + back; native value folded into the wrapped token.

    Native value the party pays the block `producer` inside the legs is a payment for the slot, not part of the result:
    it is left out of the net (and reported as `paid_to_producer_raw`), because the valuation subtracts the bundle's
    builder tips from the result to get what was kept. Counted in both, an Ethereum sandwich paying its builder 3.61
    ETH read as kept -$8,080 instead of +$177 (26334741 #0/#2). On BSC the builder is not the producer: 48 Club and
    BlockRazor are paid at their declared payment addresses (chain.toml `[known_recipients]` kind builder, `builders`),
    in BNB or WBNB; those payments are the slot's price the same way."""
    producer = producer.lower() if producer else None
    builder_set = {a.lower() for a in (builders or set())}
    payees = builder_set | ({producer} if producer else set())
    paid_to_producer = 0
    party = {bundle["operator"].lower()} | {str(a).lower() for a in bundle.get("party_extra") or []}
    if wallet:
        party.add(wallet.lower())
    # a holder-linked bundle's operator is the holder already; inferring another one picked the venue's own vault
    # (BSC 131873728: PancakeSwap Infinity's Vault 0x238a3588 received the token in the front and paid it out in the
    # back, and as a "holder" turned a token that netted to zero into a 490-unit loss and its WBNB into a $25.78 gain)
    holder = (
        None
        if bundle.get("link") == "holder"
        else infer_holder(bundle, receipts_by_hash, party, {v.lower() for v in (venues or set())})
    )
    if holder:
        party.add(holder)
    net: dict[str, int] = defaultdict(int)
    moved: dict[str, int] = defaultdict(int)  # gross volume of each token into and out of the party
    weth = weth.lower()
    for tx_hash in (bundle["front"], bundle["back"]):
        trace = (traces_by_hash or {}).get(tx_hash)
        for log in receipts_by_hash[tx_hash].get("logs", []):
            topics = log.get("topics") or []
            if not topics:
                continue
            token = addr(log["address"])
            data = log.get("data", "0x")
            value = int(data, 16) if data not in ("0x", "") else 0
            if (
                topics[0].lower() in (WETH_DEPOSIT_TOPIC, WETH_WITHDRAWAL_TOPIC)
                and token == weth
                and len(topics) >= 2
            ):
                # A wrap or unwrap moves value between the party's ETH and its WETH: with the trace the native side is
                # already in `native_flows` (the ETH taken from a Uniswap V4 pool, the ETH settled into one), so the
                # event must not be folded a second time — a V4 sandwich that unwrapped in the front and wrapped in the
                # back had reported twice its result, and one with an extra WETH leg 24 times it (Ethereum 26143358
                # #11: 0.001327 WETH read as 0.002654; 26137400 #131: 0.003426 WETH read as 0.0808). Without a trace
                # the event is the only sight of the native side and stands in for it.
                if trace is not None:
                    continue
                who = addr("0x" + topics[1][-40:])
                if who in party:
                    net[weth] += value if topics[0].lower() == WETH_DEPOSIT_TOPIC else -value
                    moved[weth] += value
                continue
            if topics[0].lower() != TRANSFER_TOPIC or len(topics) != 3:
                continue
            sender, receiver = addr("0x" + topics[1][-40:]), addr("0x" + topics[2][-40:])
            if receiver in party and sender not in party:
                net[token] += value
                moved[token] += value
            if sender in party and receiver not in party:
                if token == weth and receiver in builder_set:
                    paid_to_producer += value
                    continue
                net[token] -= value
                moved[token] += value
        if trace:
            for sender, receiver, value in native_flows(trace):
                if receiver in party and sender not in party and sender != weth:
                    net[weth] += value
                    moved[weth] += value
                if sender in party and receiver not in party and receiver != weth:
                    if receiver in payees:
                        paid_to_producer += value
                        continue
                    net[weth] -= value
                    moved[weth] += value
    nets = {t: v for t, v in net.items() if v}
    gains = {t: v for t, v in nets.items() if v > 0}
    profit_token = max(gains, key=gains.get) if gains else None
    return {
        "net_raw": {t: str(v) for t, v in nets.items()},
        "moved_raw": {t: str(moved[t]) for t in nets},
        "party": sorted(party),
        "holder_inferred": holder,
        "result_status": "measured" if nets else "no_operator_flows",
        "profit_token": profit_token,
        "profit_raw": str(gains[profit_token]) if profit_token else None,
        "losses": {t: str(v) for t, v in nets.items() if v < 0},
        "paid_to_producer_raw": str(paid_to_producer),
        "native_from_traces": bool(
            traces_by_hash and any(h in traces_by_hash for h in (bundle["front"], bundle["back"]))
        ),
    }


def round_trip_verdict(result: dict) -> str | None:
    """None when the operator's result is a round trip; else the token whose net change exceeds the allowed share.

    A sandwich returns what it took: the profit is a small share of the volume and the other pool token nets to
    dust. A token kept (bought and not sold back) or spent (sold from inventory) shows a net change close to its
    whole volume: the bundle is a position change bracketing a victim by coincidence."""
    worst = None
    for token, net in sorted((result.get("net_raw") or {}).items()):
        moved = int((result.get("moved_raw") or {}).get(token) or 0)
        share = abs(int(net)) / moved if moved else 0.0
        if share > ROUND_TRIP_MAX_SHARE and (worst is None or share > worst[1]):
            worst = (token, share)
    return f"inventory_change:{worst[0]}:{worst[1]:.2f}" if worst else None
