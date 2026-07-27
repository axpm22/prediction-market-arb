"""Offline policy replay (PROMPT_REPLAY): re-run detect() over stored quotes.
Read-only by construction — no test here opens the live DB."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from arb.cycle import _quote_row
from arb.replay import quote_from_row

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 7, 25, 18, 0, tzinfo=timezone.utc)
START = NOW + timedelta(hours=2)
EK = "baseball_mlb:2026-07-25:mets:royals"

DETECT_FIELDS = ("event_key", "venue", "book", "market_type", "line", "outcome_label",
                 "side", "raw_price", "cost", "fee_cost", "depth_usd", "ladder_json",
                 "live", "fetched_at", "category", "sport", "bid", "volume_24h",
                 "start_utc", "start_grade", "venue_market_id")


def _q(**kw):
    base = dict(event_key=EK, venue="kalshi", book="kalshi", market_type="WINNER",
                line=None, outcome_label="mets", side="YES",
                raw_price=Decimal("0.47"), cost=Decimal("0.47"),
                fee_cost=Decimal("0.01"), depth_usd=Decimal("100"),
                ladder_json=None, live=False, fetched_at=NOW, category="sports",
                sport="baseball_mlb", bid=Decimal("0.45"),
                volume_24h=Decimal("1000"), home="royals", away="mets",
                start_utc=START, start_grade="verified", venue_market_id="MKT-1")
    base.update(kw)
    from arb.models import Quote
    return Quote(**base)


ROUND_TRIP_CASES = [
    _q(),                                                   # plain YES WINNER
    _q(side="NO", outcome_label="royals"),                  # NO side
    _q(line=None, market_type="WINNER", start_grade="absent", start_utc=None),
    _q(market_type="TOTAL", line="8.5", outcome_label="OVER"),
    _q(depth_usd=None, bid=None, volume_24h=None, ladder_json='[["0.47","100"]]'),
    _q(live=True, venue="polymarket", book="polymarket"),
]


def test_quote_from_row_is_the_inverse_of_quote_row():
    for q in ROUND_TRIP_CASES:
        back = quote_from_row(_quote_row(q, poll_id=7))
        for f in DETECT_FIELDS:
            assert getattr(back, f) == getattr(q, f), f
        assert isinstance(back.cost, Decimal) and isinstance(back.fee_cost, Decimal)
        assert isinstance(back.raw_price, Decimal)
        assert back.depth_usd is None or isinstance(back.depth_usd, Decimal)
        assert back.effective_cost == q.effective_cost


def test_quote_from_row_parses_home_away_from_event_key():
    # quotes never store venue-raw home/away (_quote_row, arb/cycle.py:22)
    back = quote_from_row(_quote_row(_q(), poll_id=1))
    assert (back.away, back.home) == ("mets", "royals")
    dh = quote_from_row(_quote_row(_q(event_key=EK + ":dh2"), poll_id=1))
    assert (dh.away, dh.home) == ("mets", "royals")


def test_quote_from_row_leaves_venue_side_empty():
    # reconcile-only field, absent from the quotes table and unread by detect()
    assert quote_from_row(_quote_row(_q(), poll_id=1)).venue_side == ""
