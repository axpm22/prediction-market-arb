from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from arb.cli import main
from arb.config import load_config
from arb.models import Event, Opportunity, Quote
from arb.settlement_health import format_settle_health, settle_health
from arb.store import Store
from arb.tracker import record_tracker_poll


def ev_opp(event_day: str, cost: str = "0.50") -> Opportunity:
    event_key = f"baseball_mlb:{event_day}:kansas city royals:texas rangers"
    event = Event(
        event_key=event_key,
        category="sports",
        sport="baseball_mlb",
        home="kansas city royals",
        away="texas rangers",
        start_utc=datetime.fromisoformat(f"{event_day}T23:40:00+00:00"),
    )
    quote = Quote(
        event_key=event_key,
        venue="kalshi",
        book=f"KXMLB-{event_day}",
        market_type="WINNER",
        line=None,
        outcome_label="kansas city royals",
        raw_price=Decimal(cost),
        cost=Decimal(cost),
        fee_cost=Decimal("0"),
        depth_usd=Decimal("500"),
        live=False,
        fetched_at=datetime.now(timezone.utc),
        start_utc=event.start_utc,
    )
    return Opportunity(
        kind="EV",
        category="sports",
        event=event,
        legs=[quote],
        edge=Decimal("0.01"),
        stakes={},
        status="OK",
    )


def test_settle_health_counts_overdue_after_grace_and_dedupes_confirmed(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    opp = ev_opp("2026-06-21")
    record_tracker_poll(store, store.start_poll("watch"), [opp])
    record_tracker_poll(store, store.start_poll("watch"), [opp])
    now = datetime(2026, 6, 22, 7, 0, tzinfo=ZoneInfo("America/New_York"))

    health = settle_health(store, "America/New_York", grace_hours=6, now=now)

    assert health.open_count == 1
    assert health.overdue_count == 1
    assert health.oldest_overdue_event_day is not None
    assert health.oldest_overdue_event_day.isoformat() == "2026-06-21"
    text = format_settle_health(health)
    assert "open bets: 1" in text
    assert "OVERDUE" in text
    assert "oldest overdue event day: 2026-06-21" in text


def test_settle_health_grace_window_avoids_midnight_false_alarm(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    record_tracker_poll(store, store.start_poll("watch"), [ev_opp("2026-06-21")])
    now = datetime(2026, 6, 22, 5, 30, tzinfo=ZoneInfo("America/New_York"))

    health = settle_health(store, "America/New_York", grace_hours=6, now=now)

    assert health.open_count == 1
    assert health.overdue_count == 0


def test_settle_health_ignores_settled_fingerprints(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    opp = ev_opp("2026-06-20")
    record_tracker_poll(store, store.start_poll("watch"), [opp])
    with store.connect() as con:
        base = con.execute("SELECT * FROM flat_bets WHERE status = 'OPEN'").fetchone()
        con.execute(
            """INSERT INTO flat_bets(fingerprint, reappearance_of, opened_poll_id, category, sport, kind,
                   trusted, live, edge, stake_usd, legs_json, status, placed_at, settled_at, result, notes)
               VALUES (?, ?, ?, 'sports', 'baseball_mlb', 'EV', 1, 0, '0.01', '1.00', ?,
                       'SETTLED', ?, ?, 'WON', 'test-settled')""",
            (
                base["fingerprint"],
                base["id"],
                base["opened_poll_id"],
                base["legs_json"],
                base["placed_at"],
                "2026-06-21T12:00:00+00:00",
            ),
        )
    now = datetime(2026, 6, 22, 12, 0, tzinfo=ZoneInfo("America/New_York"))

    health = settle_health(store, "America/New_York", grace_hours=6, now=now)

    assert health.open_count == 0
    assert health.overdue_count == 0
    assert health.last_settlement_timestamp == "2026-06-21T12:00:00+00:00"


def test_settle_health_cli_exit_code_alarms_on_overdue(tmp_path, monkeypatch, capsys) -> None:
    config = replace(load_config(), database_path=tmp_path / "arb.sqlite", settle_health_overdue_max=0)
    store = Store(config.database_path)
    record_tracker_poll(store, store.start_poll("watch"), [ev_opp("2026-06-20")])
    monkeypatch.setattr("arb.cli.load_config", lambda: config)

    code = main(["settle-health"])

    captured = capsys.readouterr()
    assert code == 1
    assert "open bets: 1" in captured.out
    assert "OVERDUE" in captured.out
