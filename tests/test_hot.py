from datetime import datetime, timedelta, timezone
from decimal import Decimal

from arb.config import load_config
from arb.models import Event, Opportunity, Quote
from arb.store import Store
from arb.tracker import record_tracker_poll
from arb.experimental.hot import run_hot_pass, unwatched_bets, watch_bet

EVENT = "baseball_mlb:2026-06-12:kansas city royals:texas rangers"
HCFG = {"poll_secs": 2, "check_interval_secs": 0.01, "watch_duration_secs": 1.0,
        "dead_checks": 2, "max_watchers": 3}


def leg(venue: str, outcome: str, cost: str) -> Quote:
    price = Decimal(cost)
    return Quote(
        event_key=EVENT, venue=venue, book=f"{venue}-{outcome}", market_type="WINNER",
        line=None, outcome_label=outcome, raw_price=price, cost=price, fee_cost=Decimal("0"),
        depth_usd=Decimal("500"), live=False, fetched_at=datetime.now(timezone.utc),
        start_utc=datetime.now(timezone.utc) + timedelta(hours=4),
    )


def place_arb(store: Store) -> None:
    event = Event(event_key=EVENT, category="sports", sport="baseball_mlb",
                  home="kansas city royals", away="texas rangers",
                  start_utc=datetime.now(timezone.utc) + timedelta(hours=4))
    opp = Opportunity(kind="ARB", category="sports", event=event,
                      legs=[leg("kalshi", "kansas city royals", "0.48"),
                            leg("polymarket", "texas rangers", "0.48")],
                      edge=Decimal("0.041667"), stakes={}, status="REVIEW")
    record_tracker_poll(store, store.start_poll("watch"), [opp])


def scripted_fetcher(sequence: list[Decimal | None]):
    state = {"i": 0}

    def fetch(leg_dict):
        value = sequence[min(state["i"], len(sequence) - 1)]
        state["i"] += 1
        return value

    return fetch


def test_watcher_tracks_lifetime_until_edge_dies(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    place_arb(store)
    [bet] = unwatched_bets(store, datetime.now(timezone.utc), 900)
    # Both legs alive at 0.49 for 3 checks, then the kalshi leg's book dies.
    fetchers = {
        "kalshi": scripted_fetcher([Decimal("0.49")] * 3 + [None] * 10),
        "polymarket": lambda l: Decimal("0.49"),
    }
    summary = watch_bet(store, bet, fetchers, HCFG, sleeper=lambda s: None)
    assert summary["first_edge"] == Decimal("0.020408")
    assert summary["best_edge"] == Decimal("0.020408")
    assert summary["survived_window"] is False
    with store.connect() as con:
        lifetime = list(con.execute("SELECT * FROM flat_bets WHERE status='LIFETIME'"))
        samples = list(con.execute("SELECT * FROM hot_samples"))
    assert len(lifetime) == 1
    assert "alive=" in lifetime[0]["notes"]
    assert len(samples) == summary["checks"]


def test_each_bet_watched_once(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    place_arb(store)
    config = load_config()
    fetchers = {"kalshi": lambda l: None, "polymarket": lambda l: None}
    assert run_hot_pass(store, config, HCFG, fetchers, sleeper=lambda s: None) == 1
    assert run_hot_pass(store, config, HCFG, fetchers, sleeper=lambda s: None) == 0
