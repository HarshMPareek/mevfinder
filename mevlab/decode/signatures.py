"""Event signatures and topics of everything the classifier reads from receipts: DEX swaps, pool lifecycle,
token transfers, WETH wraps, intent settlements, ERC-6909 claims and V4 hook fees.

Topic hashes are computed with keccak from the canonical signature; nothing is hard-coded."""

from __future__ import annotations

from eth_utils import keccak

# --- signatures -----------------------------------------------------------------

SIG_V2_SWAP = "Swap(address,uint256,uint256,uint256,uint256,address)"
SIG_SOLIDLY_SWAP = "Swap(address,address,uint256,uint256,uint256,uint256)"
SIG_V3_SWAP = "Swap(address,address,int256,int256,uint160,uint128,int24)"
SIG_PANCAKE_V3_SWAP = "Swap(address,address,int256,int256,uint160,uint128,int24,uint128,uint128)"
SIG_V4_SWAP = "Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)"
SIG_V4_INITIALIZE = "Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)"
SIG_BALANCER_SWAP = "Swap(bytes32,address,address,uint256,uint256)"
# Algebra Integral (Base factory 0x36077d39…): V3 Swap + overrideFee + pluginFee, amounts pool-perspective
SIG_ALGEBRA_SWAP = "Swap(address,address,int256,int256,uint160,uint128,int24,uint24,uint24)"
# DODO V2 PMM pools: fromToken, toToken, fromAmount, toAmount, trader, receiver (none indexed)
SIG_DODO_SWAP = "DODOSwap(address,address,uint256,uint256,address,address)"
# Two venue families found by the trace cross-check on Base whose ABI is unpublished (registry rows
# family_ca5f7361 / family_cd8b75a7): topic0 measured on chain, layouts read off the logs and checked against
# the Transfer logs of the same transactions.
#   market-maker pool (one contract, many pairs): (tokenIn, amountIn, amountOut, uint8, recipient)
TOPIC_MM_POOL_SWAP = "0xfcfa612587d1091cc6d8ce39878a7e6e8742c856865fc9d64db3e1a2f1ab52ef"
#   pair pool: (sender, recipient, bool, int256 amount0, int256 amount1, int256, uint256), pool-perspective
TOPIC_PAIR_CD8B_SWAP = "0xcd8b75a7fb6cb82ab3acded68ac53af5c43d19b51a91b9d5640bb73622dbdf57"
# Trader Joe Liquidity Book v2.0 layout (Base factory 0x217da3e5…): one Swap per bin crossed:
# (sender, recipient, id indexed; swapForY, amountIn, amountOut, volatilityAccumulated, fees). [chain] the
# pool receives amountIn + fees and sends amountOut (checked against the Transfer logs of tx 0xcb99ec99…).
# Pools expose getTokenX()/getTokenY(), not token0()/token1(): tokens are inferred from the transfers.
SIG_LB_SWAP = "Swap(address,address,uint256,bool,uint256,uint256,uint256,uint256)"
# Liquidity Book v2.1 / v2.2: Swap(sender indexed, to indexed, uint24 id, bytes32 amountsIn, bytes32 amountsOut,
# uint24 volatilityAccumulator, bytes32 totalFees, bytes32 protocolFees); amounts pack X in the low 128 bits and
# Y in the high 128 bits; amountsIn already includes the fees the pool keeps.
SIG_LB_V21_SWAP = "Swap(address,address,uint24,bytes32,bytes32,uint24,bytes32,bytes32)"
# PancakeSwap Infinity (BSC, V4-style singleton + separate Vault): Uniswap V4's Swap layout plus a trailing
# uint16 protocolFee; Initialize carries hooks, fee, bytes32 parameters, sqrtPriceX96, tick. [untested until the
# BSC validation pass confirms live logs]
SIG_INFINITY_CL_SWAP = "Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24,uint16)"
SIG_INFINITY_CL_INITIALIZE = "Initialize(bytes32,address,address,address,uint24,bytes32,uint160,int24)"
# Curve: StableSwap (int128 ids), CryptoSwap (uint256 ids) and the NG variant with fee + packed price scale.
# The pool's coins are not in the event; token_in / token_out are inferred from the pool's Transfer logs
# (native-ETH legs stay unresolved at the event layer and are left to the trace pass).
SIG_CURVE_STABLE_EXCHANGE = "TokenExchange(address,int128,uint256,int128,uint256)"
SIG_CURVE_CRYPTO_EXCHANGE = "TokenExchange(address,uint256,uint256,uint256,uint256)"
SIG_CURVE_CRYPTO_NG_EXCHANGE = "TokenExchange(address,uint256,uint256,uint256,uint256,uint256,uint256)"
SIG_CURVE_STABLE_UNDERLYING = "TokenExchangeUnderlying(address,int128,uint256,int128,uint256)"
# Maverick V2 pool: PoolSwap(address sender, address recipient, (uint256 amount, bool tokenAIn, bool exactOutput, int32 tickLimit),
# uint256 amountIn, uint256 amountOut); tokens come from the pool's own transfers (tokenA()/tokenB(), no token0()).
SIG_MAVERICK_V2_SWAP = "PoolSwap(address,address,(uint256,bool,bool,int32),uint256,uint256)"
# WOOFi WooPP: WooSwap(fromToken indexed, toToken indexed, fromAmount, toAmount, from, to indexed, rebateTo, swapVol, swapFee)
SIG_WOOFI_SWAP = "WooSwap(address,address,uint256,uint256,address,address,address,uint256,uint256)"
# Balancer V3 Vault: Swap(pool indexed, tokenIn indexed, tokenOut indexed, amountIn, amountOut, swapFeePercentage, swapFeeAmount)
SIG_BALANCER_V3_SWAP = "Swap(address,address,address,uint256,uint256,uint256,uint256)"
SIG_TRANSFER = "Transfer(address,address,uint256)"
SIG_WETH_DEPOSIT = "Deposit(address,uint256)"
SIG_WETH_WITHDRAWAL = "Withdrawal(address,uint256)"

# liquidity events (Layer 0: same-tx mint/burn => zap / JIT, not arb)
SIG_V2_MINT = "Mint(address,uint256,uint256)"
SIG_V2_BURN = "Burn(address,uint256,uint256,address)"
SIG_V3_MINT = "Mint(address,address,int24,int24,uint128,uint256,uint256)"
SIG_V3_BURN = "Burn(address,int24,int24,uint128,uint256,uint256)"
SIG_V4_MODIFY = "ModifyLiquidity(bytes32,address,int24,int24,int256,bytes32)"

# reserve syncs (pre-trade price for V2-style pools)
SIG_V2_SYNC = "Sync(uint112,uint112)"
SIG_SOLIDLY_SYNC = "Sync(uint256,uint256)"

# settlement / intent fills: a loop that closes through one of these is routing, not arbitrage
SIG_UNISWAPX_FILL = "Fill(bytes32,address,address,uint256)"
SIG_1INCH_ORDER_FILLED = "OrderFilled(bytes32,uint256)"
SIG_COW_TRADE = "Trade(address,address,address,uint256,uint256,uint256,bytes)"
SIG_COW_SETTLEMENT = "Settlement(address)"

NATIVE_ETH = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"  # Uniswap V4 native currency


def event_topic(signature: str) -> str:
    return "0x" + keccak(text=signature).hex()


def selector(signature: str) -> str:
    return "0x" + keccak(text=signature)[:4].hex()


TRANSFER_TOPIC = event_topic(SIG_TRANSFER)
APPROVAL_TOPIC = event_topic("Approval(address,address,uint256)")
WETH_DEPOSIT_TOPIC = event_topic(SIG_WETH_DEPOSIT)
WETH_WITHDRAWAL_TOPIC = event_topic(SIG_WETH_WITHDRAWAL)
V4_INITIALIZE_TOPIC = event_topic(SIG_V4_INITIALIZE)
SYNC_TOPICS = {event_topic(SIG_V2_SYNC): "v2", event_topic(SIG_SOLIDLY_SYNC): "solidly"}
SETTLEMENT_TOPICS = {
    event_topic(s): s for s in (SIG_UNISWAPX_FILL, SIG_1INCH_ORDER_FILLED, SIG_COW_TRADE, SIG_COW_SETTLEMENT)
}
LIQUIDITY_TOPICS = {
    event_topic(s): s for s in (SIG_V2_MINT, SIG_V2_BURN, SIG_V3_MINT, SIG_V3_BURN, SIG_V4_MODIFY)
}
# ERC-6909 claim transfers (singleton pool managers mint and burn claims on their currencies)
ERC6909_TRANSFER_TOPIC = event_topic("Transfer(address,address,address,uint256,uint256)")
SINGLETON_SWAP_TOPICS = {event_topic(SIG_V4_SWAP), event_topic(SIG_INFINITY_CL_SWAP)}
# Fee events of Uniswap V4 hooks that keep part of a swap: topic -> layout
HOOK_FEE_EVENTS: dict[str, str] = {
    "0xc532c43b3423e14ef72748f1c8291238829ca0af8ba9b67975ad1483485a4b4d": "pons",
    "0x382c1ddbdd6f153c26f2a003979529bb14fedbaf848e35cb8a347345298bd48b": "splitter",
}


INFINITY_CL_INITIALIZE_TOPIC = event_topic(SIG_INFINITY_CL_INITIALIZE)
