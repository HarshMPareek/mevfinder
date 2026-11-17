# Base (chain 8453, OP Stack)

**Mechanics.** 2 s blocks built by the sequencer from ~200 ms flashblocks; inside a flashblock transactions are sorted by
priority fee, so `top_of_block` = the first user transaction at the block's maximum fee and a backrun is the cycle that
follows the last other transaction on one of its pools in the same block. Fees: `gasUsed × effectiveGasPrice + l1Fee`
(+ operator fee when the receipt announces one). Type-126 system transactions (L1 attributes, deposits) are excluded
from bot counts. Order-flow kickbacks to Blink (`0x743be0db…`) are a cost of a private-flow backrun.

**Measured (2026-10-08, `./arb -c base facts` on blocks 50872944 and 51281235-51281237).**
Block interval 2.0 s; 424 txs/block median; base fee 0.005 gwei; 91 % of user txs pay a priority fee; 96.8 % of
adjacent pairs are fee-monotone (fee-sorted ordering confirmed); 25 % of user txs revert (bot spam); 30 cycles per
block in that busy window, 63 backruns / 58 mid-block; venue kinds in cycles: Uniswap/Aerodrome/Pancake V3-style
106, Uniswap V4 30, market-maker pool family 22, Algebra 18, V2 12, Solidly 11, DODO/Balancer/Curve.

**Registry.** 17 entries, every addressed one has code and answers its probe (`dex.verified.json`). Decoders cover
V2, Solidly, V3, PancakeSwap V3, Algebra, Uniswap V4, Balancer V2/V3, DODO, the two unnamed Base families
(`mm_pool_fcfa`, `clmm_pair_cd8b`), Liquidity Book v2.0 and v2.1, Curve, Ekubo (log0), Maverick V2.

**Known gaps (venues the trace layer used whose events have no decoder; trace recovery still counts them).**
- `0x501568a7…` market-maker pool, topics `0xdc92a0ea…` / `0x87d25816…` (15 logs in the window; a USDC/WETH counterparty in the hand checks).
- `0x69a9f156…` (topic `0x51ab1232…`), `0x9d38282b…` (topics `0x5942a9a3…`, `0xadc301c7…`), Maverick V1 (`0x3b841dc9…`, 2 logs).
- Hooked V4 pools that take a native-ETH cut without an event: the trace pass decides (`hooked_v4_native_unverified`).
