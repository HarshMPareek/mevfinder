"""Liquidation assets the event does not name come from the same receipt's transfers (Morpho Blue, Compound V2)."""

from arbkit.scan.families import assets_from_transfers

T = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
MORPHO, LIQ, USDC, WETH = (
    "0xbbbbbbbbbb9cc5e90e3b3af64bdaf62c37eeffcb",
    "0x548549d2037a9d45026e9dec3f487fd95a034f37",
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    "0x4200000000000000000000000000000000000006",
)


def log(token, a, b, amount):
    return {
        "address": token,
        "topics": [T, "0x" + a[2:].rjust(64, "0"), "0x" + b[2:].rjust(64, "0")],
        "data": "0x" + f"{amount:064x}",
    }


def test_morpho_assets_come_from_the_matching_transfers():
    liq = {
        "protocol": "morpho_blue",
        "contract": MORPHO,
        "debt_asset": None,
        "collateral_asset": None,
        "debt_repaid_raw": "53253",
        "collateral_seized_raw": "54639817486338797",
    }
    logs = [
        log(WETH, MORPHO, LIQ, 54639817486338797),
        log(USDC, LIQ, MORPHO, 53253),
        log(USDC, LIQ, MORPHO, 999),
    ]
    assets_from_transfers(liq, logs)
    assert liq["debt_asset"] == USDC and liq["collateral_asset"] == WETH


def test_no_matching_amount_leaves_the_asset_unknown():
    liq = {
        "protocol": "morpho_blue",
        "contract": MORPHO,
        "debt_asset": None,
        "collateral_asset": None,
        "debt_repaid_raw": "1",
        "collateral_seized_raw": "2",
    }
    assets_from_transfers(liq, [log(USDC, LIQ, MORPHO, 53253)])
    assert liq["debt_asset"] is None and liq["collateral_asset"] is None
