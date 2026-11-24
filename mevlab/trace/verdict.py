"""The trace verdict for one transaction: cycle, loss_cycle, single_venue_loop, gain_only, gain_no_route,
no_taker, no_venue, reverted or empty.

Nothing here reads Swap events or the DEX registry, so a disagreement with the event layer (mevlab.receipt) is
real evidence about one of the two. `trace_verdict` runs the stages of a TraceAnalysis in order:

1. collect_movements          what moved (trace frames + the receipt's exact transfers and claims)
2. exclude_operator_side      addresses that act for the operator, never venues
3. read_quote_legs            declared RFQ settlements and their makers
4. find_venues                who traded (and who only took a fee or passed value through)
5. build_legs                 each venue's (in, out) pairs in trace order
6. mark_infrastructure        what the operator trades against
7. find_operator              the operator's group of addresses, its flash loans and its closed gain
8. fold_redirected_outputs    collectors paid straight from the route
9. separate_other_operators   other parties in the same transaction
10. settle_payouts            what left the group: payouts, repayments, inventory
11. decide                    the verdict
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, replace
from typing import Any

from mevlab.decode.signatures import NATIVE_ETH, TRANSFER_TOPIC, ZERO_ADDRESS
from mevlab.hexutil import addr
from mevlab.trace.calls import account_abstraction_sender, callees, delegator_accounts, hook_callees
from mevlab.trace.model import CLOSURE_TOL, DEAD_ADDRESS, EXACT_DUST_SHARE, FlashLoan, Movement, TraceVerdict
from mevlab.trace.movements import claim_movements, movements, nets, terminal_receivers
from mevlab.trace.operator import closed_gain, flash_lenders, party_group, party_view, pass_through_chain
from mevlab.trace.route import continuity_gap, returning_route, route_surplus
from mevlab.trace.venues import (
    conduits,
    foreign_claim_recipients,
    hook_fee_recipients,
    legs_of,
    maker_candidates,
    singleton_legs,
    swap_lockers,
    venues_of,
)

# A leg taking more than twice what the previous leg delivered is fed from outside the route (continuity_gap >= 1).
DISCONTINUITY_LIMIT = 1.0


def trace_verdict(
    trace: dict[str, Any],
    weth: str,
    tx_from: str | None = None,
    tx_to: str | None = None,
    logs: list[dict[str, Any]] | None = None,
    venue_hints: set[str] | None = None,
    fee_takers: set[str] | None = None,
    singletons: set[str] | None = None,
    known_recipients: set[str] | None = None,
    quote_venues: set[str] | None = None,
    venue_aliases: dict[str, str] | None = None,
) -> TraceVerdict:
    """`venue_hints`: addresses the event layer established as venues (pools, singleton pool managers).
    They are infrastructure: never absorbed into the operator's group. A singleton manager that only moves
    one asset in and out (flash accounting with a native leg) is otherwise invisible to venues_of().
    `fee_takers`: hook contracts of the pools on the route; what a venue pays them is a fee of the swap, so
    they are never taken as the operator's collector.
    `quote_venues`: declared RFQ settlement contracts (chain.toml `[quote_venues]`). A market maker whose
    inventory such a contract moved by allowance inside its own frame is read as that contract's leg: the
    called settlement is the venue, the maker's wallet is where its inventory sits. Undeclared settlements
    keep their makers as `uncalled_counterparties` (reported, not counted).
    `venue_aliases`: member address -> venue address for venues built from several contracts (the Maker LitePSM takes
    DAI into one contract and pays USDC from its pocket): the members' movements are read as the venue's own."""
    a = TraceAnalysis.start(
        trace, weth, tx_from, tx_to, logs, venue_hints, fee_takers, singletons, known_recipients
    )
    early = a.collect_movements(quote_venues, venue_aliases)
    if early is not None:
        return early
    a.exclude_operator_side()
    a.read_quote_legs()
    a.find_venues()
    a.build_legs()
    a.mark_infrastructure()
    a.find_operator()
    a.fold_redirected_outputs()
    a.separate_other_operators()
    a.settle_payouts()
    return a.decide()


def _addresses(values) -> set[str]:
    return {addr(h) for h in (values or set()) if h}


@dataclass
class TraceAnalysis:
    """Working state of one trace verdict; each method is one stage and reads what the earlier ones set."""

    trace: dict[str, Any]
    weth: str
    tx_from: str
    tx_to: str | None
    logs: list[dict[str, Any]] | None
    hints: set[str]  # venues the event layer established
    takers: set[str]  # hook fee takers: never the operator's collector
    managers: set[str]  # singleton pool managers
    known_payees: set[str]  # builders, kickback and bundler-fee recipients
    bundler: str | None = None
    account: str | None = None  # ERC-4337 account the bundler executed
    movs: list[Movement] = field(default_factory=list)
    exclude: set[str] = field(default_factory=set)  # operator-side addresses
    delegated: set[str] = field(default_factory=set)
    self_sellers: set[str] = field(default_factory=set)
    called: set[str] = field(default_factory=set)
    makers: set[str] = field(default_factory=set)
    quote_legs: dict[str, set[str]] = field(default_factory=dict)
    venues: list[str] = field(default_factory=list)
    sinks: set[str] = field(default_factory=set)
    token_contracts: set[str] = field(default_factory=set)
    hook_side: set[str] = field(default_factory=set)
    tv: TraceVerdict = field(default_factory=lambda: TraceVerdict("no_venue"))
    net: dict[str, dict[str, int]] = field(default_factory=dict)
    infra: set[str] = field(default_factory=set)
    paid_in: set[str] = field(default_factory=set)
    got_out: set[str] = field(default_factory=set)
    seeds: list[str] = field(default_factory=list)
    never: set[str] = field(default_factory=set)  # never part of the operator
    group: set[str] = field(default_factory=set)
    loans: list[FlashLoan] = field(default_factory=list)
    vnet: dict[str, int] = field(default_factory=dict)  # the group's net per asset at the venues
    fl: dict[str, int] = field(default_factory=dict)  # flow per asset (the scale of closure tolerances)
    pay: dict[str, dict[str, int]] = field(default_factory=dict)  # token -> recipient -> what the group paid
    entry: str | None = None
    cg: tuple | None = None  # closed gain: (token, gross, residual tokens)
    protocol: dict[str, dict[str, int]] = field(default_factory=dict)

    @classmethod
    def start(cls, trace, weth, tx_from, tx_to, logs, venue_hints, fee_takers, singletons, known_recipients):
        tx_from = addr(tx_from or trace.get("from") or ZERO_ADDRESS)
        tx_to = addr(tx_to) if tx_to else (addr(trace["to"]) if trace.get("to") else None)
        bundler: str | None = None
        account = account_abstraction_sender(trace)
        if account:
            bundler, tx_from = tx_from, account
        return cls(
            trace=trace,
            weth=weth.lower(),
            tx_from=tx_from,
            tx_to=tx_to,
            logs=logs,
            hints=_addresses(venue_hints),
            takers=_addresses(fee_takers),
            managers=_addresses(singletons),
            known_payees=_addresses(known_recipients),
            bundler=bundler,
            account=account,
        )

    @property
    def exact(self) -> bool:
        """ERC-20 amounts come from the receipt (exact), not from transfer calldata."""
        return self.logs is not None

    @property
    def callee(self) -> set[str]:
        return {self.tx_to} if self.tx_to else set()

    # 1 ----------------------------------------------------------------------------------------------------------------
    def collect_movements(self, quote_venues, venue_aliases) -> TraceVerdict | None:
        """Every value movement plus the receipt's ERC-6909 claim movements; the members of a declared multi-contract
        venue are read as the venue. Returns the verdict when there is nothing to read."""
        movs, reverted = movements(self.trace, self.weth, self.logs, quote_venues)
        if reverted:
            return TraceVerdict("reverted")
        if self.logs:
            claims = claim_movements(self.logs)
            if claims:
                movs = movs + [
                    Movement(
                        self.weth if m.token == ZERO_ADDRESS else m.token,
                        m.sender,
                        m.receiver,
                        m.amount,
                        m.order,
                        m.kind,
                    )
                    for m in claims
                ]
        if not movs:
            return TraceVerdict("empty")
        if venue_aliases:
            alias = {addr(k): addr(v) for k, v in venue_aliases.items()}
            movs = [
                replace(m, sender=alias.get(m.sender, m.sender), receiver=alias.get(m.receiver, m.receiver))
                for m in movs
            ]
            movs = [m for m in movs if m.sender != m.receiver]
            if not movs:
                return TraceVerdict("empty")
        self.movs = movs
        return None

    # 2 ----------------------------------------------------------------------------------------------------------------
    def exclude_operator_side(self) -> None:
        """Addresses that act for the operator (or for the party an order is filled for) and are never venues."""
        movs, hints, weth = self.movs, self.hints, self.weth
        # The callee is the operator's contract — unless it is a venue the event layer established (an EIP-7702
        # account calling the singleton pool manager directly): then the sender alone is the operator.
        exclude = ({self.tx_from} | self.callee | ({self.bundler} if self.bundler else set())) - hints
        # A contract that pays a known recipient (builder tip, kickback, bundler fee) out of its own balance is the
        # operator's vault or executor, never a venue, whatever it exchanged on the way (BSC 126512303 #14: the vault
        # funding a buy and tipping the 48 Club was read as the second venue, the tip as the gain).
        fee_payers = {
            m.sender
            for m in movs
            if m.receiver in self.known_payees and m.sender not in hints and m.sender != weth
        }
        # ...also through one forwarder: the executor hands tx_to the exact amount tx_to passes on to the builder
        # (BSC 126500003 #2: 0.000062152 BNB executor -> forwarder -> 48 Club).
        forwarded = {
            (m.token, m.amount, m.sender)
            for m in movs
            if m.receiver in self.known_payees and m.sender in exclude
        }
        fee_payers |= {
            m.sender
            for m in movs
            if (m.token, m.amount, m.receiver) in forwarded and m.sender not in hints and m.sender != weth
        }
        exclude |= fee_payers
        # A smart account whose delegation the MetaMask DelegationManager redeems (`executeFromExecutor`) is the party
        # the order is executed for: its exchange with the executor is an order fill whose price improvement the
        # executor keeps, never a venue's quote. The account that paid 1 USDC for exactly 22.9 tokens bought for 0.928
        # by its Spender had closed as a 0.0716 USDC "cycle" (Arbitrum 513260119 #3), as had two fills whose executors
        # paid the improvement to their fee wallets (Arbitrum 512990139 #1, 512995745 #1). A bot's own account
        # executing its cycle through the manager is operator-side anyway and stays a cycle.
        self.delegated = delegator_accounts(self.trace) - hints
        exclude |= self.delegated
        # A token contract transferring its own existing balance (a plain Transfer from itself, not a mint) is a
        # holder selling inventory: a taxed token's swapBack dumping its accumulated tax through the router (BSC
        # 126573593 #37: the token contract sold 5.17 tokens on PancakeSwap V2 and forwarded the 0.001454 BNB to its
        # marketing wallet; the router and the wallet had been read as the operator, the token contract as the first
        # venue and the whole sale as the gain). Issuers are venues on mint/redemption legs only.
        self.self_sellers = {
            m.sender
            for m in movs
            if m.kind == "receipt_transfer"
            and m.sender == m.token
            and m.sender not in hints
            and m.sender != weth
        }
        exclude |= self.self_sellers
        self.exclude = exclude

    # 3 ----------------------------------------------------------------------------------------------------------------
    def read_quote_legs(self) -> None:
        """A maker whose inventory a called, declared RFQ settlement moved is read as that settlement's leg."""
        self.called = callees(self.trace)
        self.makers = maker_candidates(self.movs, self.called, self.exclude, self.hints)
        quote_legs: dict[str, set[str]] = {}
        for m in self.movs:
            if m.via and m.via in self.called and m.via not in self.exclude:
                for who in (m.sender, m.receiver):
                    if who in self.makers:
                        quote_legs.setdefault(m.via, set()).add(who)
        if quote_legs:
            # the declared settlement is the venue; every movement of its maker is the settlement's own
            maker_of = {maker: venue for venue, ms in quote_legs.items() for maker in ms}
            movs = [
                replace(
                    m, sender=maker_of.get(m.sender, m.sender), receiver=maker_of.get(m.receiver, m.receiver)
                )
                for m in self.movs
            ]
            self.movs = [m for m in movs if m.sender != m.receiver]
            self.makers = maker_candidates(self.movs, self.called, self.exclude, self.hints)
        self.quote_legs = quote_legs

    # 4 ----------------------------------------------------------------------------------------------------------------
    def find_venues(self) -> None:
        """Venues, minus the pools' hook-side contracts and pass-through helpers; token sinks; the verdict's notes on
        everything excluded so far."""
        movs, hints, exclude, weth = self.movs, self.hints, self.exclude, self.weth
        venues = venues_of(movs, exclude, self.called)
        # Receivers of token side effects (transfer taxes, reflections, dividend shares emitted inside a transfer the
        # sender directed elsewhere) are the token's sinks: what reaches them is a cost of moving the token, like a
        # pool fee, never value the operator keeps. BTDAO's dividend distributor 0x75d5f37c (BSC 131010461 #5) took a
        # 1.5 % cut of the bot's transfer and paid holders; folded into the operator it turned the tax into a "profit"
        # and its dividend payouts into an open loss the receipt audit flagged on 20 BSC rows (2026-11-01).
        # A receiver that also trades (a token contract converting its own token, a pool) stays a venue.
        self.sinks = (
            {m.receiver for m in movs if m.kind == "receipt_side_effect"}
            - set(venues)
            - exclude
            - self.known_payees
            - hints
            - self.managers
            - {self.tx_from, weth}
            - self.callee
        )
        self.token_contracts = {
            addr(lg["address"])
            for lg in (self.logs or [])
            if str((lg.get("topics") or [""])[0]).lower() == TRANSFER_TOPIC
        } | {weth}
        self.hook_side = hook_callees(self.trace, self.managers) - hints - exclude - self.token_contracts
        hook_side_dropped = sorted(self.hook_side & set(venues))
        venues = [v for v in venues if v not in self.hook_side]
        self.takers |= self.hook_side
        helpers = conduits(movs, venues, exclude, hints)
        if helpers:
            venues = [v for v in venues if v not in helpers]
        self.venues = venues
        self.tv = TraceVerdict("no_venue", n_movements=len(movs), venues=venues)
        notes = self.tv.notes
        if hook_side_dropped:
            notes.append("hook_side_venues_dropped:" + ",".join(hook_side_dropped))
        if self.delegated:
            notes.append("delegator_accounts:" + ",".join(sorted(self.delegated)))
        if helpers:
            notes.append("pass_through_helpers:" + ",".join(sorted(helpers)))
        if self.self_sellers:
            notes.append("self_selling_token_contracts:" + ",".join(sorted(self.self_sellers)))
        for venue, ms in sorted(self.quote_legs.items()):
            notes.append(f"rfq_quote_leg:{venue}:" + ",".join(sorted(ms)))
        if self.makers:
            notes.append("uncalled_counterparties:" + ",".join(sorted(self.makers)))
        if self.exact:
            notes.append("erc20_amounts_from_receipt")
        if any(m.kind in {"receipt_mint", "receipt_burn"} for m in movs):
            notes.append("token_mint_redemption_flows")
        if self.account:
            notes.append(f"erc4337_account:{self.account}")

    # 5 ----------------------------------------------------------------------------------------------------------------
    def build_legs(self) -> None:
        """Each venue's (in, out) pairs in trace order; a singleton manager's legs are its pools' net flows."""
        tv = self.tv
        tv.legs = legs_of(self.movs, self.venues)
        hubs = {v for v in self.venues if v in self.managers}
        if hubs:
            tv.legs = sorted(
                [lg for lg in tv.legs if lg.venue not in hubs] + singleton_legs(self.movs, hubs),
                key=lambda x: x.order,
            )
        tv.tokens = sorted({t for lg in tv.legs for t in (lg.token_in, lg.token_out) if t})

    # 6 ----------------------------------------------------------------------------------------------------------------
    def mark_infrastructure(self) -> None:
        """What the operator trades against (venues, WETH, the event layer's venues, token sinks), and who paid into
        it or was paid out of it."""
        movs, exclude = self.movs, self.exclude
        self.net = nets(movs)
        if self.venues:
            infra = set(self.venues) | {self.weth}
        else:
            # flash accounting inside one singleton (or a withdrawal): only the net result moves; the payer,
            # or the one address party to every movement, plays the venue
            hub = (
                {
                    a
                    for a in {m.sender for m in movs} | {m.receiver for m in movs}
                    if all(a in (m.sender, m.receiver) for m in movs)
                }
                - exclude
                - terminal_receivers(movs)
            )
            infra = (({m.sender for m in movs} - {m.receiver for m in movs}) - exclude) | hub | {self.weth}
        infra |= self.hints - exclude
        if self.sinks:
            infra |= self.sinks
            self.tv.notes.append("token_side_effect_sinks:" + ",".join(sorted(self.sinks)))
        if self.hints & infra and not self.venues:
            self.tv.notes.append("venue_hints_applied")
        trading = infra - self.sinks  # paying a token's tax sink is not trading at a venue
        self.paid_in = {m.sender for m in movs if m.receiver in trading and m.sender not in infra}
        self.got_out = {m.receiver for m in movs if m.sender in trading and m.receiver not in infra}
        self.infra = infra

    # 7 ----------------------------------------------------------------------------------------------------------------
    def find_operator(self) -> None:
        """The operator's group (callee, sender, executors), its flash loans, and its result: closed in one asset or
        not."""
        movs, infra = self.movs, self.infra
        seeds = [c for c in (self.tx_to, self.tx_from) if c and c not in infra]
        # Never the operator, whatever value they exchange with it: token contracts (issuers mint, burn and redeem),
        # the pools' hooks, singleton pool managers and the known builder / kickback payees. Folding them in made
        # profits out of a vault redemption (BSC 0xbfe33140 $13.2k), a hook's fee residue (Base 0x2eabba74 $46) and a
        # keeper zap's token contract (Robinhood 0x06526f39) — manual review 2026-10-31.
        never = (
            (self.token_contracts - {self.weth})
            | self.takers
            | self.hook_side
            | self.managers
            | self.known_payees
            | self.sinks
        )
        group = party_group(seeds, movs, infra, never=never)
        if not (group & (self.paid_in | self.got_out)):
            # the callee never touches a venue itself: an executor contract does; seed from the first payer
            first = next((m.sender for m in movs if m.sender in self.paid_in), None)
            if first:
                seeds = [first]
                group = party_group(seeds, movs, infra, never=never)
        loans = flash_lenders(group, seeds, movs, infra)
        if loans:
            group = party_group(seeds, movs, infra, stop={fl_.lender for fl_ in loans}, never=never)
        self.seeds, self.never, self.loans, self.group = seeds, never, loans, group
        self.vnet, self.fl, self.pay, self.entry = party_view(group, movs, infra)
        self.cg = closed_gain(self.vnet, self.fl, self.entry, exact=self.exact) if self.vnet else None
        if self.exact and self.cg is None and closed_gain(self.vnet, self.fl, self.entry) is not None:
            self.tv.notes.append("receipt_loss_prevents_closure")

    # 8 ----------------------------------------------------------------------------------------------------------------
    def fold_redirected_outputs(self) -> None:
        """The last leg's output (or a V4 claim mint) may go straight to addresses the operator never exchanges
        value with — its collector, the builder, a kickback recipient — possibly through pass-through
        forwarders; with pool-to-pool legs (or a singleton paying the result out directly) the operator may
        touch no token at all. Fold every such receiver whose receipt closes (or raises) the group's gain in
        one asset into the group. Hook fee takers (the pools' registered hooks and the recipients a hook's
        fee event accounts for) are the venue's payees, never the operator's collector."""
        movs, infra, logs, group, tv = self.movs, self.infra, self.logs, self.group, self.tv
        cg = self.cg
        dust_gain = cg is not None and self.exact and cg[1] <= self.fl.get(cg[0], 0) * EXACT_DUST_SHARE
        if not ((cg is None or dust_gain) and (self.venues or self.hints & infra)):
            return
        term = terminal_receivers(movs)
        fee_paid = hook_fee_recipients(movs, logs, infra, self.weth) if logs else set()
        fee_paid |= foreign_claim_recipients(
            logs, set(group) | set(self.seeds) | {self.tx_from} | self.callee | swap_lockers(logs)
        )
        skipped_takers = {
            m.receiver
            for m in movs
            if m.sender in infra
            and m.receiver in term
            and (m.receiver in self.takers or m.receiver in fee_paid)
        }
        if skipped_takers:
            tv.notes.append("hook_fee_recipients:" + ",".join(sorted(skipped_takers)))
        not_payees = self.never - self.known_payees
        candidates: list[tuple[str, frozenset]] = []
        for m in movs:
            if m.sender not in infra or m.receiver in group:
                continue
            r, chain = pass_through_chain(m.receiver, movs, infra, group, term)
            if r is None or r in group or r in self.takers or r in fee_paid or r in infra:
                continue
            if r in not_payees or set(chain) & not_payees:
                # the `never` rule holds for redirects too: a burn is booked to its token contract, so a token
                # burned by a converter (Sky's DAI->USDS 0x3225737a) had been folded in as the operator's
                # "collector" and the burned 268.42 DAI read as a $268 gain on a $0.29 loop (Ethereum 26316647 #123).
                # Known payees (builders, kickbacks) stay eligible: a tip paid straight out of the route is part of the
                # gross and is then charged as a cost (8 Ethereum labels' exact proceeds depend on it).
                continue
            if (r, frozenset(chain)) not in candidates:
                candidates.append((r, frozenset(chain)))
        best: tuple | None = None
        grown = set(group)
        for r, chain in candidates:
            g2 = grown | set(chain) | {r}
            vnet2, fl2, _, entry2 = party_view(g2, movs, infra)
            cg2 = closed_gain(vnet2, fl2, entry2, exact=self.exact) if vnet2 else None
            if self.exact and cg2 is None and closed_gain(vnet2, fl2, entry2) is not None:
                if "receipt_loss_prevents_closure" not in tv.notes:
                    tv.notes.append("receipt_loss_prevents_closure")
                tv.unclosed_candidates.append(
                    {"possible_recipient": r, "asset_deltas": {t: str(v) for t, v in vnet2.items() if v}}
                )
            if cg2 is not None and (best is None or (cg2[0] == best[0] and cg2[1] > best[1])):
                grown, best = g2, cg2
                tv.redirects.append(r)
                tv.notes.append(f"output_redirected_to:{r}")
        if best is not None:
            self.group, self.cg = grown, best
            self.vnet, self.fl, self.pay, self.entry = party_view(self.group, movs, infra)

    # 9 ----------------------------------------------------------------------------------------------------------------
    def separate_other_operators(self) -> None:
        """Other operators in the same transaction: every venue counterparty outside the group seeds its own
        party; one that pays the same collector as the group is the same operator and is merged in."""
        movs, infra = self.movs, self.infra
        taken = set(self.group) | {fl_.lender for fl_ in self.loans} | infra
        for c in [m.sender for m in movs if m.sender in self.paid_in and m.sender in self.got_out]:
            if c in taken:
                continue
            g2 = party_group([c], movs, infra, stop=taken, never=self.never) - taken
            if not g2:
                continue
            taken |= g2
            v2, f2, p2, e2 = party_view(g2, movs, infra)
            cg2 = closed_gain(v2, f2, e2, exact=self.exact) if v2 else None
            if cg2 is None:
                continue
            shared = {r for d_ in self.pay.values() for r in d_} & {r for d_ in p2.values() for r in d_}
            if shared:
                self.group = self.group | g2
                self.vnet, self.fl, self.pay, self.entry = party_view(self.group, movs, infra)
                self.cg = closed_gain(self.vnet, self.fl, self.entry, exact=self.exact) if self.vnet else None
                self.tv.notes.append(f"merged_same_collector:{c}")
            else:
                self.tv.other_parties.append(
                    {
                        "taker": c,
                        "token": cg2[0],
                        "gross": cg2[1],
                        "payouts": {t: dict(d_) for t, d_ in p2.items()},
                    }
                )

    # 10 ---------------------------------------------------------------------------------------------------------------
    def settle_payouts(self) -> None:
        """What left the group: flash fees, payouts to collectors, repayments to token contracts (not proceeds) and
        payouts funded from inventory (not proceeds either). Records the group's evidence on the verdict."""
        movs, group, infra, tv = self.movs, self.group, self.infra, self.tv
        lenders = {fl_.lender for fl_ in self.loans}
        pay = {
            t: {k: v for k, v in d_.items() if k not in lenders} for t, d_ in self.pay.items()
        }  # principal
        for fl_ in self.loans:
            # Whatever the lender got back beyond its principal left the group: a flash fee (Aave 0.05 %) or a
            # funder taking the result (Robinhood funders, 0.5-17 % of principal). Both are reported as a payout to
            # the lender, whose ownership of the operator is unverified like any other recipient's.
            surplus = fl_.repaid - fl_.principal
            if surplus > 0:
                pay.setdefault(fl_.token, {})
                pay[fl_.token][fl_.lender] = pay[fl_.token].get(fl_.lender, 0) + surplus
        pay = {t: d_ for t, d_ in pay.items() if d_}
        tv.flash_loans = self.loans
        tv.entry_token = self.entry
        # Retain the inferred group/payment evidence even when its gain does not close.
        # Raw native/token/claim evidence is recorded separately by cashflows.py.
        tv.group = sorted(group)
        tv.asset_deltas = {t: v for t, v in self.vnet.items() if v}
        group_deltas = {t: sum(self.net.get(a, {}).get(t, 0) for a in group) for t in {m.token for m in movs}}
        tv.group_asset_deltas = {t: v for t, v in group_deltas.items() if v}
        terminal = terminal_receivers(movs) - group - infra
        tv.venue_terminal_receipts = [
            {
                "venue": m.sender,
                "recipient": m.receiver,
                "token": m.token,
                "amount_raw": str(m.amount),
                "kind": m.kind,
                "ownership_verified": False,
            }
            for m in movs
            if m.sender in infra and m.receiver in terminal
        ]
        # A payout sent to a token contract itself (DAI returned to the DAI contract after a flash mint, an asset
        # pushed into its wrapper) is a repayment or a burn, not the operator's collector: it leaves the proceeds.
        # Any other payee — a fee collector, a vault, a builder, a kickback recipient — is where the result went and
        # stays a payout (ownership unverified, costs reported separately).
        emitters = {
            addr(lg["address"])
            for lg in (self.logs or [])
            if str((lg.get("topics") or [""])[0]).lower() == TRANSFER_TOPIC
        } - self.takers
        # ...and a transfer to the conventional dead address is a burn like one to the zero address: a router's fee
        # module buying SHIB with its 1 % cut and burning it had closed as an 83,082 SHIB "cycle" (Base 52327874 #30)
        emitters.add(DEAD_ADDRESS)
        protocol: dict[str, dict[str, int]] = {}
        for t, d_ in list(pay.items()):
            for who, v in list(d_.items()):
                if who in emitters and who not in self.known_payees and who not in lenders:
                    protocol.setdefault(t, {})[who] = v
                    del d_[who]
            if not d_:
                del pay[t]
        # Payouts beyond what the group received in the transaction were paid from its pre-existing balance: inventory
        # moving, not proceeds of the cycle. Ethereum 26137380 #168: 0.0203 ETH of a tip pre-funded one transaction
        # earlier went back to the funder on top of the 0.0139 WETH the legs made. Venue legs are never discounted (a
        # loss at a venue stays a loss): only payments to counterparties, the unknown collectors first.
        tv.payout_recipients = {t: sorted(d_) for t, d_ in pay.items() if d_}
        for t in list(pay):
            inflow = sum(
                m.amount for m in movs if m.token == t and m.receiver in group and m.sender not in group
            )
            outflow = sum(
                m.amount for m in movs if m.token == t and m.sender in group and m.receiver not in group
            )
            inventory = min(outflow - inflow, sum(pay[t].values()))
            if inventory <= 0:
                continue
            tv.notes.append(f"inventory_payout:{t}:{inventory}")
            for who in sorted(pay[t], key=lambda w: (w in self.known_payees, -pay[t][w])):
                cut = min(inventory, pay[t][who])
                pay[t][who] -= cut
                inventory -= cut
                if pay[t][who] == 0:
                    del pay[t][who]
                if inventory == 0:
                    break
            if not pay[t]:
                del pay[t]
        tv.protocol_payments = protocol
        tv.payouts_by_token = pay
        self.pay, self.protocol = pay, protocol

    # 11 ---------------------------------------------------------------------------------------------------------------
    def decide(self) -> TraceVerdict:
        self.tv.taker = self.seeds[0] if self.seeds else None
        return self._unclosed() if self.cg is None else self._closed()

    def _unclosed(self) -> TraceVerdict:
        """No closed gain: no_taker / no_venue, or a loss_cycle when a closed route lost in exactly one asset."""
        tv, vnet, fl = self.tv, self.vnet, self.fl
        tv.verdict = "no_taker" if self.venues else "no_venue"
        sig_pos = [t for t, v in vnet.items() if v > CLOSURE_TOL * fl.get(t, 0)]
        sig_neg = [t for t, v in vnet.items() if v < 0 and (self.exact or -v > CLOSURE_TOL * fl.get(t, 0))]
        if len(sig_pos) > 1:
            tv.notes.append("multi_asset_gain")
        if sig_neg:
            tv.notes.append("open_loss:" + ",".join(sig_neg))
        # An executed cyclic trade can lose money. Keep its signed result instead
        # of treating a known loss as an unresolved profitable-arbitrage candidate.
        negatives = {t: v for t, v in vnet.items() if v < 0}
        if not (self.exact and len(negatives) == 1 and not any(v > 0 for v in vnet.values())):
            return tv
        token = next(iter(negatives))
        path = returning_route(tv.legs, token)
        paid = sum(
            m.amount
            for m in self.movs
            if m.token == token and m.sender in self.group and m.receiver in self.infra
        )
        received = sum(
            m.amount
            for m in self.movs
            if m.token == token and m.receiver in self.group and m.sender in self.infra
        )
        # The loss is the route's only when every swap's output is spent by another swap or is the loss token
        # itself: a swap whose output goes nowhere on the route bought something for someone. Ethereum 26317299
        # #1 ran a 0.0081 WETH loop through ADX (+0.0000107) and, in the same call, bought 8,439.8 LSK for
        # 0.8702 ETH delivered to 0x9f484667: the purchase had read as a $1,976 "loss cycle".
        spent = {self._asset(lg.token_in) for lg in tv.legs if lg.token_in and lg.amount_in > 0}
        stray = (
            {self._asset(lg.token_out) for lg in tv.legs if lg.token_out and lg.amount_out > 0}
            - spent
            - {self._asset(token)}
        )
        if stray:
            tv.notes.append("loss_with_outputs_off_route:" + ",".join(sorted(stray)))
        if path and paid > 0 and received > 0 and not stray:
            tv.verdict = "loss_cycle"
            tv.cycle_path = path
            tv.profit_token, tv.gross = token, negatives[token] - sum(self.protocol.get(token, {}).values())
            tv.net_after = tv.group_asset_deltas.get(token, 0)
            tv.notes.append("closed_route_negative_result")
        return tv

    def _asset(self, token: str) -> str:
        return self.weth if token == NATIVE_ETH else token

    def _closed(self) -> TraceVerdict:
        """A gain closed in one asset: a cycle only through a returning route over two or more distinct venues."""
        tv = self.tv
        tok, gross, residual = self.cg
        consumed = sum(self.protocol.get(tok, {}).values())
        if consumed:
            tv.notes.append(f"protocol_payments:{consumed}")
            gross -= consumed
            if gross <= 0:
                # the whole result was paid to a protocol contract (Ethereum 0x57d2f2c9…: WBTC collateral sold on CoW
                # to repay the position's USDT debt): a position operation, not a cycle
                tv.verdict = "no_taker"
                tv.notes.append("proceeds_consumed_by_protocol_payments")
                return tv
        if self.exact and gross <= self.fl.get(tok, 0) * EXACT_DUST_SHARE:
            # Receipt-exact amounts: a surplus below one part in 10^8 of the token's flow is rounding, not a profit —
            # the same bound that keeps rounding dust from opening a loss. A protocol vault rebalancing CT against a
            # pool inside a token's transfer hook left 2 wei on 1,680 USDT (BSC 126516560 #3, #9): a flat inventory
            # move, not a cycle.
            tv.verdict = "no_taker"
            tv.notes.append(f"round_trip_dust_surplus:{gross}")
            return tv
        tv.profit_token, tv.gross = tok, gross
        tv.net_after = sum(self.net.get(a, {}).get(tok, 0) for a in self.group)
        tv.payouts = dict(self.pay.get(tok, {}))
        tv.extra_gains = {t: v for t, v in self.vnet.items() if t != tok and v > 0}
        if residual:
            tv.notes.append("positive_residual:" + ",".join(residual))
        if len(self.group) > len(self.seeds):
            tv.notes.append(f"group_size:{len(self.group)}")
        venues, managers = self.venues, self.managers
        if not venues:
            tv.verdict = "gain_only"
            tv.notes.append("singleton_flash_accounting_or_withdrawal")
        elif len(venues) < 2:
            tv.verdict = "single_venue_loop"
        elif managers and set(venues) <= managers:
            # every venue is a singleton manager (Uniswap V4, Ekubo, Infinity): the pools of the route are telling
            # apart only in the Swap events, which the caller checks against this receipt-closed result
            tv.verdict = "single_venue_loop"
            tv.notes.append("singleton_only_route")
        else:
            self._route(tok, gross)
        return tv

    def _fed_from_outside(self, path: list[dict[str, Any]]) -> bool:
        """True when more than half of some route leg's input reached its venue from a party that is neither the
        operator's group nor a venue of the transaction (pool-to-pool routing pays the next pool directly, and a
        split route's other pools feed the same pool: BSC 0x02ccfe78…, three pools paying one)."""
        inside = (
            set(self.group)
            | {str(v).split(":")[0] for v in self.venues}
            | set(self.managers or ())
            | {str(leg["venue"]).split(":")[0] for leg in path}
        )
        for leg in path[1:]:
            venue, token = str(leg["venue"]).split(":")[0], leg["token_in"]
            outside = sum(
                m.amount
                for m in self.movs
                if m.receiver == venue and self._asset(m.token) == self._asset(token) and m.sender not in inside
            )
            if outside * 2 > int(leg["amount_in"]):
                return True
        return False

    def _route(self, tok: str, gross: int) -> None:
        """Rule: a cycle is a closed chain in one asset through distinct called venues, not merely a party that
        ends net positive. Without a returning route the gain is a fee, a rebate or a pass-through, never an arb.
        The chain may close in the entry asset and convert the surplus afterwards (USDC cycle, profit taken in
        WETH): search the profit token first, then the entry token, then any token the group routed."""
        tv, managers = self.tv, self.managers
        candidates = [tok] + [t for t in (self.entry, *[lg.token_in for lg in tv.legs]) if t and t != tok]
        path, cycle_token = [], tok
        for candidate in dict.fromkeys(candidates):
            path = returning_route(tv.legs, candidate)
            if path:
                cycle_token = candidate
                break
        if not path and managers and set(self.venues) & managers:
            # A singleton nets its pools' flows, so two pools of one pair inside it (Uniswap V4 USDT -> X -> USDT)
            # leave no legs, only the surplus it paid out; when every other leg converts that surplus into the profit
            # token (USDT -> WBNB on V3, BSC 131019461 #21), the route is the singleton's: its pools are told apart by
            # the Swap events, which the caller checks against this receipt-closed result like any singleton route.
            hub_net: dict[str, int] = defaultdict(int)
            for m in self.movs:
                if m.sender in managers and m.receiver in self.group:
                    hub_net[m.token] += m.amount
                elif m.receiver in managers and m.sender in self.group:
                    hub_net[m.token] -= m.amount
            outside = [lg for lg in tv.legs if lg.venue not in managers]
            if outside and all(lg.token_out == tok and hub_net.get(lg.token_in, 0) > 0 for lg in outside):
                tv.verdict = "single_venue_loop"
                tv.notes += ["singleton_only_route", "singleton_route_with_profit_conversion"]
                return
        if not path:
            tv.verdict = "gain_no_route"
            tv.notes.append("closed_gain_without_returning_route")
            return
        if cycle_token != tok:
            tv.notes.append(f"cycle_token:{cycle_token}")
        tv.cycle_path = path
        if self.entry is None and cycle_token == tok:
            # The operator paid nothing into any venue: its result can only be what the route itself produced
            # (what the last venue delivered minus what the first leg took). A "venue" handing the group more
            # than that is paying from inventory — an operator-owned position contract withdrawing 721.97 WETH of
            # Aave collateral (Ethereum 0x3d4efb0c…), a keeper-run vault distributing 107.9 NVDA against a
            # 0.00095 NVDA round trip (Robinhood 0x1e7e2d1e…), a wrap-and-deposit flow with a zero-surplus
            # mint/redeem (Base 0x052d4542…) — not a cycle.
            last = path[-1]["venue"]
            delivered = sum(lg.amount_out for lg in tv.legs if lg.venue == last and lg.token_out == tok)
            surplus = delivered - int(path[0]["amount_in"])
            if surplus <= 0 or gross * 100 > surplus * 101:
                tv.verdict = "gain_no_route"
                tv.notes.append(f"gain_exceeds_route_surplus:{surplus}")
                return
        gap = continuity_gap(path)
        if gap is not None:
            tv.notes.append(f"amount_discontinuity:{gap:.4f}")
            if cycle_token == tok and gap >= DISCONTINUITY_LIMIT and self._fed_from_outside(path):
                # A leg paid for mostly by someone outside the operator and the route's own pools: a router filling
                # a user's sale, not an arbitrage. BSC 0x640c0286…: 28.9 USDT -> 0.052 WBNB, then 315 WBNB sent into
                # the pool by another contract -> 175,654 USDT, paid back to the user, booked as a $175.6k cycle.
                # The route can only claim what its own legs' rates produce. (A split route's extra input comes from
                # the operator's other legs, so it never reaches this test.)
                surplus = route_surplus(path)
                if surplus <= 0 or gross * 100 > surplus * 101:
                    tv.verdict = "gain_no_route"
                    tv.notes.append(f"gain_exceeds_route_surplus:{surplus}")
                    return
        tv.verdict = "cycle"
