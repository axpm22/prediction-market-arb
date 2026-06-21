from datetime import datetime, timezone
from decimal import Decimal

from arb.models import Event, Opportunity, Quote
from arb.quality import classify_quality
from arb.sizing import kelly_f_star, kelly_stake

EVENT = Event(event_key="e", category="sports", sport="baseball_mlb", home="a", away="b",
              start_utc=datetime(2026, 6, 12, tzinfo=timezone.utc))


def opp(edge: str, depth: str) -> Opportunity:
    q = Quote(event_key="e", venue="kalshi", book="K", market_type="WINNER", line=None,
              outcome_label="a", raw_price=Decimal("0.5"), cost=Decimal("0.5"),
              fee_cost=Decimal("0"), depth_usd=Decimal(depth), live=False,
              fetched_at=datetime.now(timezone.utc))
    return Opportunity(kind="EV", category="sports", event=EVENT, legs=[q],
                       edge=Decimal(edge), stakes={}, status="REVIEW")


# ---- Phase 1 quality ----

def test_suspect_edge_beats_thin_book_precedence() -> None:
    # A 49.0 "edge" is the stale-book leak the EV buckets showed.
    assert classify_quality(opp("49.0", "5"), Decimal("50"), Decimal("0.25")) == "suspect-edge"


def test_thin_book_flag() -> None:
    assert classify_quality(opp("0.06", "10"), Decimal("50"), Decimal("0.25")) == "thin-book"


def test_clean_passes() -> None:
    assert classify_quality(opp("0.06", "500"), Decimal("50"), Decimal("0.25")) is None


# ---- Phase 3 Kelly (exact, Decimal) ----

def test_kelly_f_star_and_stake_vector() -> None:
    # EV_SPEC §8 / Phase 3: p=0.55, c=0.5175 → f* = (0.55-0.5175)/(1-0.5175).
    p, c = Decimal("0.55"), Decimal("0.5175")
    f = (p - c) / (Decimal("1") - c)
    assert kelly_f_star(p, c) == f.quantize(Decimal("0.000001"))
    # quarter-Kelly on $1000, well under the 5% cap.
    stake = kelly_stake(p, c, Decimal("0.25"), Decimal("1000"), Decimal("0.05"))
    assert stake == (Decimal("0.25") * kelly_f_star(p, c) * Decimal("1000")).quantize(Decimal("0.01"))


def test_kelly_zero_when_no_edge() -> None:
    assert kelly_f_star(Decimal("0.40"), Decimal("0.50")) == Decimal("0")
    assert kelly_stake(Decimal("0.40"), Decimal("0.50"), Decimal("0.25"), Decimal("1000"), Decimal("0.05")) == Decimal("0.00")


def test_kelly_respects_bankroll_cap() -> None:
    # Huge edge → f* large, but stake capped at 5% of bankroll.
    stake = kelly_stake(Decimal("0.95"), Decimal("0.10"), Decimal("0.25"), Decimal("1000"), Decimal("0.05"))
    assert stake == Decimal("50.00")  # 0.05 * 1000
