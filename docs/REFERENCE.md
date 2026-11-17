# Reference: commands, explorer proofs, verification, row format, methods and limits

The front page is the [README](../README.md); the rules are in [METHOD.md](METHOD.md); the code map is
[ARCHITECTURE.md](ARCHITECTURE.md).

A terminal tool and local dashboard that downloads blocks, classifies every transaction, labels the arbitrage it
finds (atomic cycle, backrun / top-of-block / mid-block / cross-block trigger, embedded operator cycle, losing cycle,
sandwich front / victim / back), prices the result in USD, subtracts network fees, builder payments and order-flow
kickbacks, and accounts the result per UTC day. One directory per chain under `chains/` holds the chain's settings,
DEX registry, hand-verified transactions and validation evidence: **Base, Ethereum, Robinhood Chain, BSC, Arbitrum
One, OP Mainnet.**

The classifier core (`mevlab/`) is the validated Base classifier (`0.4.2-base-port.6` lineage) with the Robinhood
fixes; chain differences are declared, not hard-coded (`chains/README.md`).

## Run

```sh
# .env: <CHAIN>_HTTP_URL, _ARCHIVE_URL, _TRACE_URL per chain (names in chains/<chain>/chain.toml [endpoints])
uv sync --locked
./arb chains                    # what is configured
./arb -c ethereum scan 26149000-26149010         # inclusive range; -c picks the chain (default base or $ARB_CHAIN)
./arb -c robinhood scan 83550000 83550299 --traces candidates
./arb -c bsc tx 0x…                              # one transaction with its whole block
./arb -c base show --tx 0x…                      # saved report, no RPC
./arb -c arbitrum day 2026-10-07 --sample 300    # 300 evenly spaced blocks of a UTC day, extrapolated per day
./arb -c ethereum day 2026-10-07 --full          # every block of the day in resumable chunks (exact)
./arb -c ethereum daily                          # per-day table from every saved report of the chain
./arb ui                                          # http://127.0.0.1:8787/ dashboard: overview, transactions, blocks (timeline), families, builders, daily, validation
./arb ui --stop                                   # stop the dashboard (it holds every report in memory, ~1-2 GB); ./arb ui --status shows if one runs
                                                  # chain tabs in the header switch the chain; the URL carries it (/#/ethereum, /explorer#/ethereum), the last pick
                                                  # is remembered in the browser, and a fresh browser opens on $ARB_CHAIN (else base) like the CLI
                                                  # http://127.0.0.1:8787/explorer  Etherscan-style explorer per chain: type a block, a range (≤ 60 blocks),
                                                  # a tx hash or a UTC time; unscanned blocks are downloaded and classified on request (never continuously);
                                                  # every block and transaction page carries its proof (transfers, internal calls, route, verdict, file hashes)
```

Trace modes: `candidates` (default: cycles, multileg, unknown, receipt route hints), `swaps`, `all` (every
successful user transaction; the recall reference), `none`. Reports go to `reports/<chain>/` as JSON + CSV, raw
blocks and traces are cached under `data/<chain>/`, Binance 1-minute prices under `data/prices/`.

## Explorer and proofs

`/explorer` works like a block explorer, one chain at a time, and scans nothing on its own. The search box takes a
block number, a range (`100-120`, at most 60 blocks per request), a transaction hash, or a UTC time
(`2026-10-07 12:34`, resolved to the first block sealed at or after it). Blocks that were never scanned are
downloaded, traced and classified on request in a worker thread; the page shows the scan's progress, then the block.
A block page lists every transaction with its label, position and result; **Download proof** returns a JSON with the
block header, the sha256 of the cached block and trace files, and every labelled row's token transfers, internal
transactions, route legs, operator group, verdict and reasons, plus the commands to re-verify (`./arb -c <chain> scan N`,
`audit`). A transaction page shows the same evidence in tabs (token transfers, internal transactions, route legs,
explorer copy status) next to the chain explorer's link, so a reader can compare them line by line. Reports written by
the explorer land in `reports/<chain>/` like any CLI scan and feed the daily figures.

## Verify

```sh
./arb -c <chain> verify-registry      # every registry address has code and answers its probe → chains/<chain>/dex.verified.json
./arb -c <chain> handlabels           # rescan the hand-verified transactions of chains/<chain>/handlabels.json and compare
./arb -c <chain> explorer-check       # reconcile saved explorer copies (chains/<chain>/validation/explorer/) with the classified rows
./arb -c <chain> explorer-pull --sheet SHEET.json | HASH...   # pull explorer copies (Blockscout API v2 direct or via a headless tab, BSCTrace/NodeReal); --refetch-partial, --upgrade-text, --skip-existing
./arb -c <chain> blind N --seed S     # blind sample: N seeded random blocks of the last day, scanned, review sheet in chains/<chain>/validation/
./arb -c <chain> review REPORT        # per-row review sheet: route, venues, funding, payouts, builder tip, flags, explorer link
./arb -c <chain> reprice REPORT       # re-value a saved report's USD figures with fresh Binance prices
./arb -c <chain> audit [REPORT]       # independent recomputation of every fee and cycle amount from raw receipts/traces
./arb -c <chain> recall [REPORT]      # decoder-free loop screen over every successful tx of the cached blocks: flagged rows the classifier did not count
./arb -c <chain> brackets [REPORT] [--all]   # direction-agnostic bracket screen (sandwich misses hunt): one wallet trading a venue twice around another party
./arb -c <chain> handreview N --seed S --kind cycles|losing|sandwiches|families|review|all [--random-only]   # fresh rows + evidence sheet;
                                                  # --random-only draws from blind-sample blocks only (100 confirmed cycles per chain must come from those)
./arb -c <chain> handreview --lock SHEET.json   # verdicts -> handlabels.json;  --progress: hand-labelled counts vs the per-class targets
./arb -c <chain> builders [REPORT]    # block-producer economics: priority fees, in-tx tips, proposer payments, what searchers paid, per builder
./arb -c <chain> facts [REPORT]       # measured block cadence, fee behaviour, ordering, tips, positions, flashblock estimate
./arb -c <chain> coverage REPORT --against BROAD   # decoder gaps among trace venues; recall vs a --traces all report
uv run pytest -q                      # offline tests (fixtures in tests/fixtures)
```

Chain mechanics, measured block facts, registry notes and known gaps are in `chains/<chain>/README.md`. Hand labels
(`chains/<chain>/handlabels.json`) and validation evidence (`chains/<chain>/validation/`) are not part of the
repository: the commands above create and read them locally.

## What a row carries

- `assessment`: `trace_supported` (closed positive cycle in the call trace, receipts decisive), `trace_supported_loss`,
  `event_candidate`, `needs_review`, `not_arb`. `mev_type` adds the position or bundle role.
- Exact integers: gross in the profit token, fee split per chain family, `builder_tip_wei` (coinbase and known
  builder EOAs), `kickback_wei` (Blink and other known recipients), forwarded proceeds per recipient.
- Valuation: `gross_usd` / `net_usd` with `price_source` (`native`, `stable`, `binance:SYM`, `leg_implied:…`,
  `unpriced`). `net_usd = gross − network fee − builder tip − kickbacks`; embedded operators have no fee share.
- Positions: `trigger_type`, `trigger_tx_hash`, `trigger_distance_blocks` (0 = same block; FCFS chains search back),
  `trigger_price_reversal`, `slot_index_est` (Base flashblock estimate), `refund_to_trigger_wei`.
- Block context: `block_producer`, `block_builder_tag` (Ethereum builder name from extraData), priority fee paid.
- Sandwiches: `sandwich.{bundle_id, role, pool, operator, victims}` on the three legs; the back leg carries the
  operator's result over front + back: `net_raw`, `moved_raw`, `profit`/`profit_symbol` (largest priced gain), `profit_usd`
  (sum of every priced net change), `unpriced_tokens`, `net_usd`, `holder_inferred`. A bracket that is not a round trip
  (a token's net change above 25 % of its volume) is not a sandwich: `sandwich_rejected.{bundle_id, reason}` on the back row.
- `bundled_operation` (arb_class): a payment to a declared bundler fee collector (`[known_recipients]` kind `bundler_fee`);
  `debt_operation`: a lending-protocol Borrow event in the receipt; neither is ever a cycle. `[declared_venues]` in chain.toml
  names venues built from several contracts (Maker LitePSM + pocket) so the trace reads them as one node. `needs_review` with `unresolved_venues_unregistered_profit_token`: trace-only gain in an unregistered token.
- `quote_venues`: declared RFQ settlement contracts whose maker legs the cycle runs through (`rfq_quote_leg:` reason);
  `trigger_distance_blocks ≥ 1` on Base / Optimism / FCFS chains is a next-block backrun.
- Other families on non-cycle rows (`mev_type`): `liquidation` (`liquidations[]`: protocol, liquidator, borrower, debt
  and collateral with USD), `launch_snipe` (`snipe`: pool created earlier in the block), `jit_liquidity` (`jit`: add /
  remove legs and victims), `cex_dex_candidate` (`cex_dex`: edge vs the Binance 1-minute price at the top of the block,
  a candidate for the on-chain leg of LVR rebalancing, not a profit).

## Methods and limits

Event layer (Swap decoders for V2, Solidly, V3, PancakeSwap V3, Algebra, Uniswap V4, PancakeSwap Infinity, Balancer
V2/V3, Curve, DODO, Liquidity Book v2.0/2.1, Ekubo, Maverick V2, WOOFi and two unnamed Base families) finds candidate
cycles; the trace layer verifies them from call traces and settled receipt transfers, including flash loans,
mint/redemption routes, ERC-6909 claims, singleton flash-accounting routes and declared RFQ quote legs
(`chains/<chain>/chain.toml [quote_venues]`). Intent fills (UniswapX, CoW, registered 1inch Fusion settlement) never
count; a resting 1inch limit order taken against a pool does. Trace support is evidence of a
closed cycle, not a proof of executable profit or of operator ownership. Positions are inferred from block order.
Builder payments made in a separate transaction and unknown builder EOAs are not attributed. Tokens without a
price source stay unpriced and are excluded from USD totals, never silently zero. Daily figures are exact for the
blocks covered; extrapolation is shown only for uniform `day` samples.

The rules and how each is enforced: `docs/METHOD.md`. The feature matrix per chain: `chains/README.md`.
