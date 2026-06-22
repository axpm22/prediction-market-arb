from datetime import datetime, timedelta, timezone
from decimal import Decimal

from arb.models import Quote
from arb.experimental.parlay import classify_parlay_ask, classify_parlay_bid, frechet_bounds, independence_fair
from arb.experimental.timing import split_by_timing

LEGS = [Decimal("0.60"), Decimal("0.55")]


def test_spec_11_vectors() -> None:
    lower, upper = frechet_bounds(LEGS)
    assert lower == Decimal("0.15")
    assert upper == Decimal("0.55")
    assert independence_fair(LEGS) == Decimal("0.33")
    threshold = Decimal("0.05")
    assert classify_parlay_ask(Decimal("0.12"), LEGS, threshold, same_game=False) == "frechet-underpriced"
    assert classify_parlay_ask(Decimal("0.25"), LEGS, threshold, same_game=False) == "independence-cheap"
    assert classify_parlay_ask(Decimal("0.32"), LEGS, threshold, same_game=False) is None
    # Same-game legs: independence is not a bound; only Fréchet violations signal.
    assert classify_parlay_ask(Decimal("0.25"), LEGS, threshold, same_game=True) is None
    assert classify_parlay_bid(Decimal("0.60"), LEGS) == "frechet-overpriced"


def quote_at(start_offset_mins: int, sport: str = "basketball_nba") -> Quote:
    now = datetime(2026, 6, 11, 2, 0, tzinfo=timezone.utc)
    return Quote(
        event_key=f"{sport}:2026-06-11:a:b", venue="kalshi", book="B", market_type="WINNER",
        line=None, outcome_label="a", raw_price=Decimal("0.5"), cost=Decimal("0.5"),
        fee_cost=Decimal("0"), depth_usd=None, live=False, fetched_at=now,
        start_utc=now - timedelta(minutes=start_offset_mins),
    )


def test_break_window_split() -> None:
    now = datetime(2026, 6, 11, 2, 0, tzinfo=timezone.utc)
    windows = {"basketball_nba": [48, 15], "soccer": [45, 15]}
    pregame, in_break, in_play = split_by_timing(
        [quote_at(-10), quote_at(50), quote_at(70), quote_at(44, "soccer")], windows, now
    )
    assert len(pregame) == 1          # starts in 10 minutes
    assert len(in_break) == 1         # NBA at start+50min, inside [48, 63]
    assert len(in_play) == 2          # NBA at +70 past window; soccer at +44 before its window
    assert all(q.live for q in in_break + in_play)
