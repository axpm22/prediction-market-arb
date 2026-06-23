import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from arb.config import load_config
from arb.experimental.negrisk_sweep import run_negrisk_pass
from arb.negrisk import find_negrisk_arb
from arb.store import Store

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
    assert len(arb.legs) == 3
    assert arb.legs[0]["side"] == "BUY_YES"
    assert arb.legs[0]["effective_price"] == "0.300000"


def test_overshoot_arb_vector() -> None:
    # bids 0.40/0.35/0.30 → Σ=1.05 → overshoot edge 0.05.
    arb = find_negrisk_arb(
        event([Decimal("0.50"), Decimal("0.50"), Decimal("0.50")],
              bids=[Decimal("0.40"), Decimal("0.35"), Decimal("0.30")]), **ARGS)
    assert arb is not None and arb.kind == "overshoot"
    assert arb.edge == Decimal("0.050000")


def test_no_arb_when_basket_sums_to_one() -> None:
    assert find_negrisk_arb(event([Decimal("0.34"), Decimal("0.33"), Decimal("0.34")]), **ARGS) is None


def test_c0_linear_fee_break_even_basket_is_not_an_arb() -> None:
    """C0: with a non-zero Polymarket taker fee, a basket that is break-even under the LINEAR
    fee model must NOT flag. The old Kalshi-quadratic fee (rate·p·(1−p)) under-charged, pushing
    Σasks+fees below 1 and manufacturing a fake convert arb in the safest class."""
    fee = Decimal("0.02")
    asks = [Decimal("0.490196"), Decimal("0.490196")]  # Σ·(1+fee) = 1.000000 exactly (linear)
    args = dict(fee_rate=fee, min_edge=Decimal("0.005"), require_exhaustive=True, max_outcomes=30)

    # Linear (correct): effective ask = 0.5 each → Σ = 1.000000 → not < 1 → no arb.
    assert find_negrisk_arb(event(asks), **args) is None

    # Sanity: the OLD quadratic curve would have under-charged and flagged this break-even basket.
    quad_sum = sum((a + (fee * a * (Decimal("1") - a)) for a in asks), Decimal("0"))
    assert quad_sum < Decimal("1")   # < 1 -> the pre-C0 code would have manufactured a fake arb

    # And a genuinely cheap basket still flags under linear fees.
    cheap = find_negrisk_arb(event([Decimal("0.45"), Decimal("0.45")]), **args)
    assert cheap is not None and cheap.kind == "convert"


def test_thin_leg_caps_capturable() -> None:
    arb = find_negrisk_arb(
        event([Decimal("0.30"), Decimal("0.30"), Decimal("0.35")], depths=[Decimal("500"), Decimal("500"), Decimal("10")]),
        **ARGS)
    assert arb.min_depth_usd == Decimal("10")          # one thin leg caps the basket
    assert arb.capturable_usd == (Decimal("10") * arb.edge).quantize(Decimal("0.000001"))


def test_thin_leg_is_flagged_against_executable_floor() -> None:
    arb = find_negrisk_arb(
        event([Decimal("0.30"), Decimal("0.30"), Decimal("0.35")], depths=[Decimal("500"), Decimal("500"), Decimal("10")]),
        **ARGS,
        min_executable_usd=Decimal("50"),
    )
    assert arb is not None
    assert arb.quality_flag == "thin-leg"


def test_non_exhaustive_set_is_rejected() -> None:
    # negRisk metadata false → cannot prove exhaustive+exclusive → no auto arb.
    assert find_negrisk_arb(event([Decimal("0.30"), Decimal("0.30"), Decimal("0.35")], neg=False), **ARGS) is None


def test_recorded_real_fixture_parses_and_is_no_arb() -> None:
    ev = json.loads(Path("tests/fixtures/polymarket_negrisk_event.json").read_text())
    # Real Spain vs Saudi Arabia: Σ asks = 1.008 > 1 → correctly NOT a convert arb.
    assert find_negrisk_arb(ev, **ARGS) is None


def test_sweep_persists_executable_legs_and_quality_flag(tmp_path) -> None:
    config = replace(load_config(), database_path=tmp_path / "arb.sqlite")
    store = Store(config.database_path)
    # Lever C lowered the negRisk floor to $5 (single-settlement, no cross-venue leg risk), so
    # the thin leg must now be below $5 to be flagged thin (a $10 leg is executable now).
    thin_event = event(
        [Decimal("0.28"), Decimal("0.30"), Decimal("0.34")],
        depths=[Decimal("500"), Decimal("500"), Decimal("2")],
    )

    assert run_negrisk_pass(store, config, fetcher=lambda: [thin_event], book_filler=lambda e: e) == (1, 1)
    with store.connect() as con:
        [row] = list(con.execute("SELECT * FROM flat_bets WHERE kind='NEGRISK'"))

    legs = json.loads(row["legs_json"])
    assert len(legs) == 3
    # C0: linear Polymarket fees (rate·price) — was 0.966800/"0.03" under the wrong quadratic curve.
    assert row["optimistic_cost"] == "0.981000"
    assert row["pnl_optimistic"] == "0.02"
    assert row["quality_flag"] == "thin-leg"
    assert "thin-leg" in row["notes"]
