from datetime import datetime, timedelta, timezone
from decimal import Decimal

from arb.config import load_config
from arb.models import Quote
from arb.store import Store
from arb.experimental.closing import events_in_window, latest_quotes, record_closing_bets, unused_events

EVENT_KEY = "baseball_mlb:2026-06-10:kansas city royals:texas rangers"
NOW = datetime(2026, 6, 10, 23, 36, tzinfo=timezone.utc)
START = datetime(2026, 6, 10, 23, 40, tzinfo=timezone.utc)  # T-4 minutes from NOW


def stored_quote(cost: str, outcome: str = "kansas city royals", venue: str = "kalshi") -> Quote:
    price = Decimal(cost)
    return Quote(
        event_key=EVENT_KEY, venue=venue, book=f"{venue}-book-{outcome}", market_type="WINNER",
        line=None, outcome_label=outcome, raw_price=price, cost=price,
        fee_cost=Decimal("0.0175"), depth_usd=Decimal("1200"), live=False,
        fetched_at=NOW - timedelta(seconds=60), start_utc=START,
    )


def test_window_selection_is_3_to_5_minutes_before_start() -> None:
    schedule = {
        "in-window": ("baseball_mlb", NOW + timedelta(minutes=4)),
        "too-soon": ("baseball_mlb", NOW + timedelta(minutes=2)),
        "too-far": ("baseball_mlb", NOW + timedelta(minutes=6)),
        "started": ("baseball_mlb", NOW - timedelta(minutes=1)),
    }
    picked = events_in_window(schedule, NOW, window_open_mins=5, window_close_mins=3)
    assert set(picked) == {"in-window"}


def test_consensus_row_is_the_use_once_guard(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    targets = {EVENT_KEY: ("baseball_mlb", START)}
    assert unused_events(store, targets) == targets
    store.save_consensus({(EVENT_KEY, "kansas city royals"): Decimal("0.55")}, NOW)
    assert unused_events(store, targets) == {}


def test_stale_stored_quotes_are_not_compared(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    poll = store.start_poll("watch")
    fresh = stored_quote("0.50")
    stale = Quote(**{**fresh.__dict__, "book": "old-book", "fetched_at": NOW - timedelta(seconds=900)})
    store.save_poll_quotes(poll, [fresh, stale])
    quotes = latest_quotes(store, [EVENT_KEY], NOW, max_age_secs=360)
    assert [q.book for q in quotes] == ["kalshi-book-kansas city royals"]


def test_one_bet_per_outcome_even_with_multiple_venues(tmp_path) -> None:
    config = load_config()
    store = Store(tmp_path / "arb.sqlite")
    poll = store.start_poll("watch")
    # Same outcome on two venues: only the cheaper effective price may be bet.
    store.save_poll_quotes(poll, [
        stored_quote("0.40", venue="kalshi"),
        stored_quote("0.38", venue="polymarket"),
    ])
    closing_cfg = {"window_open_mins": 5, "window_close_mins": 3, "max_quote_age_secs": 360, "sports": ["baseball_mlb"]}
    schedule = {EVENT_KEY: ("baseball_mlb", START)}

    import arb.experimental.closing as closing_module
    original = closing_module.OddsApiAdapter

    class FixtureAdapter(original):
        def __init__(self, cfg, fixture_dir=None):
            from pathlib import Path
            super().__init__(cfg, Path("tests/fixtures"))

    closing_module.OddsApiAdapter = FixtureAdapter
    try:
        record_closing_bets(store, config, closing_cfg, NOW, schedule)
    finally:
        closing_module.OddsApiAdapter = original

    with store.connect() as con:
        rows = list(con.execute("SELECT legs_json FROM flat_bets WHERE notes='closing-line'"))
    assert len(rows) <= 1  # never two bets on one consensus number
    if rows:
        assert '"venue": "polymarket"' in rows[0]["legs_json"]


def test_record_closing_bets_snapshots_once_and_records_ev(tmp_path) -> None:
    config = load_config()
    store = Store(tmp_path / "arb.sqlite")
    poll = store.start_poll("watch")
    store.save_poll_quotes(poll, [stored_quote("0.50"), stored_quote("0.62", outcome="texas rangers")])
    closing_cfg = {"window_open_mins": 5, "window_close_mins": 3, "max_quote_age_secs": 360, "sports": ["baseball_mlb"]}
    schedule = {EVENT_KEY: ("baseball_mlb", START)}

    # Fixture odds give KC a consensus near 0.50; KC at effective 0.5175 may or may not
    # clear EV>0, but the snapshot itself must be stored exactly once either way.
    class FixtureConfig:
        def __getattr__(self, name):
            return getattr(config, name)

    import arb.experimental.closing as closing_module
    original = closing_module.OddsApiAdapter

    class FixtureAdapter(original):
        def __init__(self, cfg, fixture_dir=None):
            from pathlib import Path
            super().__init__(cfg, Path("tests/fixtures"))

    closing_module.OddsApiAdapter = FixtureAdapter
    try:
        first = record_closing_bets(store, config, closing_cfg, NOW, schedule)
        second = record_closing_bets(store, config, closing_cfg, NOW, schedule)
    finally:
        closing_module.OddsApiAdapter = original

    assert second == 0  # use-once guard: the event is never snapshotted twice
    assert unused_events(store, schedule) == {}
    with store.connect() as con:
        rows = list(con.execute("SELECT * FROM flat_bets WHERE notes='closing-line'"))
    assert len(rows) == first
    for row in rows:
        assert row["kind"] == "EV"
        assert Decimal(row["edge"]) > 0
        assert row["status"] == "OPEN"
