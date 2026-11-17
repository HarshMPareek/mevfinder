"""A closed loop in one token whose surplus is converted into another is a cycle with the profit taken in that other
token (BSC 131019461 #21: USDT -> X -> USDT through two Uniswap V4 pools, +0.012649 USDT converted to WBNB on V3)."""

from mevlab.decode.model import SwapLeg
from mevlab.receipt.path import analyze_path

USDT, X, WBNB = "0x55d3", "0xc47c", "0xbb4c"
V4, V3 = "0x28e2ea090877bf75740558f6bfb36a5ffee9e9df", "0x172fcd41e0913e95784454622d1c3724f546f849"


def leg(pool, pool_id, t_in, a_in, t_out, a_out, i):
    lg = SwapLeg(kind="clmm_v4_singleton" if pool_id else "clmm_v3", pool=pool, log_index=i, pool_id=pool_id)
    lg.token_in, lg.amount_in, lg.token_out, lg.amount_out = t_in, a_in, t_out, a_out
    return lg


def test_loop_through_two_singleton_pools_with_its_surplus_converted_is_a_cycle():
    legs = [
        leg(V4, "0x7827", USDT, 622340182348917760, X, 4033857485175172616725106, 1),
        leg(V4, "0xd646", X, 4033857485175172616725106, USDT, 634989514625867058, 2),
        leg(V3, None, USDT, 12649332276949298, WBNB, 17743897175251, 3),
    ]
    p = analyze_path(legs)
    assert p.is_cycle and p.profit_token == WBNB and p.gain == 17743897175251
    assert "profit_converted" in p.reasons


def test_a_user_split_route_is_never_a_converted_cycle():
    # USDT sold for WBNB along two paths (direct, and through X): USDT is sold and never bought back
    legs = [
        leg(V3, None, USDT, 500, WBNB, 7, 1),
        leg(V4, "0x7827", USDT, 500, X, 3000, 2),
        leg(V4, "0xd646", X, 3000, WBNB, 7, 3),
    ]
    assert not analyze_path(legs).is_cycle


def test_a_round_trip_in_one_pool_converted_is_not_a_cycle():
    # buying and selling X back in the same pool is a wash, whatever is converted afterwards
    legs = [
        leg(V4, "0x7827", USDT, 100, X, 1000, 1),
        leg(V4, "0x7827", X, 1000, USDT, 101, 2),
        leg(V3, None, USDT, 1, WBNB, 2, 3),
    ]
    assert not analyze_path(legs).is_cycle
