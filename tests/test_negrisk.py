import json
from decimal import Decimal
from pathlib import Path

from arb.negrisk import find_negrisk_arb

ARGS = dict(fee_rate=Decimal("0"), min_edge=Decimal("0.005"), require_exhaustive=True, max_outcomes=30)


def event(asks, bids=None, neg=True, depths=None):
    bids = bids or [a - Decimal("0.02") for a in asks]
    depths = depths or [Decimal("500")] * len(asks)
    return {
        "id": "e1", "title": "T", "negRisk": neg,
        "markets": [
            {"yes_best_ask": str(a), "yes_best_bid": str(b), "yes_ask_depth": str(d)}
            for a, b, d in zip(asks, bids, depths)
        ],
    }


def test_convert_arb_vector() -> None:
    # BUILD_PLAN S1 vector: asks 0.30/0.30/0.35 → Σ=0.95 → edge 1/0.95-1.
    arb = find_negrisk_arb(event([Decimal("0.30"), Decimal("0.30"), Decimal("0.35")]), **ARGS)
    assert arb is not None and arb.kind == "convert"
    assert arb.basket_sum == Decimal("0.950000")
    assert arb.edge == (Decimal("1") / Decimal("0.95") - Decimal("1")).quantize(Decimal("0.000001"))
    assert arb.basket_n == 3


def test_overshoot_arb_vector() -> None:
    # bids 0.40/0.35/0.30 → Σ=1.05 → overshoot edge 0.05.
    arb = find_negrisk_arb(
        event([Decimal("0.50"), Decimal("0.50"), Decimal("0.50")],
              bids=[Decimal("0.40"), Decimal("0.35"), Decimal("0.30")]), **ARGS)
    assert arb is not None and arb.kind == "overshoot"
    assert arb.edge == Decimal("0.050000")


def test_no_arb_when_basket_sums_to_one() -> None:
    assert find_negrisk_arb(event([Decimal("0.34"), Decimal("0.33"), Decimal("0.34")]), **ARGS) is None


def test_thin_leg_caps_capturable() -> None:
    arb = find_negrisk_arb(
        event([Decimal("0.30"), Decimal("0.30"), Decimal("0.35")], depths=[Decimal("500"), Decimal("500"), Decimal("10")]),
        **ARGS)
    assert arb.min_depth_usd == Decimal("10")          # one thin leg caps the basket
    assert arb.capturable_usd == (Decimal("10") * arb.edge).quantize(Decimal("0.000001"))


def test_non_exhaustive_set_is_rejected() -> None:
    # negRisk metadata false → cannot prove exhaustive+exclusive → no auto arb.
    assert find_negrisk_arb(event([Decimal("0.30"), Decimal("0.30"), Decimal("0.35")], neg=False), **ARGS) is None


def test_recorded_real_fixture_parses_and_is_no_arb() -> None:
    ev = json.loads(Path("tests/fixtures/polymarket_negrisk_event.json").read_text())
    # Real Spain vs Saudi Arabia: Σ asks = 1.008 > 1 → correctly NOT a convert arb.
    assert find_negrisk_arb(ev, **ARGS) is None
