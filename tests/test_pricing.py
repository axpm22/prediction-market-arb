"""Mandatory math vectors (SPEC §7.2) — exact, Decimal, ROUND_HALF_EVEN 6dp."""
from decimal import Decimal

from arb.models import q6
from arb.pricing import (basket_edge, devig, ev, frechet_bounds, independence_fair,
                         stakes_for, true_arb_sum)


def test_devig_proportional_even():
    p = devig([Decimal("1.91"), Decimal("1.91")], "proportional")
    assert p == [Decimal("0.5"), Decimal("0.5")]


def test_devig_proportional_three_way():
    p = devig([Decimal("1.50"), Decimal("2.86"), Decimal("6.00")], "proportional")
    assert [x.quantize(Decimal("0.0001")) for x in p] == \
        [Decimal("0.5635"), Decimal("0.2956"), Decimal("0.1409")]
    assert abs(sum(p) - 1) < Decimal("0.000005")


def test_devig_power_and_shin_run_and_sum_to_one():
    for method in ("power", "shin"):
        p = devig([Decimal("1.50"), Decimal("2.86"), Decimal("6.00")], method)
        assert abs(sum(p) - 1) < Decimal("0.00001"), method
        assert all(isinstance(x, Decimal) for x in p), method
        # power/shin push the favourite up vs proportional (longshot-bias correction)
        assert p[0] >= Decimal("0.5635"), method
    assert devig([Decimal("1.91"), Decimal("1.91")], "power") == \
        [Decimal("0.5"), Decimal("0.5")]


def test_ev_vector():
    # p_hat=0.55, Kalshi ask 0.50, fee 0.0175 -> c=0.5175 -> EV=0.062802
    c = Decimal("0.50") + Decimal("0.0175")
    assert q6(ev(Decimal("0.55"), c)) == Decimal("0.062802")


def test_true_arb_vector():
    # odds 2.10 & 2.05 -> S=0.963995 -> ROI=0.037350; stakes $49.40/$50.60 on B=100
    costs = [1 / Decimal("2.10"), 1 / Decimal("2.05")]
    s = true_arb_sum(costs)
    assert q6(s) == Decimal("0.963995")
    roi = 1 / s - 1
    assert q6(roi) == Decimal("0.037350")
    st = stakes_for(Decimal("100"), costs)
    assert [x.quantize(Decimal("0.01")) for x in st] == [Decimal("49.40"), Decimal("50.60")]
    # equal contract counts identity: stake_i / c_i identical across legs
    assert q6(st[0] / costs[0]) == q6(st[1] / costs[1])


def test_no_arb_vector():
    costs = [1 / Decimal("1.95"), 1 / Decimal("1.95")]
    assert q6(true_arb_sum(costs)) == Decimal("1.025641")  # must NOT flag


def test_negrisk_no_side_vectors():
    # 3 outcomes, NO asks (0.65,0.66,0.67) -> sum 1.98 < 2 -> edge & ROI positive
    s = Decimal("0.65") + Decimal("0.66") + Decimal("0.67")
    edge = basket_edge(s, payout=Decimal(2))
    # edge_points = 1 - S/P = 1 - 0.99 = 0.01; ROI = P/S - 1 = 0.010101
    assert q6(edge) == Decimal("0.01")
    assert q6(Decimal(2) / s - 1) == Decimal("0.010101")
    # (0.67,0.67,0.67) -> sum 2.01 must NOT flag
    assert basket_edge(Decimal("2.01"), payout=Decimal(2)) < 0


def test_negrisk_break_even_does_not_flag():
    # exactly break-even under the correct quadratic fee must NOT flag (bug C0 regression)
    assert basket_edge(Decimal("1.000000"), payout=Decimal(1)) == 0


def test_parlay_vectors():
    legs = [Decimal("0.60"), Decimal("0.55")]
    lo, hi = frechet_bounds(legs)
    assert (lo, hi) == (Decimal("0.15"), Decimal("0.55"))
    assert q6(independence_fair(legs)) == Decimal("0.33")
    assert Decimal("0.12") < lo          # class-1 flag: below lower bound
    assert abs(Decimal("0.25") - independence_fair(legs)) >= Decimal("0.05")  # class-2
