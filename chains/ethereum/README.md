# Ethereum mainnet (chain 1)

**Mechanics.** 12 s slots; builders order the block, so the priority fee is not the ordering signal: `top_of_block`
= the first user transaction whatever it paid, a backrun = same-block adjacency on a shared pool (bundles). Searchers
pay the builder by native transfer to `block.miner` inside the transaction (`builder_tip_wei`, measured from the call
trace) or by a separate tip transaction (not attributed: known gap). Base fee burned; network fee = gasUsed ×
effectiveGasPrice. Sandwiches (front / victim / back by the same wallet or the same ≤5-wallet contract on one pool,
direction-confirmed from the pool's Swap logs) are reported with the operator's bundle result.

**Measured (2026-10-08, 12 blocks around 25904265-25904859, `./arb -c ethereum facts`).** 266 txs/block median;
base fee 0.72 gwei; 96 % pay a priority fee; 75 % fee-monotone pairs (builder ordering, not fee-sorted); 1.3 %
reverts; coinbase payments inside 46 txs = 0.173 ETH; 42 cycles (21 backrun, 17 mid-block, 3 top-of-block, 1
embedded), 5 sandwich bundles; builders from extraData: Titan 32 cycles, BuilderNet 5, Builder+ 3, Eureka 1; venue
kinds: V3 30, V4 23, V2 14, Balancer 6, Ekubo 3, DODO 3, Curve 2.

**Registry.** Uniswap V2/V3/V4, SushiSwap V2, PancakeSwap V3, Balancer V2, Ekubo verified; Curve decoded by
event (StableSwap, CryptoSwap, NG), Balancer V3 and Maverick V2 decoded by event; Fluid still undecoded.

**Known gaps.** `0xc186fa91…` (19 logs, topics `0x9e6c5294…`/`0xf652dd63…`), multi-emitter topic `0x458f5fa4…`
(10 logs), `0x52aa8994…` (`0x4d93b232…`), Fluid DEX; CoW settlement interactions are routing, not venues.
