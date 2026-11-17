"""What moved: every value movement of a transaction, in trace order.

Sees: the call trace (native value of successful frames, transfer calldata) and, when given, the receipt's settled
Transfer logs, which are exact (transfer taxes, mints and redemptions included).
Produces: Movement(token, sender, receiver, amount, order, kind). ETH and WETH are one asset."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from mevlab.decode.signatures import ERC6909_TRANSFER_TOPIC, TRANSFER_TOPIC, ZERO_ADDRESS
from mevlab.hexutil import addr, to_int
from mevlab.trace.model import Movement

SEL_TRANSFER = "0xa9059cbb"
SEL_TRANSFER_FROM = "0x23b872dd"


def _words(inp: str) -> list[str]:
    data = inp[10:]
    return [data[i : i + 64] for i in range(0, len(data) - len(data) % 64, 64)]


def _addr_word(w: str) -> str:
    return "0x" + w[-40:].lower()


SEL_MINT_TO = {"0x40c10f19"}  # mint(address,uint256): Curve LP tokens, most mintable ERC-20s
SEL_BURN_FROM = {"0x79cc6790", "0x9dc29fac"}  # burnFrom(address,uint256) (Curve LP), burn(address,uint256)


def movements(
    trace: dict[str, Any],
    weth: str,
    logs: list[dict[str, Any]] | None = None,
    quote_venues: set[str] | None = None,
) -> tuple[list[Movement], bool]:
    """Pre-order walk of the callTracer tree. Returns (movements, top_frame_reverted).

    `quote_venues`: declared RFQ settlement contracts. A `transferFrom` executed inside one of their frames
    (directly or through Permit2) is tagged with that contract (`Movement.via`), so an uncalled market maker
    whose inventory it moved can be read as that venue's leg."""
    weth = weth.lower()
    quotes = {addr(q) for q in (quote_venues or set()) if q}
    out: list[Movement] = []
    counter = 0

    def walk(c: dict[str, Any], stack: tuple[str, ...] = ()) -> None:
        nonlocal counter
        if c.get("error") or c.get("revertReason"):
            return  # everything under a reverted frame was undone
        counter += 1
        order = counter
        typ = c.get("type", "CALL")
        frm = addr(c["from"]) if c.get("from") else None
        to = addr(c["to"]) if c.get("to") else None
        inp = c.get("input") or "0x"
        via = next((a for a in reversed(stack) if a in quotes), None) if quotes else None
        val = to_int(c.get("value", "0x0")) if typ not in ("DELEGATECALL", "STATICCALL") else 0
        if val and frm and to and weth not in (frm, to):  # deposit/withdraw are not moves under ETH ≡ WETH
            out.append(Movement(weth, frm, to, val, order, "native"))
        elif val and frm == weth and to and to != weth:
            # A proxy WETH paying a third party (`withdrawTo`): kept provisionally; receipt_backed_movements
            # attributes it to the burner or drops it as a plain unwrap.
            out.append(Movement(weth, frm, to, val, order, "native_from_weth"))
        if typ == "CALL" and to and frm and len(inp) >= 10:
            sel = inp[:10].lower()
            words = _words(inp)
            if sel == SEL_TRANSFER and len(words) >= 2:
                out.append(Movement(to, frm, _addr_word(words[0]), int(words[1], 16), order, "transfer"))
            elif sel == SEL_TRANSFER_FROM and len(words) >= 3:
                out.append(
                    Movement(
                        to,
                        _addr_word(words[0]),
                        _addr_word(words[1]),
                        int(words[2], 16),
                        order,
                        "transferFrom",
                        via,
                    )
                )
            elif sel in SEL_MINT_TO and len(words) >= 2:
                # a pool minting its separate LP token (Curve FRAXBP -> crvFRAX): who issued it, for the receipt pass
                out.append(Movement(to, frm, _addr_word(words[0]), int(words[1], 16), order, "mint_call"))
            elif sel in SEL_BURN_FROM and len(words) >= 2:
                out.append(Movement(to, _addr_word(words[0]), frm, int(words[1], 16), order, "burn_call"))
        for cc in c.get("calls") or []:
            walk(cc, stack + (to,) if to else stack)

    walk(trace)
    out = [m for m in out if m.amount > 0 and m.sender != m.receiver]
    reverted = bool(trace.get("error") or trace.get("revertReason"))
    if logs is not None and not reverted:
        out = receipt_backed_movements(out, logs, weth)
    else:
        out = [m for m in out if m.kind not in {"native_from_weth", "mint_call", "burn_call"}]
    return out, reverted


def receipt_backed_movements(
    inferred: list[Movement], logs: list[dict[str, Any]], weth: str
) -> list[Movement]:
    """Use emitted ERC-20 amounts, including transfer taxes, rather than requested calldata.

    Trace CALL positions locate matching flows; receipt order breaks ties. Native
    moves retain trace order. Unmatched token events retain receipt order near the
    preceding matched call. This is a flow reconstruction, not exact EVM log timing.
    None vs [] matters: a provided empty receipt must not invent ERC-20 proceeds.
    """
    multiplier = len(logs) + 1  # room for every log between two trace positions
    minters = _conversion_minters(inferred, logs)
    moves = [m for m in inferred if m.kind not in {"mint_call", "burn_call"}]
    calls = [m for m in moves if m.kind in {"transfer", "transferFrom"}]
    result = _native_moves(moves, logs, weth, multiplier) + _receipt_transfers(
        calls, logs, weth, minters, multiplier
    )
    return sorted(result, key=lambda move: move.order)


def _is_transfer(log: dict[str, Any]) -> bool:
    topics = log.get("topics") or []
    return len(topics) == 3 and str(topics[0]).lower() == TRANSFER_TOPIC


def _conversion_minters(
    inferred: list[Movement], logs: list[dict[str, Any]]
) -> dict[tuple[str, str, int], str]:
    """(token, receiver, amount) -> the contract that called the token's mint for that receiver.

    LP tokens minted by the pool that issues them, when the token is a separate contract (Curve FRAXBP pool 0xdcef968d
    calls crvFRAX.mint): the pool, not the token contract, is the counterparty of the conversion. Booked to the token
    contract, the minted LP token looked like a payment from outside the route and a closed USDC -> crvFRAX -> RSR ->
    WETH -> USDC cycle read as an open loss (Ethereum 26314163 #15). Only a conversion counts: the mint's receiver paid
    the minter something else in this transaction (the bot paid USDC to FRAXBP). An issuer minting supply to a third
    party (Sky's join minting USDS interest into the sUSDS vault, Ethereum 26313968 #9) stays booked to the token.

    Burns stay booked to the token contract. Re-attributing burn calls to their caller the same way (the pool paid the
    holder back) broke a verified cycle (Ethereum 26315403, 0x1ce9b927: counted gross lost) and turned five unverified
    Ethereum rows into cycles, so it needs a narrower rule before it can apply."""
    paid_pairs = {
        (addr(lg["topics"][1]), addr(lg["topics"][2]), addr(lg["address"])) for lg in logs if _is_transfer(lg)
    }
    paid_pairs |= {
        (m.sender, m.receiver, m.token) for m in inferred if m.kind in {"transfer", "transferFrom", "native"}
    }

    def paid(frm: str, to: str, token: str) -> bool:
        return any(a == frm and b == to and t != token for a, b, t in paid_pairs)

    return {
        (m.token, m.receiver, m.amount): m.sender
        for m in inferred
        if m.kind == "mint_call" and paid(m.receiver, m.sender, m.token)
    }


def _native_moves(
    moves: list[Movement], logs: list[dict[str, Any]], weth: str, multiplier: int
) -> list[Movement]:
    """Native value in trace order. `withdrawTo` on a proxy WETH (Robinhood): X burns WETH and the contract pays the
    native coin straight to Y. Economically X paid Y; such native moves are re-attributed to the burner so payouts
    are not lost."""
    weth_burners: dict[int, list[str]] = defaultdict(list)
    for log in logs:
        data = str(log.get("data") or "0x")[2:]
        if (
            _is_transfer(log)
            and len(data) == 64
            and addr(log["address"]) == weth
            and addr(log["topics"][2]) == ZERO_ADDRESS
        ):
            weth_burners[int(data, 16)].append(addr(log["topics"][1]))
    result = []
    for m in moves:
        if m.kind == "native":
            result.append(Movement(m.token, m.sender, m.receiver, m.amount, m.order * multiplier, m.kind))
        elif m.kind == "native_from_weth":
            burner = next((b for b in weth_burners.get(m.amount, []) if b != m.receiver), None)
            if burner is None:
                continue  # a plain unwrap to the burner itself: neutral
            weth_burners[m.amount].remove(burner)
            result.append(
                Movement(m.token, burner, m.receiver, m.amount, m.order * multiplier, "native_unwrap_to")
            )
    return result


def _receipt_transfers(
    calls: list[Movement], logs: list[dict[str, Any]], weth: str, minters: dict, multiplier: int
) -> list[Movement]:
    """Every ERC-20 Transfer log as a movement, placed at the trace position of the transfer call it matches."""
    exact: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    source: dict[tuple[str, str], list[int]] = defaultdict(list)
    via_of: dict[tuple[str, str, str], str] = {}
    for move in calls:
        exact[(move.token, move.sender, move.receiver)].append(move.order)
        source[(move.token, move.sender)].append(move.order)
        if move.via:
            via_of[(move.token, move.sender, move.receiver)] = move.via
    # the largest transfer each sender itself directed per token (calldata of its transfer / transferFrom calls), counted
    # only when the receipt confirms that directed transfer (a log of the same token, sender and receiver): a tax log
    # is then a cut of a transfer that really happened, never a stray log next to calldata that disagrees with it
    logged = {
        (addr(lg["address"]), addr(lg["topics"][1]), addr(lg["topics"][2])) for lg in logs if _is_transfer(lg)
    }
    directed_max: dict[tuple[str, str], int] = {}
    for move in calls:
        if (move.token, move.sender, move.receiver) in logged:
            k = (move.token, move.sender)
            directed_max[k] = max(directed_max.get(k, 0), move.amount)
    used: dict[tuple[str, str, str], int] = defaultdict(int)
    previous = 0
    result = []
    for index, log in enumerate(logs):
        data = str(log.get("data") or "0x")[2:]
        if not _is_transfer(log) or len(data) != 64:
            continue
        token, sender, receiver = addr(log["address"]), addr(log["topics"][1]), addr(log["topics"][2])
        amount = int(data, 16)
        if not amount or sender == receiver:
            continue
        if token == weth and ZERO_ADDRESS in (sender, receiver):
            continue  # wrap/unwrap is neutral under ETH == WETH
        kind = "receipt_transfer"
        if sender == ZERO_ADDRESS:
            # Minted shares come economically from their issuer. They can be
            # delivered directly to the next pool, rather than through the bot.
            sender, kind = minters.get((token, receiver, amount), token), "receipt_mint"
        if receiver == ZERO_ADDRESS:
            # Redemption consumes shares at the issuer (the token contract), which releases collateral.
            # A standalone burn still has no opposite asset and is not a venue.
            receiver, kind = token, "receipt_burn"
        if sender == receiver:
            continue  # issuer burning its own supply is not an external cashflow
        key = (token, sender, receiver)
        if (
            kind == "receipt_transfer"
            and key not in exact
            and amount <= directed_max.get((token, sender), 0)
            and token != weth
        ):
            # Emitted inside a transfer the sender directed to someone else: the token's own side effect (a transfer
            # tax, a reflection or dividend share), never a flow the sender chose. Its receiver is the token's sink.
            kind = "receipt_side_effect"
        candidates = exact.get(key) or source.get((token, sender))
        if candidates:
            position = candidates[min(used[key], len(candidates) - 1)]
            used[key] += 1
            previous = position
        else:
            position = previous
        result.append(
            Movement(
                token, sender, receiver, amount, position * multiplier + index + 1, kind, via_of.get(key)
            )
        )
    return result


def claim_movements(logs: list[dict[str, Any]], start_order: int = 10**6) -> list[Movement]:
    """Uniswap V4 keeps a bot's profit as ERC-6909 claims when it does not `take`; the PoolManager emits
    Transfer(caller, from, to, id, amount) with id = the currency address. A mint (from = 0) is the singleton
    paying the claimant in that currency; a burn (to = 0) is the claimant paying it. Logs, not Swap events."""
    out: list[Movement] = []
    zero = "0x" + "0" * 40
    for i, lg in enumerate(logs):
        topics = lg.get("topics") or []
        if len(topics) != 4 or str(topics[0]).lower() != ERC6909_TRANSFER_TOPIC:
            continue
        pm = addr(lg["address"])
        frm, to = _addr_word(str(topics[1])[2:]), _addr_word(str(topics[2])[2:])
        token = _addr_word(str(topics[3])[2:])
        data = str(lg.get("data") or "0x")[2:]
        if len(data) < 128:
            continue
        amount = int(data[64:128], 16)
        if amount <= 0:
            continue
        if frm == zero:
            out.append(Movement(token, pm, to, amount, start_order + i, "claim_mint"))
        elif to == zero:
            out.append(Movement(token, frm, pm, amount, start_order + i, "claim_burn"))
    return out


def nets(movs: list[Movement]) -> dict[str, dict[str, int]]:
    net: dict[str, dict[str, int]] = {}
    for m in movs:
        net.setdefault(m.sender, {})
        net[m.sender][m.token] = net[m.sender].get(m.token, 0) - m.amount
        net.setdefault(m.receiver, {})
        net[m.receiver][m.token] = net[m.receiver].get(m.token, 0) + m.amount
    return net


def terminal_receivers(movs: list[Movement]) -> set[str]:
    """Addresses that only ever receive in this transaction: collectors, kickback recipients, fee sinks."""
    return {m.receiver for m in movs} - {m.sender for m in movs}
