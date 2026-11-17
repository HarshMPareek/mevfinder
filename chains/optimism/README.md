# OP Mainnet (chain 10, OP Stack)

**Mechanics.** As Base: 2 s blocks, priority-fee ordering inside ~200 ms sub-blocks whose count per block varies,
`l1Fee` in the receipt, type-126 system transactions. Positions use the `fee_sorted` policy; flashblock estimates
are unvalidated on this chain.

**Measured (2026-10-08, blocks 157944300-157944329, `./arb -c optimism facts`).** Interval 2.0 s; 41 txs/block
median; base fee 5e-7 gwei; 99.7 % pay a priority fee; 80.6 % fee-monotone pairs (lower than Base's 96.8 %,
consistent with a variable number of sub-blocks); 11 % reverts; 182 cycles in 30 blocks (83 % of blocks, 6 per
block) but gross only $10.27 in total — dust-sized pool-vs-pool rebalancing (143 mid-block, 39 backruns); kinds
Velodrome/Uni V3-style 168, Solidly 63, V2 58, V4 53, Curve 11, Balancer 8.

**Known gaps.** A pool family with many emitters (`0x73d5c2f4…`, `0x601827ae…`, `0x1239c54d…`; topics
`0x5548c837…`/`0x1f89f963…`, 44 logs) is undecoded although its pools appear inside traced cycles;
`0xc35dadb6…` (`0xb2346165…`/`0xad9ab9ee…`), `0xda7030b5…` (`0x179eaad5…`).
