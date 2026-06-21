from datetime import datetime, timezone
from decimal import Decimal

from arb.models import Event, Opportunity, Quote
from arb.paper import fill_opportunity
from arb.store import Store

EVENT_KEY = "baseball_mlb:2026-06-20:los angeles dodgers:new york yankees"


def opportunity(kind: str, status: str) -> Opportunity:
    quote = Quote(
        event_key=EVENT_KEY,
        venue="kalshi",
        book="KXMLBGAME-26JUN20-LADNYY-LAD",
        market_type="WINNER",
        line=None,
        outcome_label="los angeles dodgers",
        raw_price=Decimal("0.48"),
        cost=Decimal("0.48"),
        fee_cost=Decimal("0"),
        depth_usd=Decimal("1200"),
        live=False,
        fetched_at=datetime.now(timezone.utc),
    )
    event = Event(
        event_key=EVENT_KEY,
        category="sports",
        sport="baseball_mlb",
        home="los angeles dodgers",
        away="new york yankees",
        start_utc=datetime(2026, 6, 20, 23, 5, tzinfo=timezone.utc),
    )
    return Opportunity(
        kind=kind,  # type: ignore[arg-type]
        category="sports",
        event=event,
        legs=[quote],
        edge=Decimal("0.04"),
        stakes={},
        status=status,  # type: ignore[arg-type]
    )


def test_divergence_and_review_are_never_filled(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    bankroll, stake_pct = Decimal("1000"), Decimal("0.10")
    assert fill_opportunity(store, 1, opportunity("DIVERGENCE", "OK"), bankroll, stake_pct) is None
    assert fill_opportunity(store, 1, opportunity("ARB", "REVIEW"), bankroll, stake_pct) is None
    assert fill_opportunity(store, 1, opportunity("ARB", "OK"), bankroll, stake_pct) is not None
