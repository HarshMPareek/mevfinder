"""Who called whom: readings of the call tree itself (called contracts, Uniswap V4 hook callbacks, ERC-4337
bundles, MetaMask delegations), independent of what value moved."""

from __future__ import annotations

from typing import Any

from mevlab.hexutil import addr, to_int

DELEGATION_MANAGER = (
    "0xdb9b1e94b5b69df7e401ddbede43491141047db3"  # MetaMask delegation framework, same address everywhere
)
EXECUTE_FROM_EXECUTOR = "0xd691c964"


def delegator_accounts(trace: dict[str, Any]) -> set[str]:
    """Smart accounts the MetaMask DelegationManager calls with `executeFromExecutor`: the delegators a redeemer acts for."""
    out: set[str] = set()

    def walk(c: dict[str, Any]) -> None:
        if (
            addr(c.get("from") or "") == DELEGATION_MANAGER
            and c.get("to")
            and (c.get("input") or "0x")[:10].lower() == EXECUTE_FROM_EXECUTOR
            and not c.get("error")
        ):
            out.add(addr(c["to"]))
        for cc in c.get("calls") or []:
            walk(cc)

    walk(trace)
    return out


# Callbacks a singleton pool manager makes to the contract that opened its lock: Uniswap V4 `unlockCallback(bytes)`,
# PancakeSwap Infinity `lockAcquired(bytes)`. Besides these, a manager calls only token contracts and hooks.
LOCK_CALLBACKS = {"0x91dd7346", "0xab6291fe"}
TOKEN_CALLS = {
    "0xa9059cbb",
    "0x23b872dd",
    "0x70a08231",
    "0x095ea7b3",
    "0xd0e30db0",
    "0x2e1a7d4d",
    "0xdd62ed3e",
}


def hook_machinery(trace: dict[str, Any]) -> set[str]:
    """The hooks a singleton manager called in this transaction, and the contracts those hooks called (their
    helpers), read from the call tree alone: a manager is the caller of a lock callback; anything it calls that is
    not the locker or a token operation is a hook. A hook's helper that takes fees from a user's swap, swaps them
    and re-adds liquidity looks like a closed, net-positive operator (Robinhood 102937418 #6: helper 0x9982538f,
    called by hook 0x4e346895 from afterSwap, "earned" 0.0157 WETH from the user's 2.01 WETH buy; 30 Robinhood
    'embedded cycles' were this mechanism) — it is the pool's own mechanism, never a secondary operator."""
    frames: list[tuple[str, str, str]] = []

    def walk(c: dict[str, Any]) -> None:
        if c.get("error") or c.get("revertReason"):
            return
        frames.append(
            (
                addr(c.get("from", "0x" + "0" * 40)),
                addr(c.get("to") or "0x" + "0" * 40),
                str(c.get("input") or "0x")[:10].lower(),
            )
        )
        for sub in c.get("calls") or []:
            walk(sub)

    walk(trace)
    managers = {frm for frm, _, sel in frames if sel in LOCK_CALLBACKS}
    lockers = {to for frm, to, sel in frames if sel in LOCK_CALLBACKS}
    hooks = {
        to
        for frm, to, sel in frames
        if frm in managers
        and to not in managers
        and to not in lockers
        and sel not in TOKEN_CALLS
        and len(sel) == 10
    }
    helpers = {
        to
        for frm, to, sel in frames
        if frm in hooks and to not in managers and sel not in TOKEN_CALLS and len(sel) == 10
    }
    return hooks | helpers


def callees(trace: dict[str, Any]) -> set[str]:
    """Every address that received a CALL/STATICCALL/DELEGATECALL frame that did not revert. A venue is a
    contract that is called (swap, getAmountOut, take/settle); an EOA whose tokens move by allowance is a
    counterparty (an order being filled), never a venue."""
    out: set[str] = set()

    def walk(c: dict[str, Any]) -> None:
        if c.get("error") or c.get("revertReason"):
            return
        # a bare value transfer (a CALL with value and no calldata) counts as a call only when the callee ran code
        # (gasUsed > 0 or frames beneath): a contract bought by sending it ETH still counts. A payment to a code-less
        # address executes nothing, so an EOA funder, collector or order maker is a payee, never a venue. Every "cycle"
        # whose first venue was such a payee was a user's signed sell order filled by a bot (1inch limit orders,
        # Permit2 orders: the user's tokens taken by allowance, the user paid in plain ETH, the bot keeping the
        # improvement — Ethereum 26155312 #351, 26141764 #14; BSC 126615424 #13) or the operator's own wallet taking
        # the change of a pre-funded tip back (Ethereum 26137380 #168, read as a loss). A user paid their output the
        # same way through a contract can still look like a venue; those order executors are declared settlements
        # (Arbitrum 513092576 #2)
        if c.get("to"):
            bare = (
                c.get("type", "CALL") == "CALL"
                and (c.get("input") or "0x") in ("0x", "")
                and to_int(c.get("value", "0x0")) > 0
            )
            ran_code = to_int(c.get("gasUsed", "0x0")) > 0 or bool(c.get("calls"))
            if not bare or ran_code:
                out.add(addr(c["to"]))
        for cc in c.get("calls") or []:
            walk(cc)

    walk(trace)
    return out


HOOK_CALLBACKS = {
    "0xdc98354e",
    "0x6fe7e6eb",
    "0x259982e5",
    "0x9f063efc",
    "0x21d0ee70",
    "0x6c2bbe7e",
    "0x575e24b4",
    "0xb47b2fb1",
    "0xb6a8b0fa",
    "0xe1b4af69",
}  # Uniswap V4 IHooks: before/after initialize, liquidity, swap, donate
MODIFY_LIQUIDITY = "0x5a6bcfda"  # PoolManager.modifyLiquidity(PoolKey,ModifyLiquidityParams,bytes)


def hook_liquidity_only(trace: dict[str, Any], managers: set[str]) -> bool:
    """True when every liquidity change of a singleton manager in the transaction happens inside a hook callback (a
    frame the manager issued with an IHooks selector): the hook, its fee locker or its rebalancer collecting the pool's
    fees, compounding them or moving its range while serving the swap. Those are the pool's own changes, never the
    operator's position. False without any modifyLiquidity frame, or when one is reached outside a hook callback (the
    operator, or a position manager it called)."""
    hubs = {addr(m) for m in managers if m}
    found = [0, 0]

    def walk(c: dict[str, Any], under_hook: bool) -> None:
        if c.get("error") or c.get("revertReason"):
            return
        frm = addr(c["from"]) if c.get("from") else None
        to = addr(c["to"]) if c.get("to") else None
        sel = (c.get("input") or "0x")[:10].lower()
        if frm in hubs and to not in hubs and sel in HOOK_CALLBACKS:
            under_hook = True
        if to in hubs and sel == MODIFY_LIQUIDITY:
            found[0] += 1
            found[1] += int(under_hook)
        for cc in c.get("calls") or []:
            walk(cc, under_hook)

    walk(trace, False)
    return found[0] > 0 and found[0] == found[1]


def hook_callees(trace: dict[str, Any], managers: set[str]) -> set[str]:
    """Every address reached only inside hook callbacks (frames a singleton manager issued with an IHooks selector):
    the hook itself, its rebalancer or fee locker, the position manager or PSM they call. They act for the pool while
    it serves the swap, so they are never a venue of the operator's route nor its collector: the rebalancer of
    Robinhood 70067057 #21 exchanging with the manager inside afterSwap had read as a second venue, which hid the
    five-pool route behind a 'gain without returning route'. An address also called outside a hook callback (the
    operator's own venues and tokens) is kept: the DAI contract moved by a PSM hook had been swept in, which hid the
    operator's own DAI burn and turned a zero-result round trip into a 9,743 DAI 'cycle' (Ethereum 26139612 #64)."""
    hubs = {addr(m) for m in managers if m}
    under: set[str] = set()
    outside: set[str] = set()

    def walk(c: dict[str, Any], under_hook: bool) -> None:
        if c.get("error") or c.get("revertReason"):
            return
        frm = addr(c["from"]) if c.get("from") else None
        to = addr(c["to"]) if c.get("to") else None
        sel = (c.get("input") or "0x")[:10].lower()
        if frm in hubs and to not in hubs and sel in HOOK_CALLBACKS:
            under_hook = True
        if to and to not in hubs:
            (under if under_hook else outside).add(to)
        for cc in c.get("calls") or []:
            walk(cc, under_hook)

    walk(trace, False)
    return under - outside


HANDLE_OPS_SELECTORS = {"0x1fad948c", "0x765e827f"}  # ERC-4337 EntryPoint v0.6 / v0.7 handleOps


def account_abstraction_sender(trace: dict[str, Any]) -> str | None:
    """For an ERC-4337 bundle the party is the smart account, not the bundler EOA: the EntryPoint's first
    CALL out (validateUserOp) names it. Structure only: the handleOps selector, no EntryPoint address."""
    if (trace.get("input") or "")[:10].lower() not in HANDLE_OPS_SELECTORS:
        return None
    entry = addr(trace["to"]) if trace.get("to") else None
    for c in trace.get("calls") or []:
        if c.get("type", "CALL") == "CALL" and c.get("to") and addr(c["to"]) != entry:
            return addr(c["to"])
    return None
