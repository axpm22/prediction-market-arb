"""Phase 4 / audit E4 + E5.

E4: an aborted Odds-API scan must NOT overwrite complete prior consensus with a thinner
    partial-book one (save_consensus upserts ON CONFLICT DO UPDATE).
E5: a quote with `start_utc is None` has no protectable start, so it must be non-actionable
    (research lane), never pregame.
"""
from argparse import Namespace
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import arb.cli as cli
from arb.cli import run_scan, split_pregame
from arb.config import load_config
from arb.store import Store
from arb.venues.oddsapi import OddsApiCreditsLow, parse_odds_events

from tests.test_cli import kalshi_quote


# ---- E5: null-start is never actionable ----

def test_null_start_quote_is_non_actionable() -> None:
    now = datetime.now(timezone.utc)
    upcoming = kalshi_quote("0.50", start_days_out=1)
    null_start = replace(kalshi_quote("0.50"), start_utc=None)
    pregame, in_play = split_pregame([upcoming, null_start], now)
    assert pregame == [upcoming]                 # only the known future start is actionable
    assert null_start not in pregame
    assert len(in_play) == 1                      # null-start routed to the non-actionable lane
    assert in_play[0].live is True               # persisted live=1, never detected on


# ---- E4: aborted scan preserves prior consensus ----

def _mlb_quotes(now):
    start = now + timedelta(days=2)
    events = [{
        "commence_time": start.isoformat().replace("+00:00", "Z"),
        "home_team": "Kansas City Royals",
        "away_team": "Texas Rangers",
        "bookmakers": [{
            "key": "draftkings",
            "markets": [{"key": "h2h", "outcomes": [
                {"name": "Kansas City Royals", "price": 2.2},
                {"name": "Texas Rangers", "price": 1.7},
            ]}],
        }],
    }]
    return parse_odds_events(events, "baseball_mlb")


def _config(tmp_path):
    return replace(
        load_config(),
        consensus_enabled=True,
        database_path=tmp_path / "arb.sqlite",
        consensus_sports=["baseball_mlb", "basketball_nba"],
        min_books_for_consensus=1,
    )


def _fake_adapter(quotes, abort_on):
    class _Fake:
        name = "oddsapi"

        def __init__(self, config, fixture_dir=None):
            self.credits_remaining = 42

        def fetch_quotes(self, sport):
            if sport == abort_on:
                raise OddsApiCreditsLow("credits below abort threshold")
            return quotes if sport == "baseball_mlb" else []

    return _Fake


def test_aborted_scan_does_not_overwrite_prior_consensus(tmp_path, monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    config = _config(tmp_path)
    store = Store(config.database_path)
    quotes = _mlb_quotes(now)
    key, outcome = quotes[0].event_key, quotes[0].outcome_label
    # A complete prior consensus for this event.
    store.save_consensus({(key, outcome): Decimal("0.50")}, now)

    # MLB returns a full book (would compute ~0.436 for KC); NBA aborts -> partial poll.
    monkeypatch.setattr(cli, "OddsApiAdapter", _fake_adapter(quotes, abort_on="basketball_nba"))
    rc = run_scan(Namespace(dry_run=False, fixtures=False, sport=None), config, store)

    assert rc == 1                                              # aborted
    preserved = store.load_consensus()[(key, outcome)][0]
    assert preserved == Decimal("0.50")                        # NOT overwritten by the partial poll
    assert store.summary()["quotes"] >= 2                      # raw quotes still persisted (rule #7)


def test_clean_scan_does_update_consensus(tmp_path, monkeypatch) -> None:
    """Control: without an abort the same poll DOES refresh consensus (proves the difference
    in the test above is the abort guard, not that consensus never writes)."""
    now = datetime.now(timezone.utc)
    config = _config(tmp_path)
    store = Store(config.database_path)
    quotes = _mlb_quotes(now)
    key, outcome = quotes[0].event_key, quotes[0].outcome_label
    store.save_consensus({(key, outcome): Decimal("0.50")}, now)

    # No sport aborts -> a clean poll.
    monkeypatch.setattr(cli, "OddsApiAdapter", _fake_adapter(quotes, abort_on="__none__"))
    rc = run_scan(Namespace(dry_run=False, fixtures=False, sport=None), config, store)

    assert rc == 0
    refreshed = store.load_consensus()[(key, outcome)][0]
    assert refreshed != Decimal("0.50")                        # consensus refreshed on a clean poll
