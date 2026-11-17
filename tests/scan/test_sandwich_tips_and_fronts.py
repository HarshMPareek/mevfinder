"""Cyclic sandwiches whose front is a lost attempted cycle (Ethereum 26334741 #0/#1/#2), and the builder tip a sandwich
pays inside its legs counted once (left out of the operator's result, subtracted by the valuation)."""

from arbkit.scan.sandwich import lost_cycle, operator_result
from tests.mevlab.test_receipt import BOT, EOA, POOL_A, WETH
from tests.verify.test_brackets import BACK, FRONT, receipts_without_swap_events

PRODUCER = "0x" + "c0" * 20


def test_a_lost_attempted_cycle_can_be_a_sandwich_front():
    assert lost_cycle({"assessment": "trace_supported_loss"})
    assert lost_cycle(
        {
            "assessment": "not_arb",
            "arb_class": "attempted_cycle",
            "reasons": ["cycle_lost", "beneficiary_mixed_sign"],
        }
    )
    assert not lost_cycle(
        {"assessment": "not_arb", "arb_class": "attempted_cycle", "reasons": ["no_beneficiary"]}
    )
    assert not lost_cycle({"assessment": "trace_supported", "arb_class": "atomic_cycle", "reasons": []})


def test_the_builder_tip_paid_in_the_legs_is_left_out_of_the_result():
    receipts = receipts_without_swap_events()
    bundle = {"front": FRONT, "back": BACK, "operator": BOT, "pool": POOL_A}
    tip = 15 * 10**15
    traces = {
        FRONT: {"type": "CALL", "from": EOA, "to": BOT, "value": "0x0"},
        BACK: {
            "type": "CALL",
            "from": EOA,
            "to": BOT,
            "value": "0x0",
            "calls": [{"type": "CALL", "from": BOT, "to": PRODUCER, "value": hex(tip)}],
        },
    }
    with_tip = operator_result(bundle, receipts, WETH, traces, EOA, producer=PRODUCER)
    assert with_tip["net_raw"][WETH.lower()] == str(2 * 10**16) and with_tip["paid_to_producer_raw"] == str(
        tip
    )
    # without knowing the producer the tip reads as part of the result (the old double count's first half)
    blind = operator_result(bundle, receipts, WETH, traces, EOA)
    assert blind["net_raw"][WETH.lower()] == str(2 * 10**16 - tip) and blind["paid_to_producer_raw"] == "0"


def test_a_material_unpriced_loss_leaves_the_result_unknown():
    """Base JIT bundles had read +$1-3k from the USDC side while the token sold for it was not priced."""
    from arbkit.chain.spec import load_chain
    from arbkit.value.pricing import PriceBook, value_sandwich

    spec = load_chain("base")
    usdc, token = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", "0x" + "ee" * 20
    book = PriceBook(spec.directory / "validation", offline=True)
    lost = {
        "net_raw": {usdc: str(2745 * 10**6), token: str(-32 * 10**18)},
        "moved_raw": {usdc: str(2745 * 10**6), token: str(32 * 10**18)},
    }
    value_sandwich(spec, book, lost, 1_700_000_000, {}, 3000.0)
    assert (
        lost["profit_usd"] is None
        and lost["price_source"] == "unpriced"
        and lost["unpriced_losses"] == [token]
    )
    dust = {
        "net_raw": {usdc: str(2745 * 10**6), token: str(-1 * 10**18)},
        "moved_raw": {usdc: str(2745 * 10**6), token: str(64 * 10**18)},
        "bundle_fee_wei": 0,
    }
    value_sandwich(spec, book, dust, 1_700_000_000, {}, 3000.0)
    assert round(dust["profit_usd"]) == 2745 and "unpriced_losses" not in dust
