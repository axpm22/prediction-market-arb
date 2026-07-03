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


def test_c0_polymarket_quadratic_fee_supersedes_linear() -> None:
    """C0 (updated 2026-07-02, prompt 05): Polymarket's PUBLISHED taker fee is QUADRATIC
    (rate·p·(1−p); docs.polymarket.com/trading/fees, effective 2026-03-30). NegRisk now prices
    both sides on that curve via the single FEE_BASIS. The pre-2026 'linear' C0 rule was a
    conservative over-charge to dodge the Kalshi-curve under-charge bug; it is superseded by the
    confirmed schedule. Two consequences, both correct under the true fee:"""
    fee = Decimal("0.02")
    args = dict(fee_rate=fee, min_edge=Decimal("0.005"), require_exhaustive=True, max_outcomes=30)

    # (a) A basket break-even under the true QUADRATIC fee must NOT flag: Σ(a + fee·a·(1−a)) ≥ 1.
    even = [Decimal("0.497000"), Decimal("0.497000")]  # quad fee ≈ 0.005 each → Σ ≈ 1.004 ≥ 1
    quad_sum = sum((a + (fee * a * (Decimal("1") - a)) for a in even), Decimal("0"))
    assert quad_sum >= Decimal("1")
    assert find_negrisk_arb(event(even), **args) is None

    # (b) A basket that was only "break-even" under the over-conservative LINEAR fee IS a real
    # small edge under the true quadratic fee, and now correctly flags (the edge linear hid).
    linear_break_even = [Decimal("0.490196"), Decimal("0.490196")]  # Σ·(1+fee)=1 under linear
    flagged = find_negrisk_arb(event(linear_break_even), **args)
    assert flagged is not None and flagged.kind == "convert"

    # And a genuinely cheap basket still flags.
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
    # 2026-07-02 (prompt 05): Polymarket fee is QUADRATIC (rate·p·(1−p)) per the published schedule,
    # so the basket costs 0.966800 (was 0.981000 under the superseded linear over-charge).
    assert row["optimistic_cost"] == "0.966800"
    assert row["pnl_optimistic"] == "0.03"  # higher edge under the lower (correct) quadratic fee
    assert row["quality_flag"] == "thin-leg"
    assert "thin-leg" in row["notes"]
