from datetime import datetime, timedelta, timezone
from decimal import Decimal

from arb.config import load_config
from arb.models import Quote
from arb.store import Store
from arb.experimental.focus import build_bet_map, run_focus_pass

EVENT = "baseball_mlb:2026-06-12:kansas city royals:texas rangers"
FCFG = {"rotation_pause_secs": 0, "map_max_age_mins": 30, "sports": ["baseball_mlb", "basketball_nba"]}


def stored(venue: str, outcome: str, cost: str, event: str = EVENT, age_mins: int = 5) -> Quote:
    price = Decimal(cost)
    return Quote(
        event_key=event, venue=venue, book=f"{venue}-{outcome}", market_type="WINNER",
        line=None, outcome_label=outcome, raw_price=price, cost=price, fee_cost=Decimal("0"),
        depth_usd=Decimal("500"), live=False,
        fetched_at=datetime.now(timezone.utc) - timedelta(minutes=age_mins),
    )


def seed(store: Store, quotes: list[Quote]) -> None:
    store.save_poll_quotes(store.start_poll("watch"), quotes)


def test_bet_map_requires_two_venues_and_fresh_quotes(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    seed(store, [
        stored("kalshi", "kansas city royals", "0.48"),
        stored("polymarket", "texas rangers", "0.50"),
        stored("kalshi", "a", "0.50", event="baseball_mlb:2026-06-12:a:b"),          # single venue
        stored("kalshi", "c", "0.50", event="baseball_mlb:2026-06-12:c:d:0", age_mins=90),  # too old
        stored("polymarket", "c", "0.50", event="baseball_mlb:2026-06-12:c:d:0", age_mins=90),
    ])
    bet_map = build_bet_map(store, datetime.now(timezone.utc), max_age_mins=30, sports=["baseball_mlb"])
    assert set(bet_map) == {EVENT}
    assert len(bet_map[EVENT]) == 2


def test_focus_pass_detects_on_simultaneous_requotes_and_confirms_existing(tmp_path) -> None:
    config = load_config()
    store = Store(tmp_path / "arb.sqlite")
    seed(store, [
        stored("kalshi", "kansas city royals", "0.48"),
        stored("polymarket", "texas rangers", "0.50"),
    ])
    # Re-quotes still sum below 1 after fees + execution buffer.
    fetchers = {
        "kalshi": lambda leg: (Decimal("0.47"), Decimal("400")),
        "polymarket": lambda leg: (Decimal("0.46"), Decimal("400")),
    }
    n_events, n_opps = run_focus_pass(store, config, FCFG, fetchers)
    assert n_events == 1
    assert n_opps >= 1
    with store.connect() as con:
        first = [dict(r) for r in con.execute("SELECT * FROM flat_bets WHERE kind='ARB' ORDER BY id")]
    assert first[0]["status"] == "OPEN"
    assert "focus-simultaneous" in first[0]["notes"].split(",")

    # Second pass re-finds the same fingerprint → CONFIRMED at simultaneous prices,
    # and never closes anything it didn't open.
    run_focus_pass(store, config, FCFG, fetchers)
    with store.connect() as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM flat_bets WHERE kind='ARB' ORDER BY id")]
    assert [r["status"] for r in rows] == ["OPEN", "CONFIRMED"]
    assert "focus-confirmed-simultaneous" in rows[1]["notes"].split(",")
    assert rows[1]["pnl_confirmed"] is not None
    with store.connect() as con:
        closes = list(con.execute("SELECT * FROM flat_bets WHERE kind='CLOSE'"))
    assert closes == []


def test_focus_pass_with_dead_books_records_nothing(tmp_path) -> None:
    config = load_config()
    store = Store(tmp_path / "arb.sqlite")
    seed(store, [
        stored("kalshi", "kansas city royals", "0.48"),
        stored("polymarket", "texas rangers", "0.50"),
    ])
    fetchers = {"kalshi": lambda leg: None, "polymarket": lambda leg: None}
    n_events, n_opps = run_focus_pass(store, config, FCFG, fetchers)
    assert (n_events, n_opps) == (1, 0)
