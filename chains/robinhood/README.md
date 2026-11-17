# Robinhood Chain (chain 4663, Arbitrum Orbit / Nitro)

**Mechanics.** 100 ms blocks (10 per timestamp second), first-come-first-served sequencer: no transaction pays a
priority fee and tips buy nothing, so reactions to a trigger land in the same block or a few blocks later. Positions
therefore search the trigger across the last 50 blocks (`trigger_window_blocks`); `trigger_distance_blocks` records
how far back it was. Fees: gasUsed × effectiveGasPrice with the L1 share (`gasUsedForL1`) already inside. One type-106
system transaction per block. Quote assets: USDG (6 decimals), native ETH / proxy WETH (mint/burn on wrap); stock
tokens (NVDA, SPY, …) are unpriced unless a leg of the same transaction prices them. Blink kickbacks are a cost.
Uniswap V4 is 62 % of volume: many cycles never leave the PoolManager (flash accounting), which is why the
singleton-route rule exists (route from the Swap events' distinct pool ids, amount from the receipt-backed movements).

**Measured (2026-10-08, blocks 83550000-83550299, `./arb -c robinhood facts`).** Interval 0.104 s; 5 txs/block
median (31 max); base fee 0.0228 gwei; **0 % of user txs pay a priority fee, 100 % fee-monotone** (FCFS
confirmed); 11 % reverts; 10 cycles in 300 blocks (3.3 % of blocks; the September pilot saw 24 %), 7 backruns with
trigger distances {same block 2, 1 block 1, 2 blocks 2, 3 blocks 1, 27 blocks 1}; kinds V3 9, V4 6; kickbacks 0.00007 ETH.

**Registry.** Uniswap V4 PoolManager, Uniswap V2, PancakeSwap V3, Ekubo, Algebra fork 46 verified; Pons hook fee
events (`hooks.toml`) netted at the event layer.

**Operator families seen.** The vault `0x33eabd63…` that receives the results of the `0xdc7e8f90…` operator (through
two forwarders) is also the creator of the `0xa16fe6a6…` executor the research repo tracked in September (28 wins,
0.186 ETH native in the 23:58 UTC window of 2026-09-23, blocks 70066720-70067139); the research repo's hand-traced
numbers for that window are the benchmark in `VALIDATION.md` §8.

**Known gaps.** Protocol `0x584ac601…`/`0x9735a405…` (topics `0xcad09799…`, `0x97348197…`, also seen on BSC and
Arbitrum, unidentified), `0x5399d94d…` (`0xcdad0230…`), the V3-topic factories not yet named (`v3_style_unknown`),
stock-token transfer events (`0x37e7f0db…`, not venues).
