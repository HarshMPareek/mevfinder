# Open problems

Parts of the classifier that are in this repository but not trusted enough to show on mevfinder.com yet, and one that
does not exist. Each is a good place to help: read the code, bring transactions (explorer links) that show the
problem, and add a test built from their receipts and traces (`tests/fixtures/`).

## 1. CEX-DEX arbitrage: candidates only

**Code:** `arbkit/value/pricing.py` (`CEX_DEX_*` thresholds and the candidate check), `arbkit/scan/families.py`.
**Method:** `docs/METHOD.md`, `cex_dex_candidate`.

Today a swap is a candidate when it is worth at least $1,000, is among the first user transactions of its block (or of
an estimated flashblock), and its price beats Binance's 1-minute open by at least 15 bps; three or more wallets doing
so in one block mark a stale minute, not three trades.

Not trusted because the exchange leg happens off chain and cannot be seen, and 1-minute prices carry 5-10 bps of noise
on majors. Wanted: second-level prices from several exchanges, a way to tell a CEX-DEX arbitrageur from a large
trader (known operators, builder payments, repeat patterns), and a measured false-positive rate.

## 2. Inventory arbitrage: valuation not verified

**Code:** `inventory_arbitrage` in `arbkit/value/pricing.py`.

A route that does not close in one token but ends worth more than it started (two or more swaps at distinct pools,
everything else netting to zero, priced only from stablecoins and Binance prices). One Ethereum sample put $433.9k on
just 17 rows, which is too concentrated to trust. Wanted: a check of what the operator held before and after the
transaction, and of how much of the "gain" is only price movement inside the block.

## 3. JIT liquidity: results not trusted

**Code:** `measure_jit` in `arbkit/scan/block.py`, `arbkit/scan/families.py`; the hidden figures are listed in
`PUBLIC_HIDDEN_FIGURES` (`arbkit/site/views.py`).

The provider's result over its add and remove transactions is measured like a sandwich's. It mixed up unrelated
wallets (an Optimism "JIT bundle" of two unrelated wallets read −$194,430) and counted a USDC gain while leaving out
the unpriced token sold for it (Base). Wanted: a stricter tie between the add and the remove (same position, same
owner) and a valuation that refuses to count when any leg is unpriced.

## 4. Embedded cycles: kept out of every total

**Code:** `mevlab/trace/embedded.py`, `adopt_embedded_cycle` in `arbkit/scan/trace.py`, `HELD_TYPES` in
`arbkit/value/econ.py`.

A cycle run by another party inside someone's transaction (a helper contract's own loop, a hook arbitraging during a
user's swap). On BSC one turned out to be a protocol's own treasury loop. Wanted: a way to tell an independent
operator from the protocol or router the transaction called.

## 5. LP-token burns called by a pool

**Code:** `_conversions` in `mevlab/trace/movements.py`.

Mints that a pool calls on a separate LP token are booked to the pool; burns are still booked to the token contract.
Doing the same for burns broke a counted Ethereum cycle (block 26315403, `0x1ce9b927…`) and turned five other rows
into cycles. Wanted: a narrower rule that recognises a real redemption (the pool pays out the coins the burned LP
tokens were worth) and leaves other burns alone.

## 6. Cross-chain arbitrage: not built

Every chain is classified on its own. Ideas: match one operator's opposite trades on two chains within seconds (same
sender or deployer, shared collectors), and follow bridge transfers that link the legs. Inventory-based cross-chain
arbitrage, with no bridge per trade, is the hard case.
