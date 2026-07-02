"""SKIP_DEAD_SPORTS: venue-derived activity gating — a sport with no near-future venue
fixture is dormant (skipped by detection and Odds API spend) and its fingerprints are
exempt from absent-closing that poll."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from arb.activity import active_sports
from arb.store import Store
from arb.tracker import record_tracker_poll

from tests.test_tracker import NOW, arb_opp, quote

MLB_TOMORROW = replace(
    quote("los angeles dodgers", "0.48"),
    event_key="baseball_mlb:2026-06-21:los angeles dodgers:new york yankees",
    start_utc=NOW + timedelta(hours=20),
)
NBA_FAR_FUTURE = replace(
    quote("boston celtics", "0.50"),
    event_key="basketball_nba:2026-10-20:boston celtics:new york knicks",
    start_utc=NOW + timedelta(days=110),
)


def test_active_iff_venue_fixture_within_lookahead() -> None:
    # MLB game tomorrow + NBA only a futures listing months out -> NBA dormant.
    assert active_sports([MLB_TOMORROW, NBA_FAR_FUTURE], NOW, 48) == {"baseball_mlb"}
    # NBA markets reappear with a near start -> reactivates the very next poll.
    nba_back = replace(NBA_FAR_FUTURE, start_utc=NOW + timedelta(hours=10))
    assert active_sports([MLB_TOMORROW, nba_back], NOW, 48) == {"baseball_mlb", "basketball_nba"}
    # Unknown or already-past starts never make a sport active.
    assert active_sports([replace(MLB_TOMORROW, start_utc=None),
                          replace(MLB_TOMORROW, start_utc=NOW - timedelta(hours=1))], NOW, 48) == set()


def test_dormant_skip_poll_closes_zero_fingerprints(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    record_tracker_poll(store, store.start_poll("watch"), [arb_opp("0.48", "0.48")], now=NOW)
    # Next poll MLB is dormant-skipped: its absence proves nothing -> no CLOSE row.
    record_tracker_poll(store, store.start_poll("watch"), [], now=NOW,
                        dormant_sports={"baseball_mlb"})
    with store.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM flat_bets WHERE kind='CLOSE'").fetchone()[0] == 0
    # Sport active again and still absent -> the close proceeds as before.
    record_tracker_poll(store, store.start_poll("watch"), [], now=NOW)
    with store.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM flat_bets WHERE kind='CLOSE'").fetchone()[0] == 1


def test_scan_gate_reads_activity_from_stored_quotes(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    now = datetime(2026, 6, 20, 20, 0, tzinfo=timezone.utc)
    poll = store.start_poll("watch")
    store.save_poll_quotes(poll, [replace(MLB_TOMORROW, fetched_at=now),
                                  replace(NBA_FAR_FUTURE, fetched_at=now)])
    assert store.sports_with_upcoming(now, 48) == {"baseball_mlb"}


def test_tournament_tag_stripped_from_names() -> None:
    # "Wimbledon ATP: Rafael Jodar" must normalize to the bare player name or tennis
    # event_keys never join across venues / the Odds API consensus.
    from arb.matching import event_key, normalize_name

    assert normalize_name("Wimbledon ATP: Rafael Jodar", "tennis", {}) == "rafael jodar"
    assert normalize_name("Pablo Carreno Busta", "tennis", {}) == "pablo carreno busta"
    start = datetime(2026, 7, 2, 15, 0, tzinfo=timezone.utc)
    tagged = event_key("tennis", "Wimbledon ATP: Rafael Jodar", "Pablo Carreno Busta", start, {})
    clean = event_key("tennis", "Rafael Jodar", "Pablo Carreno Busta", start, {})
    assert tagged == clean
