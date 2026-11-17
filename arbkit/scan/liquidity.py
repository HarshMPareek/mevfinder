"""Receipt evidence for LP inventory changes, separate from swap proceeds."""

from collections import defaultdict

from mevlab.decode.signatures import event_topic
from mevlab.hexutil import addr

MINT = event_topic("Mint(address,address,int24,int24,uint128,uint256,uint256)")
BURN = event_topic("Burn(address,int24,int24,uint128,uint256,uint256)")
COLLECT = event_topic("Collect(address,address,int24,int24,uint128,uint128)")
MODIFY = event_topic("ModifyLiquidity(bytes32,address,int24,int24,int256,bytes32)")
V2_MINT = event_topic("Mint(address,uint256,uint256)")
V2_BURN = event_topic("Burn(address,uint256,uint256,address)")
TRANSFER = event_topic("Transfer(address,address,uint256)")


def signed(word):
    value = int(word, 16)
    return value - 2**256 if value >= 2**255 else value


async def liquidity_evidence(logs, resolver):
    events, collected, deposited, removed = [], defaultdict(int), defaultdict(int), defaultdict(int)
    unresolved, owners = set(), set()
    added = reduced = False
    for log in logs:
        topics = log.get("topics", [])
        data = log.get("data", "0x")[2:]
        words = [data[i : i + 64] for i in range(0, len(data), 64)]
        if not topics or topics[0].lower() not in {MINT, BURN, COLLECT, MODIFY, V2_MINT, V2_BURN}:
            continue
        signature, pool = topics[0].lower(), addr(log["address"])
        if signature in {V2_MINT, V2_BURN} and len(words) == 2:
            meta = await resolver.get(pool)
            event = {
                "kind": "v2_mint" if signature == V2_MINT else "v2_burn",
                "pool": pool,
                "amount0_raw": str(int(words[0], 16)),
                "amount1_raw": str(int(words[1], 16)),
            }
            if meta:
                event.update(token0=meta.token0, token1=meta.token1)
                totals = deposited if signature == V2_MINT else collected
                totals[meta.token0] += int(words[0], 16)
                totals[meta.token1] += int(words[1], 16)
            else:
                unresolved.add(pool)
            events.append(event)
            added |= signature == V2_MINT
            reduced |= signature == V2_BURN
            continue
        if signature == MODIFY and len(topics) == 3 and len(words) == 4:
            delta = signed(words[2])
            events.append(
                {
                    "kind": "v4_modify",
                    "pool": pool,
                    "pool_id": topics[1].lower(),
                    "owner": addr(topics[2]),
                    "tick_lower": signed(words[0]),
                    "tick_upper": signed(words[1]),
                    "liquidity_delta": str(delta),
                    "salt": "0x" + words[3],
                }
            )
            owners.add(addr(topics[2]))
            added |= delta > 0
            reduced |= delta < 0
            continue  # V4 liquidity deltas are not token cash amounts.
        expected = 4 if signature == MINT else 3
        if len(topics) != 4 or len(words) != expected:
            continue
        owner = addr(topics[1])
        owners.add(owner)
        kind = {MINT: "mint", BURN: "burn", COLLECT: "collect"}[signature]
        liquidity = int(words[1 if signature == MINT else 0], 16) if signature != COLLECT else None
        event = {
            "kind": kind,
            "pool": pool,
            "owner": owner,
            "tick_lower": signed(topics[2][2:]),
            "tick_upper": signed(topics[3][2:]),
            "liquidity": str(liquidity) if liquidity is not None else None,
            "amount0_raw": str(int(words[-2], 16)),
            "amount1_raw": str(int(words[-1], 16)),
        }
        meta = await resolver.get(pool)
        if meta:
            event.update(token0=meta.token0, token1=meta.token1)
            totals = {MINT: deposited, BURN: removed, COLLECT: collected}[signature]
            totals[meta.token0] += int(words[-2], 16)
            totals[meta.token1] += int(words[-1], 16)
        else:
            unresolved.add(pool)
        events.append(event)
        added |= signature == MINT and bool(liquidity)
        reduced |= signature == BURN and bool(liquidity)
    nfts = [
        {
            "manager": addr(log["address"]),
            "from": addr(log["topics"][1]),
            "to": addr(log["topics"][2]),
            "token_id": str(int(log["topics"][3], 16)),
        }
        for log in logs
        if len(log.get("topics", [])) == 4
        and log["topics"][0].lower() == TRANSFER
        and log.get("data") == "0x"
        and addr(log["address"]) in owners
    ]
    kind = (
        "rebalance"
        if added and reduced
        else "provision"
        if added
        else "withdrawal"
        if reduced
        else "collection"
        if events
        else "unresolved"
    )
    return {
        "kind": kind,
        "events": events,
        "position_transfers": nfts,
        "collected_raw": {t: str(v) for t, v in collected.items() if v},
        "deposited_raw": {t: str(v) for t, v in deposited.items() if v},
        "burn_principal_raw": {t: str(v) for t, v in removed.items() if v},
        "unresolved_pools": sorted(unresolved),
        "position_valuation_included": False,
        "cash_totals_scope": "V2/V3 pool events for all owners; V4 retains liquidity deltas only",
        "collect_is_not_profit": True,
    }


V3_INITIALIZE = event_topic("Initialize(uint160,int24)")
ZERO_WORD = "0x" + "0" * 64


def created_pools(logs) -> list[str]:
    """Pools this receipt creates, as venue keys: a V2 pair's first mint (it locks MINIMUM_LIQUIDITY with a Transfer of its
    own LP token from and to the zero address, which only the first mint does), a V3 pool's Initialize, a V4 or
    PancakeSwap Infinity CL pool's Initialize on its pool manager (key `manager:pool_id`). The first price of the pool is
    set here: an arbitrage right after it reacts to this transaction (positions.created_pools)."""
    from mevlab.decode.signatures import INFINITY_CL_INITIALIZE_TOPIC, V4_INITIALIZE_TOPIC

    out = []
    for log in logs:
        topics = [str(t).lower() for t in log.get("topics") or []]
        if not topics:
            continue
        pool = addr(log["address"])
        if topics[0] == TRANSFER and len(topics) == 3 and topics[1] == ZERO_WORD and topics[2] == ZERO_WORD:
            out.append(pool)
        elif topics[0] == V3_INITIALIZE:
            out.append(pool)
        elif (
            topics[0] in (V4_INITIALIZE_TOPIC.lower(), INFINITY_CL_INITIALIZE_TOPIC.lower())
            and len(topics) > 1
        ):
            out.append(f"{pool}:{topics[1]}")
    return sorted(set(out))
