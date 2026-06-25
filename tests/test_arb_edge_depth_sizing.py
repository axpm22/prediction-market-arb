"""Edge/depth-scaled ARB sizing wired into detection.

The profit-max ladder size jointly encodes EDGE (margin below $1) and DEPTH (how far each
book can be walked before the lock crosses negative). detect_true_arbs(size_by_ladder=True)
must size the basket off that — scaled by config knobs (size_safety_frac, fillable_fraction),
never a hardcoded fraction — and still cap at bankroll * max_stake_pct.
"""
from datetime import datetime, timezone
from decimal import Decimal

from arb.arbitrage import detect_true_arbs
from arb.models import Quote


def _leg(outcome: str, venue: str, ladder: list[tuple[str, str]]) -> Quote:
    top_cost = Decimal(ladder[0][0])
    top_depth = Decimal(ladder[0][1])
    return Quote(
        event_key="baseball_mlb:2026-06-20:los angeles dodgers:new york yankees",
        venue=venue,
        book=f"book_{outcome}",
        market_type="WINNER",
        line=None,
        outcome_label=outcome,
        raw_price=Decimal("2"),
        cost=top_cost,
        fee_cost=Decimal("0"),
        depth_usd=top_depth,
        live=False,
        fetched_at=datetime.now(timezone.utc),
        start_utc=datetime(2026, 6, 20, 23, 5, tzinfo=timezone.utc),
        home="los angeles dodgers",
        away="new york yankees",
        ladder=tuple((Decimal(c), Decimal(d)) for c, d in ladder),
    )


def _legs() -> list[Quote]:
    # Top of book 0.48 + 0.49 = 0.97 (3% lock), thin ($48/$49); deeper levels (0.53/0.52)
    # eventually push Σ over 1, so profit-max sits past the top but well below the full book.
    return [
        _leg("los angeles dodgers", "kalshi", [("0.48", "48"), ("0.53", "5300")]),
        _leg("new york yankees", "polymarket", [("0.49", "49"), ("0.52", "5200")]),
    ]


def _detect(**kwargs):
    return detect_true_arbs(
        _legs(),
        bankroll=Decimal("1000"),
        max_stake_pct=Decimal("0.10"),
        trusted_pairs={"kalshi+polymarket", "kalshi+polymarket:baseball_mlb"},
        stale_warn=Decimal("0.04"),
        **kwargs,
    )


def test_ladder_sizing_differs_from_flat_and_caps_at_bankroll() -> None:
    flat = _detect()
    laddered = _detect(size_by_ladder=True, size_safety_frac=Decimal("0.5"))
    assert len(flat) == 1 and len(laddered) == 1
    flat_total = sum(flat[0].stakes.values())
    ladder_total = sum(laddered[0].stakes.values())
    # Flat sizing uses the full bankroll*max_stake_pct = $100 budget.
    assert flat_total == Decimal("100.00")
    # Ladder sizing walks the book to the profit-max size (smaller than $100 on a thin top
    # level) and stakes a safety fraction of it -> strictly less than the flat budget.
    assert Decimal("0") < ladder_total < flat_total


def test_safety_frac_scales_stake_down() -> None:
    half = sum(_detect(size_by_ladder=True, size_safety_frac=Decimal("0.5"))[0].stakes.values())
    quarter = sum(_detect(size_by_ladder=True, size_safety_frac=Decimal("0.25"))[0].stakes.values())
    # Halving the safety fraction roughly halves the basket (cent-rounding tolerance).
    assert quarter < half
    assert abs(quarter * 2 - half) <= Decimal("0.05")


def test_fillable_haircut_shrinks_ladder_stake() -> None:
    full = sum(_detect(size_by_ladder=True, size_safety_frac=Decimal("0.5"),
                       fillable_fraction=Decimal("1"))[0].stakes.values())
    hair = sum(_detect(size_by_ladder=True, size_safety_frac=Decimal("0.5"),
                       fillable_fraction=Decimal("0.5"))[0].stakes.values())
    # Discounting takeable depth to 50% reduces the profit-max size -> smaller basket.
    assert hair < full
