from datetime import datetime, timezone
from decimal import Decimal

from arb.arbitrage import detect_true_arbs
from arb.models import Quote
from arb.pricing import implied_from_decimal_odds, q6


def quote(outcome: str, book: str, odds: str, fetched_at: datetime | None = None,
          venue: str = "kalshi") -> Quote:
    return Quote(
        event_key="baseball_mlb:2026-06-20:los angeles dodgers:new york yankees",
        venue=venue,
        book=book,
        market_type="WINNER",
        line=None,
        outcome_label=outcome,
        raw_price=Decimal(odds),
        cost=q6(implied_from_decimal_odds(Decimal(odds))),
        fee_cost=Decimal("0"),
        depth_usd=None,
        live=False,
        fetched_at=fetched_at or datetime.now(timezone.utc),
        start_utc=datetime(2026, 6, 20, 23, 5, tzinfo=timezone.utc),
        home="los angeles dodgers",
        away="new york yankees",
    )


def test_true_arb_vector() -> None:
    opportunities = detect_true_arbs(
        [
            quote("los angeles dodgers", "book_a", "2.10"),
            quote("new york yankees", "book_b", "2.05"),
        ],
        bankroll=Decimal("1000"),
        max_stake_pct=Decimal("0.10"),
        trusted_pairs=set(),
        stale_warn=Decimal("0.04"),
    )
    assert len(opportunities) == 1
    # ROI from q6-rounded effective costs (0.476190 + 0.487805 = 0.963995); 1/S - 1.
    assert opportunities[0].edge == Decimal("0.037350")
    assert opportunities[0].edge_points == Decimal("0.036005")  # #9 margin below $1 (1-S)
    assert opportunities[0].stakes == {
        "los angeles dodgers": Decimal("49.40"),
        "new york yankees": Decimal("50.60"),
    }
    assert opportunities[0].status == "OK"


def test_legs_fetched_minutes_apart_are_flagged_time_skew() -> None:
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    opportunities = detect_true_arbs(
        [
            quote("los angeles dodgers", "book_a", "2.10", fetched_at=now - timedelta(seconds=180)),
            quote("new york yankees", "book_b", "2.05", fetched_at=now),
        ],
        bankroll=Decimal("1000"),
        max_stake_pct=Decimal("0.10"),
        trusted_pairs=set(),
        stale_warn=Decimal("0.04"),
        max_leg_skew_secs=60,
    )
    assert len(opportunities) == 1
    assert opportunities[0].status == "REVIEW"
    assert any(note.startswith("time-skew:") for note in opportunities[0].notes)

    simultaneous = detect_true_arbs(
        [
            quote("los angeles dodgers", "book_a", "2.10", fetched_at=now),
            quote("new york yankees", "book_b", "2.05", fetched_at=now),
        ],
        bankroll=Decimal("1000"),
        max_stake_pct=Decimal("0.10"),
        trusted_pairs=set(),
        stale_warn=Decimal("0.04"),
        max_leg_skew_secs=60,
    )
    assert simultaneous[0].status == "OK"


def test_no_arb_vector() -> None:
    opportunities = detect_true_arbs(
        [
            quote("los angeles dodgers", "book_a", "1.95"),
            quote("new york yankees", "book_b", "1.95"),
        ],
        bankroll=Decimal("1000"),
        max_stake_pct=Decimal("0.10"),
        trusted_pairs=set(),
        stale_warn=Decimal("0.04"),
    )
    assert opportunities == []
