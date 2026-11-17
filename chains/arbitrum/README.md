# Arbitrum One (chain 42161, Nitro)

**Mechanics.** 250 ms blocks. Since 2026-09-24 the sequencer runs priority gas auctions (PGA): transactions ranked by
`maxPriorityFeePerGas` in 125 ms rounds, ties by arrival, two rounds per block. Positions use the `fee_sorted` policy with
`flashblocks_per_block = 2` (top_of_block = first user tx at the block's max fee; `top_of_flashblock` = the fee leader of
the second round, estimated from fee jumps as `slot_index_est`) and keep the 20-block (5 s) trigger window for
cross-block backruns. Before 2026-09-24 the chain was first-come first-served plus the Timeboost express lane (receipt
flag `timeboosted`); set `policy = "fcfs"` to read those blocks. Fees: gasUsed × effectiveGasPrice with the L1 share
inside; types 100/106 are system transactions (one internal tx per block).

**Measured (2026-10-08, blocks 512976000-512976199, `./arb -c arbitrum facts`).** Interval mean 0.276 s (up to 7
blocks per timestamp second); 3 txs/block median; base fee 0.02 gwei; 55 % pay a priority fee; 83.5 %
fee-monotone; 4.8 % reverts; 0 timeboosted; 7 cycles + 1 losing cycle in 200 blocks (3.5 % of blocks), triggers
{same block 1, 1 block 3, 5 blocks 1}; kinds V3 4, V2 3, Algebra (Camelot) 1, DODO 1; gross $0.71, net $0.15.

**Registry.** Uniswap V2/V3/V4, PancakeSwap V3, SushiSwap V3, Balancer V2, Ekubo verified; Maverick V2 and WOOFi
decoded by event; Camelot V3 (Algebra) decodes through the Algebra signature once its factory is added.

**Known gaps.** `0x4c9c6842…` (topics `0xcad09799…`/`0x97348197…`, the cross-chain protocol also seen on Robinhood
and BSC), Fluid, Curve pools without a registry entry (decoded by event anyway).
