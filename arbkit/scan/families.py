"""Non-cycle MEV families tagged from receipts, block order and (when present) traces: liquidations, launch
snipes, just-in-time liquidity, and the swap flows that the pricing stage turns into CEX-DEX (LVR rebalancing)
candidates. Nothing here changes an arbitrage verdict; these are additional labels on rows the cycle classifier
left as `not_arb`, each with the evidence it was built from.

- liquidation: a lending protocol's liquidation event in the receipt (Aave V3 `LiquidationCall`, Compound V2 /
  Venus `LiquidateBorrow`, Compound V3 `AbsorbCollateral`, Morpho Blue `Liquidate`, Euler V2 `Liquidate`); the
  liquidator, borrower, debt repaid and collateral seized as the event states them (USD where the assets are priced).
- launch_snipe: a swap on a pool created earlier in the same block (factory PairCreated / PoolCreated, Uniswap V4
  Initialize) by a sender other than the creator: buying at launch. The position is not closed; the amounts spent
  and received are recorded, not a profit.
- jit_liquidity: the same wallet adds liquidity to a pool and removes it later in the same block with another
  party's swap on that pool in between (Mint … Swap … Burn / ModifyLiquidity ±): just-in-time liquidity. Both
  legs carry the bundle; the operator's fee income is not computed here.
- swap_flow: for single-route swaps, what the sender/callee pair paid and received, from the settled receipt; the
  pricing stage compares the execution price with the Binance 1-minute price and tags `cex_dex_candidate` when a
  top-of-block (or first-in-flashblock) swap executed better than the CEX by more than the noise band. That is a
  candidate for the on-chain leg of a CEX-DEX rebalancing (the arbitrageur's side of LVR); the off-chain leg and
  the operator's profit are not observable on chain.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from eth_abi import decode as abi_decode

from mevlab.decode.signatures import (
    SIG_SOLIDLY_SWAP,
    SIG_V2_BURN,
    SIG_V2_MINT,
    SIG_V2_SWAP,
    SIG_V3_BURN,
    SIG_V3_MINT,
    SIG_V3_SWAP,
    SIG_V4_MODIFY,
    SIG_V4_SWAP,
    V4_INITIALIZE_TOPIC,
    event_topic,
)
from mevlab.hexutil import addr, to_int
from mevlab.trace.movements import receipt_backed_movements

LIQUIDATION_EVENTS = {
    event_topic("LiquidationCall(address,address,address,uint256,uint256,address,bool)"): "aave_v3",
    event_topic("LiquidateBorrow(address,address,uint256,address,uint256)"): "compound_v2",
    event_topic("AbsorbCollateral(address,address,address,uint256,uint256)"): "compound_v3",
    event_topic("Liquidate(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)"): "morpho_blue",
    event_topic("Liquidate(address,address,address,uint256,uint256)"): "euler_v2",
    # Marginly (leveraged trading pools): a liquidator receives a position's base/quote at a discount
    event_topic("ReceivePosition(address,address,uint8,uint256,uint256)"): "marginly",
}
CREATION_EVENTS = {
    event_topic("PairCreated(address,address,address,uint256)"): "v2_factory",
    event_topic("PoolCreated(address,address,uint24,int24,address)"): "v3_factory",
    event_topic("PoolCreated(address,address,bool,address,uint256)"): "solidly_factory",
    V4_INITIALIZE_TOPIC: "v4_initialize",
}
ADD_TOPICS = {event_topic(SIG_V2_MINT): "v2", event_topic(SIG_V3_MINT): "v3"}
REMOVE_TOPICS = {event_topic(SIG_V2_BURN): "v2", event_topic(SIG_V3_BURN): "v3"}
MODIFY_TOPIC = event_topic(SIG_V4_MODIFY)
SWAP_TOPICS = {event_topic(s) for s in (SIG_V2_SWAP, SIG_SOLIDLY_SWAP, SIG_V3_SWAP, SIG_V4_SWAP)} | {
    "0x19b47279256b2a23a1665c810c8d55a1758940ee09377d4f8d26497a3577dc83"
}  # PancakeSwap V3
SWAP_CLASSES = {"single_swap", "routed_swap", "split_route", "unknown"}


def _words(log: dict) -> list[bytes]:
    data = bytes.fromhex(str(log.get("data") or "0x")[2:])
    return [data[i : i + 32] for i in range(0, len(data) - len(data) % 32, 32)]


def _topic_addr(topic: str) -> str:
    return "0x" + str(topic)[-40:].lower()


# ------------------------------------------------------------------------------------------------ liquidations


def decode_liquidation(log: dict) -> dict | None:
    topics = [str(t).lower() for t in (log.get("topics") or [])]
    if not topics or topics[0] not in LIQUIDATION_EVENTS:
        return None
    kind = LIQUIDATION_EVENTS[topics[0]]
    words = _words(log)
    out: dict[str, Any] = {
        "protocol": kind,
        "contract": addr(log["address"]),
        "liquidator": None,
        "borrower": None,
        "debt_asset": None,
        "debt_repaid_raw": None,
        "collateral_asset": None,
        "collateral_seized_raw": None,
    }
    try:
        if kind == "aave_v3" and len(topics) == 4 and len(words) >= 4:
            out.update(
                collateral_asset=_topic_addr(topics[1]),
                debt_asset=_topic_addr(topics[2]),
                borrower=_topic_addr(topics[3]),
                debt_repaid_raw=str(int.from_bytes(words[0], "big")),
                collateral_seized_raw=str(int.from_bytes(words[1], "big")),
                liquidator="0x" + words[2][-20:].hex(),
                receive_a_token=bool(int.from_bytes(words[3], "big")),
            )
        elif kind == "compound_v2" and len(words) >= 5:
            liq, bor, repay, ctoken, seize = abi_decode(
                ["address", "address", "uint256", "address", "uint256"], b"".join(words[:5])
            )
            out.update(
                liquidator=liq.lower(),
                borrower=bor.lower(),
                debt_asset=addr(log["address"]),
                debt_market_is_ctoken=True,
                debt_repaid_raw=str(repay),
                collateral_asset=ctoken.lower(),
                collateral_seized_raw=str(seize),
                collateral_in_ctoken_shares=True,
            )
        elif kind == "compound_v3" and len(topics) == 4 and len(words) >= 2:
            out.update(
                liquidator=_topic_addr(topics[1]),
                borrower=_topic_addr(topics[2]),
                collateral_asset=_topic_addr(topics[3]),
                collateral_seized_raw=str(int.from_bytes(words[0], "big")),
                collateral_usd_from_protocol=int.from_bytes(words[1], "big") / 1e8,
            )
        elif kind == "marginly" and len(topics) == 3 and len(words) >= 3:
            # ReceivePosition(liquidator, position, newPositionType, newPositionQuoteDiscounted, newPositionBaseDiscounted):
            # the amounts are the received position's discounted sizes, not the debt repaid or collateral seized
            out.update(
                liquidator=_topic_addr(topics[1]),
                borrower=_topic_addr(topics[2]),
                position_type=int.from_bytes(words[0], "big"),
                position_quote_discounted_raw=str(int.from_bytes(words[1], "big")),
                position_base_discounted_raw=str(int.from_bytes(words[2], "big")),
            )
        elif kind == "morpho_blue" and len(topics) == 4 and len(words) >= 3:
            out.update(
                market_id=topics[1],
                liquidator=_topic_addr(topics[2]),
                borrower=_topic_addr(topics[3]),
                debt_repaid_raw=str(int.from_bytes(words[0], "big")),
                collateral_seized_raw=str(int.from_bytes(words[2], "big")),
                bad_debt_raw=str(int.from_bytes(words[3], "big")) if len(words) > 3 else None,
            )
        elif kind == "euler_v2" and len(topics) == 4 and len(words) >= 2:
            out.update(
                liquidator=_topic_addr(topics[1]),
                borrower=_topic_addr(topics[2]),
                collateral_asset=_topic_addr(topics[3]),
                debt_asset=addr(log["address"]),
                debt_repaid_raw=str(int.from_bytes(words[0], "big")),
                collateral_seized_raw=str(int.from_bytes(words[1], "big")),
            )
        else:
            return None
    except (ValueError, OverflowError):
        return None
    return out


def assets_from_transfers(liq: dict, logs: list[dict]) -> None:
    """Fill a liquidation's assets the event does not name from the same receipt's Transfer logs: Morpho Blue's
    Liquidate carries only a market id (the repayment into Morpho and the collateral out of it match the event's
    amounts exactly); a Compound-V2 market's debt is repaid in its underlying token, paid into the cToken. Every
    Morpho liquidation on Base, Arbitrum and Robinhood had stayed unpriced (2026-11-01)."""
    transfer = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    moves = []
    for lg in logs:
        topics = [str(t).lower() for t in (lg.get("topics") or [])]
        data = str(lg.get("data") or "0x")[2:]
        if len(topics) == 3 and topics[0] == transfer and len(data) == 64:
            moves.append((addr(lg["address"]), _topic_addr(topics[1]), _topic_addr(topics[2]), int(data, 16)))
    venue = str(liq.get("contract") or "").lower()
    if liq.get("protocol") == "morpho_blue":
        debt = next(
            (t for t, _, to, a in moves if to == venue and str(a) == liq.get("debt_repaid_raw")), None
        )
        coll = next(
            (t for t, fr, _, a in moves if fr == venue and str(a) == liq.get("collateral_seized_raw")), None
        )
        if debt and not liq.get("debt_asset"):
            liq["debt_asset"], liq["assets_from"] = debt, "receipt_transfers"
        if coll and not liq.get("collateral_asset"):
            liq["collateral_asset"], liq["assets_from"] = coll, "receipt_transfers"
    elif liq.get("protocol") == "compound_v2" and liq.get("debt_market_is_ctoken"):
        market = str(liq.get("debt_asset") or "").lower()
        under = next(
            (
                t
                for t, _, to, a in moves
                if to == market and t != market and str(a) == liq.get("debt_repaid_raw")
            ),
            None,
        )
        if under:
            liq["debt_market"], liq["debt_asset"], liq["debt_market_is_ctoken"] = market, under, False
            liq["assets_from"] = "receipt_transfers"


# ------------------------------------------------------------------------------------------------ swap flows


def party_flows(tx: dict, receipt: dict, weth: str) -> dict | None:
    """What {tx.from, tx.to} paid and received net, from settled Transfer logs: one asset out, one asset in."""
    movs = receipt_backed_movements([], receipt.get("logs") or [], weth)
    if not movs:
        return None
    party = {str(tx.get("from", "")).lower(), str(tx.get("to") or "").lower()} - {""}
    net: dict[str, int] = defaultdict(int)
    for m in movs:
        if (m.sender in party) != (m.receiver in party):
            net[m.token] += m.amount if m.receiver in party else -m.amount
    outs = {t: -v for t, v in net.items() if v < 0}
    ins = {t: v for t, v in net.items() if v > 0}
    if len(outs) != 1 or len(ins) != 1:
        return None
    (tin, ain), (tout, aout) = next(iter(outs.items())), next(iter(ins.items()))
    return {"token_in": tin, "amount_in_raw": str(ain), "token_out": tout, "amount_out_raw": str(aout)}


# ------------------------------------------------------------------------------------------------ block pass


def annotate_block(
    spec, rows: list[dict], block: dict, receipts_by_hash: dict[str, dict], tx_by_index: dict[int, dict]
) -> None:
    """Tag liquidations, launch snipes, JIT bundles and swap flows on the rows of one block (in place)."""
    weth = spec.weth
    ordered = sorted(rows, key=lambda r: r["tx_index"])
    user = [r for r in ordered if not r["fees"]["is_system"]]
    for rank, r in enumerate(user):
        r["user_index_in_block"] = rank
    # first transaction of every estimated flashblock slot (Base)
    seen_slots: set[int] = set()
    for r in user:
        slot = r.get("slot_index_est")
        r["first_in_slot"] = slot is not None and slot not in seen_slots
        if slot is not None:
            seen_slots.add(slot)
    new_pools: dict[str, tuple[int, str]] = {}
    adds: dict[str, list[tuple[int, dict]]] = defaultdict(list)
    removes: dict[str, list[tuple[int, dict]]] = defaultdict(list)
    swaps_on: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for r in ordered:
        receipt = receipts_by_hash.get(r["tx_hash"].lower())
        if not receipt or to_int(receipt.get("status", "0x1")) != 1:
            continue
        tx = tx_by_index.get(r["tx_index"]) or {}
        sender = str(tx.get("from") or r.get("tx_from") or "").lower()
        liquidations = [d for d in (decode_liquidation(lg) for lg in receipt.get("logs") or []) if d]
        if liquidations:
            for liq in liquidations:
                assets_from_transfers(liq, receipt.get("logs") or [])
            r["liquidations"] = liquidations
        for lg in receipt.get("logs") or []:
            topics = [str(t).lower() for t in (lg.get("topics") or [])]
            if not topics:
                continue
            t0, emitter = topics[0], addr(lg["address"])
            if t0 in CREATION_EVENTS:
                words = _words(lg)
                if CREATION_EVENTS[t0] == "v4_initialize" and len(topics) > 1:
                    key = f"{emitter}:{topics[1]}"
                elif CREATION_EVENTS[t0] == "v3_factory" and len(words) >= 2:
                    key = "0x" + words[1][-20:].hex()
                elif words:
                    key = "0x" + words[0][-20:].hex()
                else:
                    continue
                new_pools.setdefault(key, (r["tx_index"], sender))
            elif t0 in ADD_TOPICS:
                adds[emitter].append((r["tx_index"], r))
            elif t0 in REMOVE_TOPICS:
                removes[emitter].append((r["tx_index"], r))
            elif t0 == MODIFY_TOPIC and len(topics) > 1:
                words = _words(lg)
                if len(words) >= 3:
                    delta = int.from_bytes(words[2], "big", signed=True)
                    key = f"{emitter}:{topics[1]}"
                    (adds if delta > 0 else removes)[key].append((r["tx_index"], r))
            elif t0 in SWAP_TOPICS:
                key = (
                    f"{emitter}:{topics[1]}"
                    if t0 == event_topic(SIG_V4_SWAP) and len(topics) > 1
                    else emitter
                )
                swaps_on[key].append((r["tx_index"], sender))
        if (
            r["arb_class"] in SWAP_CLASSES
            and r["assessment"] == "not_arb"
            and not r.get("sandwich")
            and r.get("n_legs", 0) >= 1
        ):
            flow = party_flows(tx, receipt, weth)
            if flow:
                flow["venues"] = list(r.get("venues") or [])[:4]
                flow["dexes"] = list(r.get("dexes") or [])[:4]
                r["swap_flow"] = flow
    # launch snipes: swaps on a pool created earlier in this block by someone else
    if new_pools:
        for r in ordered:
            keys = {str(v).lower() for v in (r.get("venues") or [])}
            hit = [k for k in keys if k in new_pools and new_pools[k][0] < r["tx_index"]]
            if not hit:
                continue
            created_at, creator = new_pools[hit[0]]
            sender = str(r.get("tx_from") or "").lower()
            if sender == creator:
                continue
            receipt = receipts_by_hash.get(r["tx_hash"].lower()) or {}
            flow = party_flows(tx_by_index.get(r["tx_index"]) or {}, receipt, weth) if receipt else None
            r["snipe"] = {
                "pool": hit[0],
                "created_in_tx_index": created_at,
                "creator": creator,
                "blocks_after_creation": 0,
                "flow": flow,
            }
    # JIT liquidity: one add, other parties' swaps, one remove, both sent by the same wallet on the same pool in one
    # block. A shared callee is not enough: on Optimism 159083366 two wallets using liquidity manager 0x1c0ab45f (an add
    # at #16, a remove at #70) were paired into a "JIT bundle" valued at -$194,430 (the sandwich rule, sandwich.py,
    # likewise never pairs two wallets). A transaction that removes and adds on the pool (a rebalance) is no JIT leg,
    # and no other liquidity action of the wallet on the pool may sit between the legs: Ethereum 26339453 #25 add,
    # #30 remove + add, #32 remove had read as a bundle #30-#32 that counted #25's principal as an $83,599 result.
    bundle = 0
    used_removes: set[str] = set()
    sender_of = lambda row: str(row.get("tx_from") or "").lower()  # noqa: E731
    for pool, added in adds.items():
        uniq_adds = {rr["tx_hash"]: (k, rr) for k, rr in added}
        uniq_rems = {rr["tx_hash"]: (k, rr) for k, rr in removes.get(pool, [])}
        mixed = uniq_adds.keys() & uniq_rems.keys()
        actions = [*uniq_adds.values(), *uniq_rems.values()]
        for i, add_row in sorted(
            uniq_adds.values(), key=lambda x: -x[0]
        ):  # latest add first: nearest the remove
            op_from = sender_of(add_row)
            if not op_from or add_row["tx_hash"] in mixed:
                continue
            same = [
                (k, rr)
                for k, rr in uniq_rems.values()
                if k > i
                and sender_of(rr) == op_from
                and rr["tx_hash"] not in mixed
                and rr["tx_hash"] not in used_removes
            ]
            if not same:
                continue
            k, rem_row = min(same, key=lambda x: x[0])
            if any(i < j < k and sender_of(rr) == op_from for j, rr in actions):
                continue
            victims = sorted({s for j, s in swaps_on.get(pool, []) if i < j < k and s not in (op_from,)})
            if not victims:
                continue
            bundle += 1
            info = {
                "bundle_id": f"jit-{block.get('number')}-{bundle}",
                "pool": pool,
                "add_tx_index": i,
                "remove_tx_index": k,
                "victims": victims,
            }
            add_row["jit"] = {**info, "role": "add"}
            rem_row["jit"] = {**info, "role": "remove"}
            used_removes.add(rem_row["tx_hash"])


def family_of(row: dict) -> str | None:
    """Extra MEV family of a row that the cycle classifier left as not_arb (None when none applies)."""
    if row.get("arb_class") == "pool_exploit":
        return "pool_exploit"
    if row.get("jit"):
        return "jit_liquidity"
    if row.get("liquidations"):
        return "liquidation"
    if row.get("snipe"):
        return "launch_snipe"
    if row.get("cex_dex"):
        return "cex_dex_candidate"
    return None


__all__ = ["annotate_block", "decode_liquidation", "family_of", "party_flows"]
