# BNB Smart Chain (chain 56)

**Mechanics.** Validators build blocks from builder bundles (48 Club, bloXroute, BlockRazor). `baseFeePerGas` is 0,
so the whole gas price is the validator's; searchers additionally pay the builder by native BNB transfer inside the
transaction to the builder's EOA, not to `block.miner`. Those recipients are declared in `chain.toml`
`[known_recipients]` (kind `builder`) and counted as tips; they were found empirically (paid by 4 and 3 distinct
operators in a 60-block window). Positions follow the `builder` policy (first user tx = top of block, same-block
backruns). Native coin BNB; WBNB plays the wrapped-native role.

**Measured (2026-10-08, blocks 126499000-126499059, `./arb -c bsc facts`).** Interval mean 0.44 s (3 blocks per
timestamp second; `block_time_ms` set to 450 from this measurement); 64 txs/block median; base fee 0; 92.5 % pay
a priority fee; 83 % fee-monotone pairs; 1.4 % reverts; 32 cycles in 60 blocks (35 % of blocks), 18 backruns / 14
mid-block, 3 embedded; builder payments inside 102 txs = 0.0075 BNB, 23 of 32 cycles paying; gross $8.56 → net
$6.39 after fees and builder payments; venue kinds V3 22, V4-style singletons 18, V2 13; no sandwich in the window.

**Known gaps.** `0x76096218…` (22 logs, topics `0xee503bee…`/`0xa2c38e2d…`), `0xe2ce6ab8…` (16 logs, four topics,
same protocol as Robinhood's `0x26605f32…`), `0x9400f8ad…`, `0x57fd7147…`, `0xfd5840cd…` — all unidentified venue
families used inside traced cycles; PancakeSwap Infinity bin pools and THENA factories not yet registered. BlockRazor's
payment address is not yet known: payments to it appear as forwarded proceeds, not tips.
