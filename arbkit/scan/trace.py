"""The trace verdict applied to one classified row: the event layer's label is confirmed, corrected or overruled
by what the call trace shows (mevlab.trace), and the row gets its exact gross, payouts and costs."""

from __future__ import annotations

from arbkit.chain.spec import ChainSpec
from arbkit.scan.families import LIQUIDATION_EVENTS
from arbkit.scan.venues import hook_addresses
from mevlab.decode.signatures import (
    LIQUIDITY_TOPICS,
    SETTLEMENT_TOPICS,
    SIG_1INCH_ORDER_FILLED,
    TRANSFER_TOPIC,
    event_topic,
)
from mevlab.hexutil import addr
from mevlab.trace.calls import callees, hook_callees
from mevlab.trace.cashflows import cashflow_evidence, native_flows
from mevlab.trace.embedded import embedded_cycles
from mevlab.trace.verdict import trace_verdict

# Intent fills: the transaction executes someone else's order (UniswapX Dutch orders, CoW batches). A 1inch
# limit order resting on chain is taken like an RFQ quote (verified cycle Ethereum 0x9af997d4…), so OrderFilled
# alone is not an intent fill; 1inch Fusion intents go through a settlement contract registered per chain.
INTENT_FILL_TOPICS = {t: sig for t, sig in SETTLEMENT_TOPICS.items() if sig != SIG_1INCH_ORDER_FILLED}


# Known order-flow kickback recipients (Blink Labs): a structural marker of a private-flow backrun, the mechanism's fee
KICKBACK_RECIPIENTS = {"0x743be0db30148336a3db479f19d4e1828b293869": "blink"}


def share_operation(tx: dict, receipt: dict) -> str | None:
    """The transaction's callee is a share token and the sender's shares of it are burned (a withdrawal, redemption) or
    minted (a deposit) in this transaction: the sender exchanged its position with the issuer, never a cycle. A Yield
    Basis yb-WETH redemption (Ethereum 26316933 #190: 1.9104 yb-WETH burned, the vault unwound its Curve LP and repaid
    its crvUSD debt, 1.9984 WETH to the user) had closed as a $4,527 "cycle" with the vault as the operator, because the
    burned shares never left the operator group (withdrawals must never be promoted as cycles)."""
    callee = str(tx.get("to") or "").lower()
    sender = str(tx.get("from") or "").lower()
    if not callee or callee == sender:
        return None
    zero = "0x" + "0" * 40
    for lg in receipt.get("logs") or []:
        topics = [str(t).lower() for t in lg.get("topics") or []]
        if (
            len(topics) != 3
            or topics[0] != TRANSFER_TOPIC.lower()
            or str(lg.get("address", "")).lower() != callee
        ):
            continue
        frm, to = "0x" + topics[1][-40:], "0x" + topics[2][-40:]
        if frm == sender and to == zero:
            return "shares_burned_at_callee"
        if frm == zero and to == sender:
            return "shares_minted_by_callee"
    return None


def sender_wallet_trades(tx: dict, receipt: dict) -> bool:
    """The transaction's sender is a wallet distinct from its callee and tokens leave or reach that wallet itself: the
    sender is trading through a router, aggregator or smart wallet (a user's swap), not running its own inventory in
    a contract. A searcher keeps inventory in its contract (or is the contract, EIP-7702). Ethereum 26317003 #52: a
    wallet paid 55,503 USDC into a router that split it over V3, V4, Ekubo and the Sky PSM and paid back 55,559 USDT."""
    callee = str(tx.get("to") or "").lower()
    sender = str(tx.get("from") or "").lower()
    if not callee or callee == sender:
        return False
    for lg in receipt.get("logs") or []:
        topics = [str(t).lower() for t in lg.get("topics") or []]
        if (
            len(topics) == 3
            and topics[0] == TRANSFER_TOPIC.lower()
            and sender in ("0x" + topics[1][-40:], "0x" + topics[2][-40:])
        ):
            return True
    return False


def mark_pool_exploit(row) -> bool:
    """A swap beat its own pool's pre-swap price (`geometry.invariant_violations`, reason `pool_invariant_violation`):
    the pool's math was broken, so what the operator kept came out of the pool's liquidity providers, not from a
    price difference between venues. Family `pool_exploit`, never counted as arbitrage (OP 0x782ca99b…, a
    KyberSwap Elastic tick-crossing exploit, had been counted as a $109 cycle)."""
    exploited = sorted(
        {
            str(r).split(":")[1]
            for r in row.get("reasons") or []
            if str(r).startswith("pool_invariant_violation:")
        }
    )
    if not exploited or row.get("arb_class") == "reverted":
        return False
    row["arb_class"] = "pool_exploit"
    row["is_arb"] = False
    row["assessment"] = "not_arb"
    row["exploited_pools"] = exploited
    return True


def builder_tip(trace, party: set[str] | None, miner: str | None) -> int:
    """Native value paid to the block producer inside the transaction: by the operator's group (`party`), or by any
    contract when `party` is None — the searcher's own transaction, where a router it calls pays the builder for it
    (Ethereum 0x03b6518a: a sandwich back-run's 0.00598 ETH tip came from a helper outside the group)."""
    if not miner:
        return 0
    miner = miner.lower()
    return sum(
        value
        for sender, receiver, value in native_flows(trace)
        if receiver == miner and (party is None or sender in party)
    )


DEBT_EVENT_TOPICS = {
    event_topic("Borrow(bytes32,address,address,address,uint256,uint256)").lower(): "morpho_blue_borrow",
    event_topic("Borrow(address,address,address,uint256,uint8,uint256,uint16)").lower(): "aave_v3_borrow",
    event_topic("Borrow(address,uint256,uint256,uint256)").lower(): "compound_v2_borrow",
}
# A debt repayment in the receipt closes or deleverages a position: the collateral that comes out pays for the debt that
# went in, whatever pool legs surround it (Arbitrum 512966161 #4: a USDT0 flash swap repaid a position's Aave debt,
# its 1.77 WETH collateral was withdrawn and sold, and the 0.33 USDC left over had closed as a "cycle").
V4_SWAP_TOPIC = event_topic("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)").lower()
REPAY_EVENT_TOPICS = {
    event_topic("Repay(bytes32,address,address,uint256,uint256)").lower(): "morpho_blue_repay",
    event_topic("Repay(address,address,address,uint256,bool)").lower(): "aave_v3_repay",
    event_topic("RepayBorrow(address,address,uint256,uint256,uint256)").lower(): "compound_v2_repay",
    # Fluid vaults: one `LogOperate` carries the collateral and debt deltas of a position (deposit, withdraw, borrow,
    # repay). A Safe repaying its 1.0078 WBTC debt with a flash loan, withdrawing 130,681 USDC of collateral and buying
    # the WBTC back for 83,942 USDC had closed as a "130,681 USDC cycle" (Ethereum 26141146 #245). The liquidity-layer
    # event is not used: Fluid DEX swaps emit it too.
    event_topic("LogOperate(address,uint256,int256,int256,address)").lower(): "fluid_vault_operate",
    event_topic("LogOperate(address,uint256,int256,int256,address,uint256)").lower(): "fluid_vault_operate",
}


def review_unregistered_profit(spec: ChainSpec, row) -> None:
    """A counted cycle whose profit token the chain does not register stays under review in two shapes:

    - no venue of the route was resolved by the event layer (counterfeit "USDT" between two unidentified contracts,
      vault shares minted on a deposit: BSC `0x4f73c0a7…`, Ethereum `gtUSDT` 1,709 USDT -> a "$1,709 cycle");
    - the profit is a token minted on the route and not the cycle's own token (vault shares, staking receipts, a debt
      token: Ethereum `SY-USD3`, `bbqAUSDterm`, `stkCvxCrv`, `variableDebtEthUSDT`): a deposit or a claim, not an
      arbitrage result, until the chain registers and prices it."""
    if row.get("assessment") != "trace_supported":
        return
    registered = {tk.address.lower() for tk in spec.tokens} | {spec.weth}
    profit = str(row.get("profit_token") or "").lower()
    if profit in registered:
        return
    notes = (row.get("trace") or {}).get("notes") or []
    cycle_token = next(
        (n.split(":")[1] for n in notes if n.startswith("cycle_token:")), row.get("profit_token")
    )
    reason = None
    if not row.get("kinds"):
        reason = "unresolved_venues_unregistered_profit_token"
    elif "token_mint_redemption_flows" in notes and str(cycle_token or "").lower() != profit:
        reason = "unregistered_minted_profit_token"
    if reason:
        row["assessment"] = "needs_review"
        row["is_arb"] = False
        row["reasons"] = [*row.get("reasons", []), reason]


# Classes the structural rules give a row: never a cycle, whatever the trace closes
EXCLUDED_CLASSES = {
    "lp_action",
    "settlement_like",
    "reverted",
    "round_trip",
    "bundled_operation",
    "debt_operation",
    "hook_mechanism",
    "pool_exploit",
    "share_operation",
}


def apply_trace(spec: ChainSpec, row, trace, tx, receipt, miner: str | None = None, resolver=None):
    """Verify one classified row against its call trace, in order:

    1. the trace verdict (mevlab.trace), told what the event layer knows: venues, hooks, singleton managers, payees
    2. structural exclusions: order fills, rebalances, hook mechanisms, debt / liquidation / bundler operations, pool
       exploits and share operations are never cycles, whatever the trace closes
    3. the verdict on the row: a counted cycle (or loss cycle) with its exact gross, or a row held for review
    4. a cycle of another operator embedded in the transaction
    5. payments to builders and kickback recipients (costs, kept apart from gas)"""
    if trace.get("from", "").lower() != tx.get("from", "").lower() or (
        tx.get("to") and trace.get("to", "").lower() != tx["to"].lower()
    ):
        raise ValueError("Trace root sender/recipient disagrees with transaction")
    # Validate successful/reverted status before trusting any native cash movement.
    recipients = {**KICKBACK_RECIPIENTS, **spec.recipient_names}
    evidence = cashflow_evidence(
        trace, receipt.get("logs"), spec.weth, recipients, receipt_status=receipt.get("status")
    )
    event_venues = list(row.get("event_label", {}).get("venues") or row.get("venues") or [])
    # The registry's singleton managers are infrastructure whether or not a Swap event of theirs was decoded: a flash
    # take/settle round trip through the Uniswap V4 manager emits none, and the manager had been absorbed into the
    # operator's group (Robinhood 84263927 #1, Arbitrum 513170582 #2).
    hints = (
        {v.split(":")[0].lower() for v in event_venues}
        | set(spec.declared_venues or {})
        | set(spec.pool_managers)
    )
    managers = {v.split(":")[0].lower() for v in event_venues if ":" in str(v)} | set(spec.pool_managers)
    tv = trace_verdict(
        trace,
        spec.weth,
        tx.get("from"),
        tx.get("to"),
        logs=receipt.get("logs"),
        venue_hints=hints,
        fee_takers=hook_addresses(resolver, event_venues),
        singletons=managers,
        known_recipients=set(recipients) | {str(miner or "").lower()},
        quote_venues=set(spec.quote_venues or {}),
        venue_aliases=spec.venue_aliases or None,
    )
    row["trace"] = {"status": "ok", **tv.to_json()}
    row["cashflows"] = evidence
    row["route_type"] = (
        "swaps_and_token_conversions" if "token_mint_redemption_flows" in tv.notes else "swaps"
    )

    settlements = spec.recipients_of_kind("settlement")
    exclude_structural(spec, row, tv, trace, tx, receipt, event_venues, hints, settlements)
    excluded = row["arb_class"] in EXCLUDED_CLASSES
    quote_legs = [n.split(":")[1] for n in tv.notes if n.startswith("rfq_quote_leg:")]
    row["quote_venues"] = (
        [spec.quote_venues.get(a, {}).get("name", a) for a in quote_legs] if quote_legs else []
    )
    apply_verdict(row, tv, excluded, event_venues)
    flag_trace_only_venues(spec, row, tv, hints)
    review_unregistered_profit(spec, row)
    if not excluded and row["assessment"] in {"not_arb", "needs_review"}:
        adopt_embedded_cycle(spec, row, tv, trace, receipt, hints, settlements)
    if row["arb_class"] == "unknown" and row["assessment"] == "not_arb" and row["n_legs"] > 0:
        inventory = tv.group_asset_deltas
        if (
            sum(v > 0 for v in inventory.values()) == 1
            and sum(v < 0 for v in inventory.values()) == 1
            and tv.venues
        ):
            row["arb_class"] = "routed_swap"
            row["reasons"] = [*row["reasons"], "trace_resolved_one_way_token_exchange"]
    record_payments(spec, row, tv, trace, tx, evidence, recipients, settlements, miner)


def _not_arb(row, arb_class: str, reason: str) -> None:
    row["arb_class"] = arb_class
    row["is_arb"] = False
    row["reasons"] = [*row["reasons"], reason]


def _logged(receipt, table: dict[str, str]) -> list[str]:
    """Names (table values) of the receipt's events whose topic0 is in `table`."""
    return sorted(
        {
            table[t]
            for lg in receipt.get("logs") or []
            for t in [str((lg.get("topics") or [""])[0]).lower()]
            if t in table
        }
    )


def exclude_structural(
    spec: ChainSpec, row, tv, trace, tx, receipt, event_venues, hints, settlements
) -> None:
    """Rules that make a row a non-cycle by what the transaction is, applied in this order; a class set by an earlier
    rule is not overwritten by a later one (each rule lists the classes it leaves alone)."""
    # Intent settlements and aggregator routers (CoW, 1inch) fill orders: a transaction that calls one is a
    # settlement, never a cycle, whatever surplus the trace closes (solver surplus, user hooks repaying debt).
    touched = sorted(settlements & callees(trace))
    if touched and row["arb_class"] not in {"lp_action", "reverted"}:
        names = ",".join(spec.recipient_names.get(a, a) for a in touched)
        _not_arb(row, "settlement_like", "settlement_contract_called:" + names)
    # A route venue that minted or burned liquidity in this very transaction is being rebalanced, not traded: a
    # position manager (Gamma Hypervisor, a zapper) burns its Uniswap V3 range, collects, swaps and mints again, and
    # the pool's collected tokens had read as a cycle with the manager's fee split as the result (Arbitrum 513003645
    # #3, 513247838 #1) or the operator's own withdrawn liquidity as a $200 "cycle" (Base 52350210 #125). Singleton
    # managers are left out: their liquidity events belong to any pool of the singleton.
    liquidity_emitters = {
        str(lg.get("address", "")).lower()
        for lg in receipt.get("logs") or []
        if str((lg.get("topics") or [""])[0]).lower() in LIQUIDITY_TOPICS
    }
    route_venues = {str(p.get("venue", "")).lower() for p in tv.cycle_path or []} | {
        str(v).lower() for v in tv.venues or []
    }
    rebalanced = sorted((liquidity_emitters & route_venues) - set(spec.pool_managers))
    if rebalanced and row["arb_class"] not in {"reverted"}:
        _not_arb(row, "lp_action", "liquidity_event_at_route_venue:" + ",".join(rebalanced))
    hook_mechanism(spec, row, tv, trace, receipt, event_venues, hints)
    # A delegated order executed through the MetaMask DelegationManager for an account that is not the operator's
    # (the account's exchange with the executor did not close as a cycle) is an order fill, like an intent fill.
    delegated = [n.split(":", 1)[1] for n in tv.notes if n.startswith("delegator_accounts:")]
    if (
        delegated
        and tv.verdict != "cycle"
        and row["arb_class"] not in {"lp_action", "reverted", "settlement_like"}
    ):
        _not_arb(row, "settlement_like", "delegated_order_execution:" + delegated[0])
    # An intent fill event in the receipt (UniswapX Fill, CoW Trade / Settlement) means this transaction executes
    # someone's order: an order fill, never a cycle, whatever the filler keeps.
    fills = sorted({name.split("(")[0] for name in _logged(receipt, INTENT_FILL_TOPICS)})
    if fills and row["arb_class"] not in {"lp_action", "reverted", "settlement_like"}:
        _not_arb(row, "settlement_like", "intent_fill_event:" + ",".join(fills))
    # A Borrow event of a lending protocol in the receipt: the inflow is debt against collateral the party supplied,
    # a position operation whatever surplus the route shows (Ethereum 26140449 #260: a Morpho Blue borrow whose
    # 0.87 USDC "profit" was the loan).
    debt = _logged(receipt, DEBT_EVENT_TOPICS)
    if debt and row["arb_class"] not in {"lp_action", "reverted", "settlement_like"}:
        _not_arb(row, "debt_operation", "debt_inflow:" + ",".join(debt))
    # A liquidation event in the receipt (Aave, Compound, Morpho, Euler, Marginly): the party repays a position's debt
    # and receives its collateral at the protocol's discount — a position operation of the liquidation family, never a
    # cycle, however the collateral is sold afterwards (Arbitrum 513093052 #2: a Marginly `ReceivePosition` bought 0.59
    # WETH 30 % under the pools' price for 0.0138 WBTC and had closed as a $337 "cycle").
    liquidated = _logged(receipt, LIQUIDATION_EVENTS)
    repaid = _logged(receipt, REPAY_EVENT_TOPICS)
    if repaid and row["arb_class"] not in {"lp_action", "reverted", "settlement_like", "debt_operation"}:
        _not_arb(row, "debt_operation", "debt_repayment:" + ",".join(repaid))
    if liquidated and row["arb_class"] not in {"lp_action", "reverted", "settlement_like", "debt_operation"}:
        _not_arb(row, "debt_operation", "liquidation_event:" + ",".join(liquidated))
    bundled_operation(spec, row, trace, receipt)
    # A swap that beat its own pool's pre-swap price (geometry.invariant_violations): the pool's math was broken, so
    # whatever the operator kept was taken from the pool's liquidity providers, not earned from a price difference
    # between venues (OP 0x782ca99b…, KyberSwap Elastic tick-crossing exploit, had been counted as a $109 cycle).
    mark_pool_exploit(row)
    row["sender_wallet_trades"] = sender_wallet_trades(tx, receipt)
    shares = share_operation(tx, receipt)
    if shares and row["arb_class"] not in {"reverted", "pool_exploit"}:
        _not_arb(row, "share_operation", shares)
    if row["arb_class"] in {"settlement_like", "bundled_operation", "debt_operation"} and row[
        "assessment"
    ] in {
        "event_candidate",
        "trace_supported",
    }:
        row["assessment"] = "not_arb"
    if row["arb_class"] == "share_operation":
        row["assessment"] = "not_arb"


def hook_mechanism(spec: ChainSpec, row, tv, trace, receipt, event_venues, hints) -> None:
    """The event layer's cycle may have been executed by the pool's hook inside the party's own swap: every Swap event
    of the loop was emitted for a hook-side swapper (an address reached only inside a hook callback). That is the
    pool's mechanism — an MEV-capturing hook arbitraging its own pool while serving a user's $0.03 buy (Robinhood
    84319662 #1) — never the party's cycle, like the BSC transfer-hook rebalances."""
    if not (
        row["is_arb"]
        and tv.verdict in {"no_taker", "no_venue", "gain_no_route"}
        and row["arb_class"] not in {"lp_action", "reverted", "settlement_like"}
    ):
        return
    hook_side = hook_callees(trace, set(spec.pool_managers)) - hints
    hook_pools: set[str] = set()
    hook_swappers: set[str] = set()
    for lg in receipt.get("logs") or []:
        topics = lg.get("topics") or []
        if (
            len(topics) >= 3
            and str(topics[0]).lower() == V4_SWAP_TOPIC
            and addr("0x" + topics[2][-40:]) in hook_side
        ):
            hook_pools.add(f"{addr(lg['address'])}:{str(topics[1]).lower()}")
            hook_swappers.add(addr("0x" + topics[2][-40:]))
    cycle_pools = {str(v).lower() for v in event_venues}
    if hook_pools and cycle_pools and cycle_pools <= hook_pools:
        _not_arb(row, "hook_mechanism", "cycle_legs_executed_by_hook:" + ",".join(sorted(hook_swappers)))
        row["assessment"] = "not_arb"


def bundled_operation(spec: ChainSpec, row, trace, receipt) -> None:
    """A payment to a declared bundler fee collector (chain.toml `[known_recipients]`, kind `bundler_fee`) means the
    transaction executes bundled users' operations for a fee: the fee is the only "gain" and a WBNB round trip
    of the bundler's mechanics the only "loss" (BSC `0xcf4ee250…`, `0xeb91d9ed…`): never a cycle."""
    fee_collectors = spec.recipients_of_kind("bundler_fee")
    if not fee_collectors or row["arb_class"] in {
        "lp_action",
        "reverted",
        "settlement_like",
        "debt_operation",
    }:
        return
    payees = {
        addr("0x" + lg["topics"][2][-40:])
        for lg in receipt.get("logs") or []
        if len(lg.get("topics") or []) == 3 and str(lg["topics"][0]).lower() == TRANSFER_TOPIC.lower()
    }
    payees |= {addr(receiver) for _, receiver, value in native_flows(trace) if receiver and value > 0}
    hit = sorted(fee_collectors & payees)
    if hit:
        _not_arb(
            row,
            "bundled_operation",
            "bundler_fee_paid:" + ",".join(spec.recipient_names.get(a, a) for a in hit),
        )


def apply_verdict(row, tv, excluded: bool, event_venues) -> None:
    """The trace verdict decides the row: a counted cycle, a loss cycle, a singleton route the events confirm, or a
    row held for review (never silently dropped)."""
    # several collectors folded in by the redirect rule: the sum must stay within what the Swap events establish
    guarded = len(tv.redirects) > 1 and not singleton_result_consistent(row, tv)
    if guarded:
        row["reasons"] = [*row["reasons"], "redirected_collectors_exceed_event_gain"]
    if tv.verdict == "cycle" and tv.gross is not None and tv.gross > 0 and not excluded and not guarded:
        row["assessment"] = "trace_supported"
        row["is_arb"] = True
        row["arb_class"] = "atomic_cycle"
        if not row["n_legs"]:
            row["n_legs"] = len(tv.legs)
            row["venues"] = tv.venues
        row["profit_token"] = tv.profit_token
        row["gross_profit_raw"] = str(tv.gross)
        row["gross_source"] = "trace"
        if row["quote_venues"]:
            row["reasons"] = [*row["reasons"], "rfq_quote_leg:" + ",".join(row["quote_venues"])]
    elif tv.verdict == "loss_cycle" and not excluded:
        row["assessment"] = "trace_supported_loss"
        row["is_arb"] = False
        row["arb_class"] = "loss_cycle"
        row["profit_token"] = tv.profit_token
        row["gross_profit_raw"] = str(tv.gross)
        row["gross_source"] = "trace"
        row["reasons"] = [*row["reasons"], "executed_cycle_with_inventory_loss"]
    elif (
        row["is_arb"]
        and tv.verdict in {"gain_only", "single_venue_loop"}
        and tv.gross is not None
        and tv.gross > 0
        and len(set(event_venues)) >= 2
        and not excluded
        and singleton_result_consistent(row, tv)  # the bound also covers folded-in collectors
    ):
        # Flash accounting settles only the net of the route (Uniswap V4 `unlock`): the operator may pay no explicit
        # input and take output − input in one payment. The receipt-backed result is accepted when it does not
        # exceed what the Swap events say the operator extracted; a larger receipt gain means someone else funded
        # part of the output and the row stays under review.
        # Singleton route (Uniswap V4 / Infinity flash accounting): the Swap events establish >= 2 distinct
        # pools, the receipt-backed movements close the operator's result in one asset. The trace cannot
        # tell pool ids apart, so the route comes from the events and the amount from the receipts/trace.
        row["assessment"] = "trace_supported"
        row["is_arb"] = True
        row["arb_class"] = "atomic_cycle"
        row["profit_token"] = tv.profit_token
        row["gross_profit_raw"] = str(tv.gross)
        row["gross_source"] = "trace"
        row["reasons"] = [*row["reasons"], "singleton_multi_pool_route_receipt_closed"]
    elif row["is_arb"]:
        # A singleton trace cannot distinguish separate V4 pool IDs. Keep the candidate visible.
        row["assessment"] = "needs_review"
        row["is_arb"] = False
    elif (
        tv.verdict in {"gain_no_route", "single_venue_loop"}
        and tv.gross
        and tv.gross > 0
        and not excluded
        and (
            tv.verdict == "gain_no_route"
            or any(n.startswith("uncalled_counterparties:") for n in tv.notes)
            # a singleton route with its surplus converted that the Swap events could not confirm (unresolved or
            # zero-amount hooked legs) stays held like any closed gain without a proven route, never dropped
            or "singleton_route_with_profit_conversion" in tv.notes
        )
    ):
        # the receipts close a gain in one asset but a leg runs through no called venue (an RFQ maker filled by
        # allowance, a user's order): not a cycle under the method, kept visible rather than silently dropped
        row["assessment"] = "needs_review"
        row["is_arb"] = False
        row["reasons"] = [*row["reasons"], "closed_gain_without_called_route"]
    elif not excluded and "receipt_loss_prevents_closure" in tv.notes:
        row["assessment"] = "needs_review"
        row["is_arb"] = False
        row["reasons"] = [*row["reasons"], "open_inventory_loss_not_a_closed_cycle"]
        row["gross_profit_raw"] = None
        row["profit_token"] = None


def flag_trace_only_venues(spec: ChainSpec, row, tv, hints) -> None:
    """A counted cycle whose route runs through a venue only the trace established (no decoded Swap event, not a
    declared quote venue, declared venue or singleton manager) is flagged, not dropped: these legs are where the
    remaining false-positive families hide (order executors, vaults, fee modules) and also where real cycles through
    undecoded venues sit (Fluid, Curve NG, PMMs). The flag ranks the registry backlog per chain."""
    if not (row.get("is_arb") and row.get("assessment") == "trace_supported"):
        return
    registered = (
        set(hints)
        | set(spec.quote_venues or {})
        | set(spec.venue_aliases or {})
        | set(spec.pool_managers)
        | {spec.weth}
    )
    trace_only = sorted({str(leg.venue).lower() for leg in tv.legs} - registered)
    if trace_only:
        row["reasons"] = [*row["reasons"], "trace_only_venue:" + ",".join(trace_only)]


def adopt_embedded_cycle(spec: ChainSpec, row, tv, trace, receipt, hints, settlements) -> None:
    """A row that is not a cycle itself may carry one run by another operator inside it (a helper contract's own loop,
    a hook arbitraging during the user's swap): the first such cycle becomes the row's result, the outer trace kept."""
    secondary = embedded_cycles(
        trace,
        spec.weth,
        receipt.get("logs", []),
        tv,
        hints,
        blocked=set(spec.quote_venues or {}) | settlements,
    )
    if not secondary:
        return
    row["outer_trace"] = row["trace"]
    row["embedded_cycles"] = [candidate.to_json() for candidate in secondary]
    row["trace"] = {"status": "ok", **secondary[0].to_json()}
    row["assessment"], row["arb_class"], row["is_arb"] = "trace_supported", "embedded_cycle", True
    row["operator_scope"] = "embedded"
    row["profit_token"] = secondary[0].profit_token
    row["gross_profit_raw"], row["gross_source"] = str(secondary[0].gross), "trace"
    row["reasons"] = [*row["reasons"], "separate_operator_cycle_inside_transaction"]


def record_payments(spec: ChainSpec, row, tv, trace, tx, evidence, recipients, settlements, miner) -> None:
    """What the transaction paid known recipients: builders (tips) and order-flow kickbacks (Blink and others), costs
    of the strategy kept separate from gas."""
    receipts = evidence["known_recipient_receipts"]
    builders = spec.recipients_of_kind("builder")
    row["known_recipient_received_wei"] = sum(int(v["eth_weth_received_wei"]) for v in receipts.values())
    row["kickback_wei"] = str(
        sum(
            int(v["eth_weth_received_wei"])
            for a, v in receipts.items()
            if a.lower() not in builders and a.lower() not in settlements
        )
    )
    row["known_recipient_payments"] = {
        a: {
            "name": recipients.get(a.lower(), a),
            "kind": "builder"
            if a.lower() in builders
            else ("settlement" if a.lower() in settlements else "kickback"),
            "eth_weth_received_wei": v["eth_weth_received_wei"],
        }
        for a, v in receipts.items()
        if int(v["eth_weth_received_wei"])
    }
    party = {str(tx.get("from", "")).lower(), str(tx.get("to", "") or "").lower()} | set(tv.group)
    builder_paid = sum(int(v["eth_weth_received_wei"]) for a, v in receipts.items() if a.lower() in builders)
    # the whole transaction is the searcher's unless its cycle is embedded in someone else's
    row["builder_tip_wei"] = str(
        builder_tip(trace, None if row.get("operator_scope") != "embedded" else party, miner) + builder_paid
    )


def singleton_result_consistent(row, tv, tol: float = 0.01) -> bool:
    """Receipt-backed singleton gain <= event-layer gain × (1 + tol), same profit token. Zero-amount hook swaps
    make the event figure unreliable; then the receipts decide alone."""
    event = row.get("event_label") or {}
    event_gross = event.get("gross_profit_raw")
    if event_gross is None and row.get("gross_source") == "events":
        event_gross = row.get("gross_profit_raw")
    if row.get("gross_unreliable") or event_gross is None:
        return True
    if any(str(n).startswith("hook_side_venues_dropped:") for n in (tv.notes or [])):
        # the hook's own swaps inside its callback are Swap events the event layer cannot tell from the operator's
        # legs (Robinhood 70067057 #21: the rebalancer's swaps made AAPL the "profit token"); the receipts decide alone
        return True
    if (
        event.get("profit_token")
        and tv.profit_token
        and event["profit_token"].lower() != tv.profit_token.lower()
    ):
        return False
    if tv.gross is None:
        return False
    return tv.gross <= int(event_gross) * (1 + tol)
