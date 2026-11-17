# Method: what counts as an arbitrage, and how each rule is enforced

The old approach scored a transaction as arbitrage when some address ended net positive after a loop of
transfers. That counted aggregator settlements, order fills and fee events, and the net-positive filter deleted real
cycles whose profit went to a collector. The classifier now requires three structural things. Each is listed with the
code that enforces it (`mevlab/classifiers/base/`, `arbkit/engine.py`).

## 1. A closed chain in one asset, leg to leg

| Requirement | Event layer (Swap logs) | Trace layer (callTracer + settled receipts) |
|---|---|---|
| route returns to its entry asset through ≥ 2 distinct venues | `structure.analyze_path`: `is_cycle`, `single_venue_loop` → `round_trip` | `tracelegs.returning_route` on the profit token; no route → verdict `gain_no_route`, never a cycle |
| amount continuity | `analyze_path.worst_gap`, `amount_discontinuity` reason | `tracelegs.continuity_gap` along the route; `amount_discontinuity:<gap>` note when a leg used more than the previous one delivered |
| sender continuity | legs are the transaction's own Swap events | legs are built from the operator group's movements in trace order (`legs_of`); the group is the callee, its wallet, helpers that pay it, and collectors it pays (`party_group`) |
| amounts | Swap event amounts, hook cuts netted (`_apply_hook_cuts`, `_apply_hook_claims`) | settled receipt Transfer logs (transfer taxes, mint/burn included) + successful native value calls; calldata only without receipts |

Singleton flash-accounting routes (Uniswap V4, PancakeSwap Infinity) are the one exception where the trace cannot see
distinct pools: the route comes from the Swap events' distinct pool ids, the amount from the receipt-backed closure
(`engine.apply_trace`, reason `singleton_multi_pool_route_receipt_closed`); the PoolManager is handed to the trace
verifier as a venue hint so it is never absorbed into the operator group. Flash accounting settles only the net of
the route (`unlock`: pay 0.0025 ETH in, take 0.3745 out as one 0.372 payment), so no explicit input is required;
instead the receipt-backed gain may not exceed the gain the Swap events establish (`singleton_result_consistent`),
otherwise someone else funded part of the output and the row stays under review.

Four singleton rules were added from the random-block review rows of 2026-10-08 (`tests/test_singleton_rules.py`):

- **the callee can be the venue**: an EIP-7702 account calls `PoolManager.unlock` directly (`tx.to` = the manager),
  so the callee is excluded from the operator group only when the event layer did not establish it as a venue
  (`exclude - hints`); the sender alone is the operator.
- **net-flow legs**: takes precede settles under flash accounting, so movement order cannot pair a manager's legs;
  a hinted singleton is represented by its net flows, one leg per (asset netted in, asset netted out)
  (`singleton_legs`). A route whose venues are all singletons (Uniswap V4 plus Ekubo) defers its shape to the Swap
  events (`singleton_only_route` → the singleton rule above).
- **results paid out by the manager**: when the group itself closes nothing, every terminal receiver a venue paid —
  directly or through pass-through forwarders (`pass_through_chain`) — whose receipt closes or raises the group's
  gain in one asset is folded in (`redirects`, `output_redirected_to:<addr>`): the builder's coinbase, the operator's
  vault, a kickback recipient. A receiver that emitted a log is never a collector (`active_contracts`): a lending
  pool taking a deposit, a vault with events, a hook — folding Morpho Blue in had closed a 1,488 USDC phantom
  "cycle" out of a wrap-and-deposit flow on Base (`0x052d4542…`). The sum must stay within the Swap events' gain
  when several are folded in (`redirected_collectors_exceed_event_gain` otherwise).
- **hook fee takers are never collectors**: the pools' registered hooks (`hook_addresses` from the Initialize
  cache), the recipients a hook's fee event accounts for to the wei (`hook_fee_recipients`, subset of the manager's
  payees), and ERC-6909 claims minted by a caller outside the party (`foreign_claim_recipients`; the mint's
  `caller` is in the log data). Claims minted by the party — the callee, the sender, or the contract holding the
  manager's lock (the Swap event's `sender`, `swap_lockers`) — are the operator's result taken to a collector.

An RFQ or order-book fill moves the maker's asset by allowance: the maker is never called and the called settlement
contract holds no tokens, so a route with such a leg has no called venue there. Structurally this is the same as a
solver filling a user's order (CoW, Permit2 pulls), which the method excludes, so the maker is not promoted to a
venue: the receipt-closed gain stays visible as `needs_review` (`closed_gain_without_called_route`, Robinhood
`0x4d90a858…` sells SPY to a market maker on its third leg), never silently `not_arb`. Trying the promotion turned
a CoW settlement (callee `0x9008d19f…`) and a Permit2-mediated user swap into "cycles" (random blocks 25904265 /
26150001), which is why it was withdrawn.

Three guards came out of the first day-scale run on Ethereum (2026-10-07 sample), each a phantom worth more than
every real cycle of the sample together:

- **an unfunded operator earns only the route's surplus**: when the group paid nothing into any venue, its closed
  gain must equal what the last venue delivered minus what the first leg took (within 1 %). A "venue" handing the
  group more is paying from inventory — an operator-owned position contract withdrawing 721.97 WETH of Aave
  collateral (`0x3d4efb0c…`, `0xa768a4c9…`), a keeper-run vault distributing 107.9 NVDA against a 0.00095 NVDA
  round trip (Robinhood `0x1e7e2d1e…`), a wrap-and-deposit flow with a zero-surplus mint/redeem (Base
  `0x052d4542…`) — `gain_exceeds_route_surplus`, kept as `needs_review`.
- **a payout to a token contract is not proceeds**: tokens sent to their own contract (DAI returned to the DAI
  contract after a flash mint, `0x7198930a…`: 9,743 DAI) are a repayment or a burn, subtracted from gross
  (`protocol_payments`; `proceeds_consumed_by_protocol_payments` when nothing remains). Every other payee stays
  a payout: a wider rule (any contract that emitted a log or was called) was tried and withdrawn because it
  stripped the per-user fee payments of the BSC bundlers out of verified cycles.
- **settlements never count**: CoW's GPv2Settlement is registered per chain (`[known_recipients]`, kind
  `settlement`); a transaction that calls it is `settlement_like` whatever the trace closes — 17 of 737 Ethereum
  "cycles" were CoW batches (solver surplus, user hooks selling collateral to repay debt: `0x57d2f2c9…` had
  closed as 85,025 USDT). The 1inch routers are not settlements: a verified Ethereum cycle (`0x9af997d4…`)
  routes one leg through the aggregation router.

The returning route may pass through the same venue address twice only with a different token pair (a singleton
manager hosts many pools); the same pair again is a same-pool round trip and is rejected. The route is searched in
the profit token, then the entry token, then any routed token: a cycle may close in USDC and convert the surplus to
WETH afterwards (`cycle_token:<token>` note).

## 2. Every venue is an actually called contract

`venues_of(movs, exclude, called)`: a venue gives one asset and takes a different one and appears in the call tree.
Addresses that are not venues even when they move tokens:

- **pass-through helpers** (`conduits`): received an asset from a venue and forwarded it to the operator (≤ 1 % kept)
  while taking nothing else. They join the operator group. This removed the Ethereum `0x0c1e551a…` "7.45 WETH cycle"
  (a sandwich back-leg selling inventory through a helper) and the Base fee-retaining-router cut.
- **flash lenders** (`flash_lenders`): give and take back the same asset; principal is not profit.
- **operator-side payers**: a contract that pays a known recipient (builder tip, kickback, bundler fee) out of its own
  balance, directly or through the called forwarder, is the operator's vault or executor and never a venue. Found on
  BSC (2026-10-09 hand review): the vault funding a buy and tipping the 48 Club, or the executor `0x57fd7147…` paying
  bloXroute, had been read as the second venue and the tip as the gain (25 rows whose "gross" equalled the tip).
- **bundler fee collectors** (`[known_recipients]` kind `bundler_fee`): a payment to one makes the transaction a
  `bundled_operation` (never a cycle, never a losing cycle): the bundler executes users' operations for a fee, and the
  fee was the only "gain", a WBNB round trip of its mechanics the only "loss" (BSC `0x8a08d98c…`, 158 rows).
- **unregistered profit tokens through unresolved venues** (`unresolved_venues_unregistered_profit_token`): a gain the
  trace closes through venues the event layer could not resolve, in a token the chain does not register (counterfeit
  "USDT", vault shares), stays `needs_review`.
- **debt inflows** (`debt_operation`): a Borrow event of a lending protocol in the receipt (Morpho Blue, Aave V3,
  Compound V2) makes the transaction a position operation, never a cycle: the inflow is a loan against collateral the
  party supplied (Ethereum `0x2e47ddf9…`, a Morpho Blue borrow whose 0.87 USDC "profit" was the loan).
- **declared venues** (`chain.toml [declared_venues]`, with `members`): a venue the receipts cannot reveal because it
  is built from several contracts, read as one node that is never absorbed into the operator's group. The Maker LitePSM
  takes DAI into one contract and pays USDC from its pocket; without the declaration a +3.78 USDC five-venue cycle
  (Ethereum `0xdff8e55a…`) read as a 1,825 USDC loss.
- **receipt rounding dust**: with receipt-backed amounts a negative residual in a non-profit token opens a loss, except
  below one part in 10^8 of that token's flow (`EXACT_DUST_SHARE`): a DAI/USDC pool returning 6e-7 DAI less on a
  14,248 DAI leg does not turn a $3.90 cycle into an open position.
- **CEX-DEX candidates on first-come chains** (`ordering_policy = fcfs`): block position means nothing, so the edge must
  also hold against the next Binance minute, and a block where three or more wallets show the same edge is a stale
  minute, not three rebalances (`cex_dex_dropped`).
- **wrappers**: ETH ≡ WETH (`canonical_token`, `weth_mint_burn`), ERC-6909 claims stay separate from cash.
- **token issuers** on mint/redemption legs are venues (collateral in, shares out), so basket/vault routes close.
- **uncalled counterparties** (`maker_candidates`): an address that gave one asset and took another without ever being
  called — a market maker whose quote a settlement contract filled by allowance, or a user whose order a filler
  executed. They are never venues by themselves; the row is reported (`uncalled_counterparties:<addr>`,
  `closed_gain_without_called_route` → `needs_review`), not counted.

**Declared RFQ quote venues (`chain.toml [quote_venues]`, 2026-10-08).** When the settlement contract that moved a
maker's inventory is declared, the maker's movements are read as that contract's leg (`Movement.via`,
`rfq_quote_leg:<settlement>:<maker>`): the called settlement is the venue, the maker's wallet is where its inventory
sits, and a pool-versus-quote take closes as a cycle (Robinhood `0xe7705fc4…`: flash-swap on a V3 pool, maker quote
through `0x000c9f31…`, 0.0205 USDG). The declaration is per contract and rests on a structural reading of the data:
a quote venue settles for **many distinct takers against one or a few maker wallets** (Robinhood `0x55555522…`: 28
takers, one maker; the `0x1eeaa4ac…`-event pools: one maker each), whereas an intent protocol settles for **many
users against a few fillers** (UniswapX reactors: 22 users / 6 fillers; CoW's vault relayer: 107 / 11). A stablecoin
redemption module (Ethereum `0x54f5eb3e…`, one liquidity wallet, many users, no round trip) is not declared. Undeclared
settlements keep their makers uncalled.

**Intent fills never count (user rule: "order fills never count").** A UniswapX `Fill` or CoW `Trade` / `Settlement`
event in the receipt makes the transaction `settlement_like` (`intent_fill_event:<name>`) whatever the trace closes:
the filler's surplus is the price of executing someone's order (Ethereum `0x242c339b…`: three Dutch-order fills whose
502-sat surplus was sold for WETH; Arbitrum `0x3668f343…`, `0x5c523357…`, `0x765a7f8a…`, the last one a fill at a
loss — all three had been explorer-reconciled "cycles" in the label set and were relabelled). A 1inch `OrderFilled`
alone is **not** an intent fill: a resting limit order is taken like a quote (verified cycle Ethereum `0x9af997d4…`);
1inch Fusion intents go through a settlement contract registered as `kind = "settlement"` instead.

## 3. Profit at the callee or its collector; losses are attempts, not deletions

- The taker is the callee (`tx.to`), else the wallet; collectors paid straight by a venue are added to the group
  (`output_redirected_to:<addr>`); payouts to third parties are reported as **forwarded proceeds** with ownership
  unverified, never silently subtracted or added.
- `closed_gain`: exactly one asset ends positive beyond tolerance; with receipts every negative residual fails closure.
- A route that returns to its entry asset with a negative result is `loss_cycle` (`trace_supported_loss`), shown
  with its path and kept in the counts; an event-layer cycle without a positive balance is `attempted_cycle`.
- Costs are separate fields, never folded into gross: network fee (per chain family), builder payments inside the
  transaction (coinbase and known builder EOAs), order-flow kickbacks (known recipients). `net_usd` subtracts all
  three; embedded operators inside another caller's transaction get no fee share (`fee_coverage = shared_transaction_fee`).

### Rules added by the 2026-10-09 hand-review passes (`tests/test_handreview_rules.py`)

Every rule below came from a counted row read by hand against its explorer copy and is pinned by a test named after it.

- A token contract transferring its own existing balance (a plain Transfer from itself, not a mint) is a holder selling
  inventory, never a venue (`self_selling_token_contracts`): a taxed token's swapBack dumping its tax through the router
  had closed as a cycle (BSC 126573593 #37).
- With receipt-exact amounts a surplus below one part in 10^8 of the token's flow on a closed route is rounding, not a
  profit (`round_trip_dust_surplus`): a vault rebalancing inside a transfer hook left 2 wei on 1,680 USDT (BSC 126516560).
- Embedded cycles: the transaction's own parties (sender, callee and their group) are never a secondary operator's venue.
- A payout burned to `0x…dEaD` is a protocol payment, not proceeds.
- Profit token with receipts: the entry token is the profit token when it ends positive beyond tolerance; otherwise a
  token the group only ever received (its gain is its whole flow: a pass-through residue) never outranks a traded token.
  1.2e-9 ETHFI of dust had been priced at $2.33 over +0.0121 USDC (Arbitrum 512984150 #15).
- Liquidation events (Aave, Compound, Morpho, Euler, Marginly `ReceivePosition`) make the row a `debt_operation`
  (`liquidation_event:<protocol>`; Arbitrum 513093052 #2), and so do debt repayments (Aave V3 `Repay`, Morpho Blue
  `Repay`, Compound `RepayBorrow`; `debt_repayment:<protocol>`): a flash-swapped USDT0 repaid a position's debt, its
  1.77 WETH collateral was withdrawn and sold, and 0.33 USDC was left (Arbitrum 512966161 #4).
- The registry's singleton managers (`dex.toml` `pool_manager`) are venue hints and singletons whether or not a Swap
  event of theirs was decoded: a flash take/settle through the Uniswap V4 manager emits none and the manager had been
  absorbed into the operator's group (Robinhood 84263927 #1, Arbitrum 513170582 #2). A vault the manager mints the
  cycle's claim to is folded in as the redirected output and shown as a forwarded ERC-6909 claim.
- A dust-level own gain (≤ 1e-8 of flow) does not stop the search for the redirected output: 6,701 wei WETH left in the
  contract had become "the" gain while the 0.000046 USDC.e output was claim-minted to the vault (Optimism 157975774 #44).
- Smart accounts the MetaMask DelegationManager (`0xdb9b1e94…`, the same address on every chain) calls with
  `executeFromExecutor` are the parties orders are executed for: their exchange with the executor is a fill whose price
  improvement the executor keeps, never a venue's quote (`delegator_accounts:`; the row becomes `settlement_like` with
  `delegated_order_execution:`). An account that paid 1 USDC for exactly 22.9 tokens its Spender bought for 0.928 had
  closed as a 0.0716 USDC cycle (Arbitrum 513260119 #3); two fills whose improvement went to the executors' fee wallets
  had been confirmed as cycles and are relabelled (Arbitrum 512990139 #1, 512995745 #1). A bot's own account executing
  its cycle through the manager stays a cycle.
- Declared settlements (`[known_recipients]` kind `settlement`, callee-based): GMX V2 OrderVault and MultichainVault,
  GlideDepositFactory and the Keeper order-batch vault on Arbitrum; GlideDepositFactory and the deposit-address sweep
  factory `0x5d1d6fa7…` on Ethereum (pre-funded CREATE2 deposit contracts swept into CCTP with a fee); the gasless-swap
  Adapter `0x0000000000bbf5c5…` on every chain (a relayer executes users' permit orders through GenericSwapExecutor
  `0x16f19efd…` and keeps a fee). Declared quote venue: Bebop's second settlement `0xbeb0009a…` (Ethereum, BSC) —
  undeclared it was absorbed into the operator group and its 4.53 USDT fee became the "result" (Ethereum 26154677 #2).
  Declared builder EOAs on BSC: four native payees of cycle proceeds from three or more operators each.
- Bare value transfers count as calls when the callee ran code: bonding curves and launch contracts are bought by
  sending them ETH with no calldata (Ethereum 26155312 #351, BSC 126615424). A user paid their output the same way can
  look like the closing venue (Arbitrum 513092576 #2); the order executor is declared instead. (Refined below: a
  payment to a code-less address is never a call.)
- `explorer-check` ignores internal native transfers the explorer lists inside reverted frames (`reverted_natives`):
  they never executed, and the trace does not carry them.
- A route venue that minted or burned liquidity in the same transaction is being rebalanced, not traded
  (`liquidity_event_at_route_venue:` → `lp_action`): a position manager (a Gamma Hypervisor, a zapper) burns its
  Uniswap V3 range, collects, swaps and mints again, and the pool's collected tokens had read as a cycle with the
  manager's fee split as the result (Arbitrum 513003645 #3, 513247838 #1) or the operator's own withdrawn liquidity
  as a $200 "cycle" (Base 52350210 #125). Singleton managers are left out of the rule (their liquidity events belong
  to any pool of the singleton).
- More declared settlements: the Sweeper `0xf9e6d0d6…` on Arbitrum (sells a user's tokens through the Tycho router,
  fee split to its wallets; 513085162 #1) and the launchpad purchase executor `0x3cb658cc…` on BSC (buys the exact
  token amount at the PancakeSwap price and returns the change while a distributor delivers the tokens; the purchase
  price had read as a losing cycle, 126602652 #28, 126638856 #62).
- Liquidity events, two refinements from a study of hooked pools. (a) Liquidity
  events made inside a hook callback (a frame the singleton manager issued with an `IHooks` selector, under which the
  hook, its fee locker or its rebalancer calls `modifyLiquidity`) are the pool's own — a fee collect with a zero delta,
  an auto-compound or a range rebalance while serving the swap — never the operator's position (`hook_liquidity_only`
  in the trace verifier; the engine reads the trace before the event layer for receipts whose liquidity events are all
  the singleton's and passes `hook_liquidity`; reason `liquidity_changes_inside_hook_callbacks`). Real cycles had been
  held as `lp_action` by such events: Ethereum 26150007 #441, 26137938 #441, 26137281 #16; Base 52340882 #443; BSC
  83600063 #2, 83600097 #1; Robinhood 84259655 #4, 70067057 #13, 70067123 #13, 54346064 #7. A `modifyLiquidity` the
  operator reaches itself, or through a position manager it calls, still holds the row — including a zero-delta
  collect: a fee harvest sold on the spot is the holder's LP operation (Ethereum 26139831 #180, 26143936 #138), which
  is why a trace-free "zero delta is no event" rule was tried and withdrawn. Addresses reached inside a hook callback
  (the hook, its rebalancer or fee locker, the position manager they call) act for the pool: they are dropped from the
  venues and never folded in as collectors (`hook_callees`; note `hook_side_venues_dropped:`) — the rebalancer of
  Robinhood 70067057 #21 exchanging with the manager inside afterSwap had read as a second venue, which hid a
  five-pool route behind a "gain without returning route". Only addresses reached *solely* inside hook callbacks
  count, never token contracts or anything the operator also calls itself: the DAI contract moved by a PSM hook had
  been swept in, hiding the operator's own DAI burn and turning a zero-result round trip into a 9,743 DAI "cycle"
  (Ethereum 26139612 #64). Known cost: a hook that executes the swap itself and settles the output as claims
  (Ethereum 26142919 #100, +0.00012 ETH) is now machinery, not a venue, and its cycle is no longer counted. (b) A
  bare value transfer counts as a call only when the callee ran code (`gasUsed` > 0 or frames beneath): bonding curves
  bought by sending ETH still count; a payment to a code-less address (an EOA funder or collector) executes nothing and
  never makes a venue — the wallet that had funded the bot by allowance and took the change of a pre-funded builder tip
  back as plain ETH had read as the closing venue, and the 0.0203 ETH change as a loss (Ethereum 26137380 #168, a
  +0.0139 WETH +3.86 USDT cycle). Payouts beyond what the group received in the transaction are inventory moving, not
  proceeds (`inventory_payout:<token>:<wei>`; venue legs are never discounted, so a loss at a venue stays a loss).
- A hook arbitraging its own pool inside a user's swap is the pool's mechanism, never the party's cycle
  (`hook_mechanism`, reason `cycle_legs_executed_by_hook:`): when every pool of the event-layer cycle was swapped by a
  hook-side address and the party's own flows close nothing — Robinhood 84319662 #1, an EOA's 0.00001 ETH buy during
  which the hook `0x89c9fb50…` ran a 0.086 ETH round trip through its executor and kept +0.000315 ETH, had been counted
  as the sender's cycle.
- A declared RFQ settlement or order settlement is never a secondary operator (`embedded_cycles(..., blocked=)`): a
  user's 0x Settler swap sold to a Bebop maker through the Bebop settlement had closed as the Settler's "embedded cycle"
  with the maker as its venue and the Settler's 0.0000249 ETH fee as the gain (Base 52340002 #58). Bebop's settlement
  `0xbbbbbbb5…` is declared a quote venue on every chain. Declaring the 0x AllowanceHolder / Settlers as settlements
  was tried and withdrawn: bots route legs through the 0x Settler (11 real Ethereum cycles, three hand-labelled, had
  flipped).
- Fluid vault operations (`LogOperate` of a Fluid vault, `fluid_vault_operate`) make the row a `debt_operation`: a
  Safe repaying its 1.0078 WBTC debt with a flash loan, withdrawing 130,681 USDC of collateral and buying the WBTC back
  for 83,942 USDC had closed as a "130,681 USDC cycle" (Ethereum 26141146 #245). The liquidity-layer event is not used
  (Fluid DEX swaps emit it too).
- Arbitrum ordering: the Kairos express-lane payment contract `0x60e6a315…` (Timeboost era, until 2026-09-24) is a
  `builder`-kind recipient; under the priority gas auctions in force since then the bid is the priority fee, which the
  fee accounting already carries.
- Sandwich results (`arbkit/sandwich.py` `operator_result`): with the leg's trace available, WETH `Deposit` /
  `Withdrawal` events are no longer folded into the operator's WETH change — the native side is already in the trace's
  value flows (the ETH taken from or settled into a Uniswap V4 pool), so a V4 sandwich that unwrapped ahead and
  wrapped behind had reported twice its result (Ethereum 26143358 #11: 0.001327 WETH read as 0.002654) and one with
  an extra WETH leg far more (26137400 #131: 0.003426 WETH read as 0.0808). Without a trace the events still stand in
  for the native side.

## Block-level context carried on every row

- `block_producer` (fee recipient / validator / sequencer) and `block_builder_tag` (printable extraData: Ethereum
  builders sign their blocks, e.g. `Titan (titanbuilder.xyz)`, `beaverbuild.org`); `builder_tip_wei` is what the
  transaction itself paid the producer or a known builder EOA; priority fee per gas and paid priority are exact.
- `slot_index_est` / `top_of_flashblock` on fee-sorted chains with flashblocks (Base): estimated from the upward
  priority-fee jumps inside the sealed block, labelled as an estimate.
- `trigger_distance_blocks`: FCFS chains search the trigger across `trigger_window_blocks` earlier blocks (Robinhood
  50, Arbitrum 20); fee-sorted chains with a window (Base 3, Optimism 3) do the same for cycles without a same-block
  trigger — a **next-block backrun** (`positions.backrun_next_block` in the daily figures: 43 of 154 cycles in the
  Optimism window 157946000-157946039, 3 of 16 on Base 52340000-52340004). `trigger_price_reversal` records whether
  the trigger moved the pool the opposite way (True), the same way (False) or unknown (None).
- `refund_to_trigger_wei`: a backrun's forwarded proceeds paid to the trigger transaction's sender or callee
  (MEV-Share-style refunds).

## Other MEV families (labels on rows the cycle classifier left alone; `arbkit/families.py`)

None of these changes an arbitrage verdict; each carries the evidence it was built from and `mev_type` names it.

- **liquidation**: a lending protocol's liquidation event in the receipt (Aave V3 `LiquidationCall`, Compound V2 /
  Venus `LiquidateBorrow`, Compound V3 `AbsorbCollateral`, Morpho Blue `Liquidate`, Euler V2 `Liquidate`) with the
  liquidator, borrower, debt repaid and collateral seized as the event states them, priced where the assets are
  priced (`bonus_usd` = collateral − debt).
- **launch_snipe**: a swap on a pool created earlier in the same block (factory `PairCreated` / `PoolCreated`,
  Uniswap V4 `Initialize`) by a sender other than the creator. The position is held, so only the amounts spent and
  received are recorded.
- **jit_liquidity**: the same sender adds liquidity to a pool and removes it later in the same block with another
  party's swap on that pool in between (Mint … Swap … Burn, or V4 `ModifyLiquidity` ±). Both legs carry the bundle id
  and the victims; the fee income is not computed.
- **cex_dex_candidate**: the on-chain leg of a CEX-DEX rebalancing (the arbitrageur's side of LVR). A single-route
  swap by {sender, callee} at the top of the block (first three user transactions, or first in its estimated
  flashblock on Base) whose execution price beats the Binance 1-minute price by ≥ 15 bps on ≥ $1,000 notional, with
  both tokens priced directly (native, stable or Binance symbol — leg-implied prices would be circular). Mid-block
  swaps are never candidates: when the CEX price moves inside the minute, every swap of the block shows the same
  "edge" (Ethereum 26137380: twelve retail swaps through one router at 40-55 bps). `edge_usd` is the edge versus the
  CEX price, never a profit: the off-chain leg is unobservable.
- **block-producer economics** (`./arb -c <chain> builders`, `arbkit/builders.py`): per block the priority fees, the
  in-transaction builder payments the trace pass saw (coverage reported), direct transfers to known builder EOAs,
  the fee recipient's own payment to the proposer (Ethereum), and the searchers' side (gross, net, what they paid the
  producer). Margin = priority + tips − proposer payment: a lower bound on income with an exact proposer cost, the
  same quantity relay dashboards show as "builder profit", but with its sources named.

## What is excluded by construction

- intent fills / settlements (UniswapX, 1inch, CoW): `settlement_like`
- same-transaction liquidity events: `lp_action` (collections are not profit)
- single swaps, split routes, probes, reverts, system transactions, failed ERC-4337 operations
- sandwiches are their own bundle type: front and back are sent by the same wallet (two wallets driving one contract
  are two users of a router; 9 of the 12 brackets rejected on 2026-10-09 were public routers, and none of the 41
  confirmed sandwiches had different wallets); the operator's result is measured over front + back from receipts (+ native
  value from traces), direction confirmed from the pool's Swap logs. The legs must be swaps (a liquidity action is
  JIT, a counted cycle stays a cycle and is not counted twice) and the bundle must be a round trip: no token's net
  change over front + back may exceed half of that token's volume through the operator (`round_trip_verdict`,
  `sandwich_rejected` on the back row with the token and share). Top-of-block multi-asset trades and buy-and-hold
  pairs that bracket a victim by coincidence are rejected by this rule. When the called contract only forwards, the
  holding contract is inferred from the legs' transfers (the one non-venue address that receives in the front and
  sends in the back, or the reverse; `holder_inferred`), so the result is measured where the inventory lives
  The bundle's USD value sums every priced token the operator ended up with (gains and losses); the profit token
  shown is the largest priced gain, and unpriced residues (dust of the victim's token, memecoins) are listed
  with the bundle, never silently chosen as "the" profit (`unpriced_tokens`)

## Blind samples and the review sheet

A blind sample (`./arb -c <chain> blind N --seed S`) draws N blocks at random from the last day with a stated seed,
scans them, and writes a review sheet (`chains/<chain>/validation/blind_<seed>_<date>.md`): one entry per positive,
losing or review row with the route leg by leg (amounts, tokens, venue and DEX), who funded it, the group, the
collectors folded in, every payout, the builder payment, the structural flags the rules raised, and the explorer link.
The question per row is the method's: a closed chain in one asset through called venues, funded by the operator or
a singleton take, result at the operator or its collector. Nothing in the sheet is a verdict a person has not seen;
the explorer copies (`explorer-check`) prove the arithmetic, the sheet is for the semantics.

## How recall is checked

`--traces all` traces every successful user transaction regardless of events; `./arb -c <chain> coverage REPORT
--against BROAD` lists positives the default schedule missed and the undecoded event families among trace venues.
`./arb -c <chain> recall REPORT` (`arbkit/recall.py`) is a decoder-free loop screen over every successful transaction
of the cached blocks: from receipt Transfer logs alone (plus native calls when a trace is cached) it flags any
transaction where a token left and came back to {sender, callee, their terminal receivers} with a positive net through
at least one counterparty that exchanged one asset for another, and lists the flagged rows the classifier did not
count with the classifier's reasons. It is blind to singleton flash accounting and to operator groups beyond
{from, to} (it saw 24 of 46 Robinhood blind positives), so its value is the suspect list, which a person reads.
`./arb -c <chain> brackets [REPORT | --all]` (`arbkit/brackets.py`) is the sandwich counterpart: a direction-agnostic
bracket screen. A venue is any contract that emitted a decoded swap event or exchanged one token for another while
emitting any event; a bracket is one wallet trading the same venue twice with another party's trade on it in between
(up to 40 positions, any direction). Each bracket is classified against the report (counted sandwich, JIT pair,
counted cycle as a leg, position change, round trip without gain, no measurable party flows, uncounted round trip
with a gain); the last two are suspects to read. Blind to operators whose inventory the legs' transfers do not
reveal. Its first run (2026-10-09, every scanned block of six chains) found one miss, a DODO USDT/USDC sandwich whose
swap event the direction check did not know, now covered by `_transfer_direction`.
Hand-verified transactions per chain (`chains/<chain>/handlabels.json`) are re-run after every classifier change
(`./arb -c <chain> handlabels`); a change that flips a verified row is a regression.
Explorer copies of random-block cycles (`chains/<chain>/validation/explorer/`) are pulled by
`./arb -c <chain> explorer-pull --sheet SHEET.json | HASH… [--refetch-partial] [--upgrade-text] [--skip-existing]`
(`arbkit/explorer_pull.py`): Blockscout API v2 (`/api/v2/transactions/{hash}`, its `token-transfers` and
`internal-transactions`, every page) saved as a compact JSON copy — directly where the API answers plain requests
(eth.blockscout.com, optimism.blockscout.com) or through a same-origin headless Chrome tab (`arbkit/cdp_fetch.mjs`)
where Blockscout sits behind a Cloudflare JavaScript challenge (base, arbitrum, robinhoodchain; the tab waits for the
challenge to settle by itself and never clicks it) — and BSCTrace's NodeReal API (`tx/getDetail`, `tx/getInternalTxList`)
for BSC, which gives raw exact values. Page-text copies that a compact pull supersedes move to `explorer/superseded/`.
Copies are pulled after a sheet is drawn, never before: `handreview` excludes hashes that already have a copy. The
copies are reconciled with the rows by `./arb -c <chain> explorer-check`: status, block, counterparties, fee, every
token transfer, the internal native transfers, and the operator's result rebuilt from the explorer's own movements
with the audit's cash rules. Rows that the explorer confirms feed the chain's `handlabels.json`.
