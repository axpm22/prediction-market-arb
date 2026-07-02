"""Offline test for the MLB Stats backfill settler (rescues Odds-API-orphaned bets).

Uses a recorded MLB Stats fixture (real API shape, verified June 2026) so it runs with no
network. Same completed game as the Odds API settlement test: KC 5 - TEX 3 on 2026-06-09.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from arb.models import Event, Opportunity, Quote
from arb.settlement import settle_backfill_mlb
from arb.store import Store
from arb.tracker import record_tracker_poll
from arb.validation import ValidationRule

EVENT = "baseball_mlb:2026-06-09:kansas city royals:texas rangers"
START = datetime(2026, 6, 9, 23, 41, tzinfo=timezone.utc)
FIX = Path("tests/fixtures")


def _ev(outcome: str, cost: str, market_type: str = "WINNER", line: str | None = None) -> Opportunity:
    quote = Quote(
        event_key=EVENT, venue="kalshi", book=f"KXMLB-{outcome}-{line}",
        market_type=market_type, line=None if line is None else Decimal(line),
        outcome_label=outcome, raw_price=Decimal(cost), cost=Decimal(cost),
        fee_cost=Decimal("0.0175"), depth_usd=Decimal("1200"), live=False,
        fetched_at=datetime.now(timezone.utc), start_utc=START,
    )
    event = Event(event_key=EVENT, category="sports", sport="baseball_mlb",
                  home="kansas city royals", away="texas rangers", start_utc=START)
    return Opportunity(kind="EV", category="sports", event=event, legs=[quote],
                       edge=Decimal("0.03"), edge_points=Decimal("0.01"), stakes={}, status="OK")


def _place(store: Store, opps) -> None:
    record_tracker_poll(
        store,
        store.start_poll("watch"),
        opps,
        validation_rule=ValidationRule(min_cost=Decimal("0"), max_cost=Decimal("1"), max_flagged_edge=Decimal("1")),
        now=START - timedelta(hours=1),
    )


def _settled(store: Store):
    with store.connect() as con:
        return list(con.execute("SELECT * FROM flat_bets WHERE status='SETTLED' ORDER BY id"))


def test_backfill_settles_from_mlbstats_fixture(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    _place(store, [
        _ev("kansas city royals", "0.50"),       # KC won
        _ev("texas rangers", "0.40"),            # TEX lost
        _ev("UNDER", "0.50", "TOTAL", "8.5"),    # total 8 < 8.5 -> won
        _ev("OVER", "0.30", "TOTAL", "8.5"),     # lost
    ])
    assert settle_backfill_mlb(store, FIX) == 4
    results = {(r["result"], r["pnl_optimistic"]) for r in _settled(store)}
    assert ("WON", "0.932367") in results     # win at effective 0.5175 -> (1-c)/c = 0.93
    assert ("LOST", "-1.000000") in results
    won = [r for r in _settled(store) if r["result"] == "WON"]
    assert len(won) == 2                  # KC moneyline + UNDER


def test_backfill_is_idempotent(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    _place(store, [_ev("kansas city royals", "0.50")])
    assert settle_backfill_mlb(store, FIX) == 1
    assert settle_backfill_mlb(store, FIX) == 0   # already settled -> no-op
