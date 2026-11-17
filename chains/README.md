# Chains

One directory per chain. Everything that differs between chains is declared here, nothing is hard-coded in the
classifier or the application:

| File | Purpose |
|---|---|
| `chain.toml` | chain id, fee family, block time, ordering policy (incl. the cross-block trigger window), system transaction types, explorer, RPC env variable names, priced tokens, known payment recipients (builders, kickbacks, intent settlements), declared RFQ quote venues (`[quote_venues]`) |
| `dex.toml` | DEX registry: factories / pool managers / vaults, swap event signatures, probe calls. `./arb -c <chain> verify-registry` checks every address on chain and writes `dex.verified.json` |
| `handlabels.json` | *local only, not in the repository:* transactions a person verified on the explorer; `./arb -c <chain> handlabels` rescans their blocks and compares |
| `validation/` | *local only, not in the repository:* hand-label sheets, measured chain facts (`facts_*.json`), recall notes and explorer reconciliation sheets produced by the commands; `validation/explorer/` holds the verbatim explorer copies (Blockscout pulls, Arbiscan / BSCTrace page texts) that `./arb -c <chain> explorer-check` compares with the rows |
| `README.md` | what is known about the chain's mechanics, what was measured, and the known gaps |

## Fee families (`chain.family`)

| Family | Chains | Network fee | Extra |
|---|---|---|---|
| `op-stack` | base, optimism | gasUsed × effectiveGasPrice + receipt `l1Fee` + operator fee | priority fee is the sequencer's ordering signal |
| `arbitrum` | arbitrum, robinhood | gasUsed × effectiveGasPrice (L1 share `gasUsedForL1` already inside) | `timeboosted` receipt flag on Arbitrum One |
| `ethereum` | ethereum | gasUsed × effectiveGasPrice, base fee burned | builder paid by coinbase transfer inside the tx |
| `bsc` | bsc | gasUsed × effectiveGasPrice, base fee 0 | builders paid by native transfer to known builder EOAs |

## Ordering policies (`ordering.policy`) and what "position" means

| Policy | Chains | top_of_block | backrun |
|---|---|---|---|
| `fee_sorted` | base, optimism | first user tx paying the block's max priority fee | same block: last other tx that moved one of the cycle's pools the opposite way; else the last other-party tx on one of its pools up to `trigger_window_blocks` (3) earlier (`trigger_distance_blocks` ≥ 1: next-block backrun) |
| `builder` | ethereum, bsc | first user tx, whatever it paid | same block (bundles) |
| `fcfs` | robinhood, arbitrum | first user tx | same block, else the last other-party tx on one of the cycle's pools within `trigger_window_blocks` earlier blocks (`trigger_distance_blocks`) |

Positions are inferences from block order and pool price directions. Nothing here measures mempool or feed timing.

## Chain-feature matrix (what is measured or tagged per chain)

| Feature | base | optimism | ethereum | bsc | arbitrum | robinhood |
|---|---|---|---|---|---|---|
| exact fee split (execution / L1 / priority / operator) | ✓ | ✓ | ✓ (base burned) | ✓ (base 0) | ✓ (L1 share inside) | ✓ |
| block producer address | sequencer vault | sequencer vault | builder fee recipient | validator | sequencer | sequencer |
| builder name from extraData (`block_builder_tag`) | – | – | ✓ (Titan, beaverbuild, rsync, BuilderNet…) | – (binary) | – | – |
| builder payment inside the tx (`builder_tip_wei`) | coinbase (≈0) | coinbase (≈0) | coinbase transfer | known builder EOAs (48 Club, bloXroute) | – | – |
| order-flow kickback (`kickback_wei`) | Blink | Blink | – | Blink | Blink | Blink |
| top_of_block rule | max fee first tx | max fee first tx | first user tx | first user tx | first user tx | first user tx |
| flashblock / sub-block estimate (`slot_index_est`, `top_of_flashblock`) | ✓ (10 per block) | – (count varies) | – | – | – | – |
| cross-block trigger search (`trigger_distance_blocks`) | 3 blocks | 3 blocks | – | – | 20 blocks | 50 blocks |
| declared RFQ quote venues (`[quote_venues]`, maker legs counted) | 2 | – | 4 | 3 | – | 10 |
| intent fills excluded by event (UniswapX `Fill`, CoW) / registered settlements | ✓ | ✓ | ✓ (+ 1inch Fusion) | ✓ | ✓ | ✓ |
| liquidations, launch snipes, JIT liquidity (receipts) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| CEX-DEX candidates (needs a directly priced pair) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ (USDG/WETH) |
| block-producer economics (`builders`) | sequencer fees | sequencer fees | priority + tips − proposer payment | gas + builder EOA payments | sequencer fees | sequencer fees |
| express lane flag (`timeboosted`) | – | – | – | – | ✓ | – (not enabled) |
| sandwiches (bundle result from receipts + traces) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| refund to trigger sender (`refund_to_trigger_wei`) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| system transactions excluded | type 126 | type 126 | – | – | 100, 106 | 100, 106 |

Not observable from chain data and therefore not tagged: private-mempool submission, separate builder tip
transactions, off-chain CEX legs of non-atomic arbitrage.

## Adding a chain

1. `mkdir chains/<name>` and write `chain.toml` (copy the closest family) and `dex.toml`.
2. Add `<PREFIX>_HTTP_URL` / `_ARCHIVE_URL` / `_TRACE_URL` to `.env` (names are declared in `chain.toml`).
3. `./arb -c <name> verify-registry`, then `./arb -c <name> scan <recent range>` and `./arb -c <name> facts`.
4. Collect hand-verified transactions into `handlabels.json` and run `./arb -c <name> handlabels` until they all pass.
5. Run the same range with `--traces all` into a separate output dir and compare with `./arb -c <name> coverage REPORT --against BROAD`.
