import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from arb.config import load_config
from arb.models import Event, Opportunity, Quote
from arb.settlement import settle_backfill_mlb, settle_manual, settle_sports
from arb.store import Store
from arb.tracker import record_tracker_poll
from arb.validation import ValidationRule

# Completed game in the recorded scores fixture: KC 5 - TEX 3 on 2026-06-09 (total 8).
SETTLED_EVENT = "baseball_mlb:2026-06-09:kansas city royals:texas rangers"
START = datetime(2026, 6, 9, 23, 41, tzinfo=timezone.utc)


def ev_opp(
    outcome: str,
    cost: str,
    market_type: str = "WINNER",
    line: str | None = None,
    *,
    quality_flag: str | None = None,
    kelly_stake: str | None = None,
    stake: str | None = None,
    event_key_value: str = SETTLED_EVENT,
    start: datetime = START,
) -> Opportunity:
    quote = Quote(
        event_key=event_key_value,
        venue="kalshi",
        book=f"KXMLB-{outcome}-{line}",
        market_type=market_type,  # type: ignore[arg-type]
        line=None if line is None else Decimal(line),
        outcome_label=outcome,
        raw_price=Decimal(cost),
        cost=Decimal(cost),
        fee_cost=Decimal("0.0175"),
        depth_usd=Decimal("1200"),
        live=False,
        fetched_at=datetime.now(timezone.utc),
        start_utc=start,
    )
    event = Event(
        event_key=event_key_value, category="sports", sport="baseball_mlb",
        home="kansas city royals", away="texas rangers", start_utc=start,
    )
    return Opportunity(
        kind="EV", category="sports", event=event, legs=[quote],
        edge=Decimal("0.03"),
        edge_points=Decimal("0.01"),
        stakes={} if stake is None else {outcome: Decimal(stake)},
        status="OK",
        quality_flag=quality_flag,
        kelly_f_star=None if kelly_stake is None else Decimal("0.10"),
        kelly_stake=None if kelly_stake is None else Decimal(kelly_stake),
    )


def place_bets(store: Store, opps) -> None:
    poll = store.start_poll("watch")
    first_start = min(opp.event.start_utc for opp in opps)
    record_tracker_poll(
        store,
        poll,
        opps,
        validation_rule=ValidationRule(min_cost=Decimal("0"), max_cost=Decimal("1"), max_flagged_edge=Decimal("1")),
        now=first_start - timedelta(hours=1),
    )


def settled_rows(store: Store):
    with store.connect() as con:
        return list(con.execute("SELECT * FROM flat_bets WHERE status = 'SETTLED' ORDER BY id"))


def strip_leg_start_times(store: Store) -> None:
    with store.connect() as con:
        con.execute("UPDATE flat_bets SET legs_json = json_remove(legs_json, '$[0].start_utc')")


def test_score_settlement_vectors(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    place_bets(store, [
        ev_opp("kansas city royals", "0.50"),              # effective 0.5175, KC won
        ev_opp("texas rangers", "0.40"),                   # TEX lost
        ev_opp("UNDER", "0.50", "TOTAL", "8.5"),           # total 8 < 8.5, won
        ev_opp("OVER", "0.30", "TOTAL", "8.5"),            # lost
    ])
    config = load_config()
    assert settle_sports(store, config, Path("tests/fixtures")) == 4
    rows = {(r["result"], r["pnl_optimistic"]) for r in settled_rows(store)}
    # SPEC §9 vector: win at effective cost 0.5175 pays (1-0.5175)/0.5175 = $0.93 to the cent.
    assert ("WON", "0.932367") in rows
    assert ("LOST", "-1.000000") in rows
    won = [r for r in settled_rows(store) if r["result"] == "WON"]
    lost = [r for r in settled_rows(store) if r["result"] == "LOST"]
    assert len(won) == 2 and len(lost) == 2
    # First-observation-only bets have no confirmed fill, so confirmed P&L stays NULL ("missed").
    assert all(r["pnl_confirmed"] is None for r in settled_rows(store))


def test_settlement_is_idempotent_and_append_only(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    place_bets(store, [ev_opp("kansas city royals", "0.50")])
    config = load_config()
    assert settle_sports(store, config, Path("tests/fixtures")) == 1
    assert settle_sports(store, config, Path("tests/fixtures")) == 0  # already settled
    with store.connect() as con:
        statuses = [r["status"] for r in con.execute("SELECT status FROM flat_bets ORDER BY id")]
    assert statuses == ["OPEN", "SETTLED"]


def test_settlement_invalidates_pregame_bet_placed_after_start(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    place_bets(store, [ev_opp("kansas city royals", "0.50")])
    with store.connect() as con:
        con.execute("UPDATE flat_bets SET placed_at = ? WHERE status = 'OPEN'", ((START + timedelta(minutes=1)).isoformat(),))

    assert settle_sports(store, load_config(), Path("tests/fixtures")) == 1

    with store.connect() as con:
        rows = list(con.execute("SELECT status, result, quality_flag, notes FROM flat_bets ORDER BY id"))
    assert [row["status"] for row in rows] == ["OPEN", "CLOSED"]
    assert rows[-1]["result"] == "INVALID_IN_PLAY"
    assert rows[-1]["quality_flag"] == "invalid-in-play"
    assert "invalid-in-play" in rows[-1]["notes"]


def test_confirmed_fill_settles_from_second_observation_price(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    place_bets(store, [ev_opp("kansas city royals", "0.50")])
    place_bets(store, [ev_opp("kansas city royals", "0.40")])  # second consecutive poll
    config = load_config()
    assert settle_sports(store, config, Path("tests/fixtures")) == 1
    row = settled_rows(store)[0]
    assert row["pnl_optimistic"] == "0.932367"   # entry at 0.5175 effective
    assert row["pnl_confirmed"] == "1.395210"    # confirmed at 0.4175 effective: (1-c)/c
    assert row["placed_at"] is not None


def test_settlement_records_kelly_pnl_from_base_sizing(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    place_bets(store, [ev_opp("kansas city royals", "0.50", kelly_stake="10.00")])
    place_bets(store, [ev_opp("kansas city royals", "0.40", kelly_stake="20.00")])
    config = load_config()
    assert settle_sports(store, config, Path("tests/fixtures")) == 1
    row = settled_rows(store)[0]
    assert row["pnl_kelly_optimistic"] == "9.323671"
    # Confirmed P&L uses the second-observation cost, but the base row's Kelly size.
    assert row["pnl_kelly_confirmed"] == "13.952096"
    assert row["kelly_stake_usd"] == "10.00"


def test_settlement_uses_recorded_ev_stake_for_micro_bets(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    place_bets(store, [
        ev_opp("kansas city royals", "0.50", quality_flag="micro", stake="5.00"),
        ev_opp("texas rangers", "0.40", quality_flag="micro", stake="5.00"),
    ])
    config = load_config()
    assert settle_sports(store, config, Path("tests/fixtures")) == 2
    rows = {(r["result"], r["pnl_optimistic"], r["stake_usd"]) for r in settled_rows(store)}
    assert ("WON", "4.661836", "5.00") in rows
    assert ("LOST", "-5.000000", "5.00") in rows


def test_settlement_carries_quality_flag_to_scoring_row(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    place_bets(store, [ev_opp("kansas city royals", "0.50", quality_flag="suspect-edge")])
    config = load_config()
    assert settle_sports(store, config, Path("tests/fixtures")) == 1
    row = settled_rows(store)[0]
    assert row["quality_flag"] == "suspect-edge"


def test_manual_settlement(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    place_bets(store, [ev_opp("kansas city royals", "0.50")])
    assert settle_manual(store, SETTLED_EVENT, "kansas city royals") == 1
    assert settled_rows(store)[0]["result"] == "WON"


def test_mlb_backfill_uses_event_key_start_bucket_to_resolve_same_day_pair(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    event_key = "baseball_mlb:2026-06-11:chicago cubs:colorado rockies"
    place_bets(store, [
        ev_opp(
            "OVER",
            "0.50",
            "TOTAL",
            "8.5",
            event_key_value=event_key,
            start=datetime(2026, 6, 11, 19, 10, tzinfo=timezone.utc),
        )
    ])
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    (fixture_dir / "mlbstats_schedule_2026-06-11.json").write_text(json.dumps({
        "dates": [{
            "games": [
                {
                    "gameDate": "2026-06-11T01:00:00Z",
                    "gameNumber": 1,
                    "status": {"abstractGameState": "Final"},
                    "teams": {
                        "away": {"team": {"name": "Chicago Cubs"}, "score": 2},
                        "home": {"team": {"name": "Colorado Rockies"}, "score": 3},
                    },
                },
                {
                    "gameDate": "2026-06-11T19:10:00Z",
                    "gameNumber": 2,
                    "status": {"abstractGameState": "Final"},
                    "teams": {
                        "away": {"team": {"name": "Chicago Cubs"}, "score": 9},
                        "home": {"team": {"name": "Colorado Rockies"}, "score": 3},
                    },
                },
            ]
        }]
    }))

    assert settle_backfill_mlb(store, fixture_dir) == 1
    assert settled_rows(store)[0]["result"] == "WON"


def test_mlb_backfill_accepts_adjacent_utc_date_when_bucket_is_unique(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    event_key = "baseball_mlb:2026-06-11:atlanta braves:chicago white sox:3"
    place_bets(store, [
        ev_opp(
            "chicago white sox",
            "0.50",
            event_key_value=event_key,
            start=datetime(2026, 6, 11, 23, 40, tzinfo=timezone.utc),
        )
    ])
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    (fixture_dir / "mlbstats_schedule_2026-06-10.json").write_text(json.dumps({
        "dates": [{
            "games": [{
                "gameDate": "2026-06-10T23:40:00Z",
                "gameNumber": 1,
                "status": {"abstractGameState": "Final"},
                "teams": {
                    "away": {"team": {"name": "Atlanta Braves"}, "score": 1},
                    "home": {"team": {"name": "Chicago White Sox"}, "score": 2},
                },
            }]
        }]
    }))

    assert settle_backfill_mlb(store, fixture_dir) == 1
    assert settled_rows(store)[0]["result"] == "WON"


def test_mlb_backfill_uses_stored_start_time_to_resolve_doubleheader(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    event_key = "baseball_mlb:2026-06-17:atlanta braves:san francisco giants:3"
    place_bets(store, [
        ev_opp(
            "UNDER",
            "0.50",
            "TOTAL",
            "10.5",
            event_key_value=event_key,
            start=datetime(2026, 6, 17, 23, 15, tzinfo=timezone.utc),
        ),
    ])
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    (fixture_dir / "mlbstats_schedule_2026-06-17.json").write_text(json.dumps({
        "dates": [{
            "games": [
                {
                    "gameDate": "2026-06-17T18:00:00Z",
                    "gameNumber": 1,
                    "status": {"abstractGameState": "Final"},
                    "teams": {
                        "away": {"team": {"name": "San Francisco Giants"}, "score": 7},
                        "home": {"team": {"name": "Atlanta Braves"}, "score": 2},
                    },
                },
                {
                    "gameDate": "2026-06-17T23:15:00Z",
                    "gameNumber": 2,
                    "status": {"abstractGameState": "Final"},
                    "teams": {
                        "away": {"team": {"name": "San Francisco Giants"}, "score": 7},
                        "home": {"team": {"name": "Atlanta Braves"}, "score": 5},
                    },
                },
            ]
        }]
    }))

    assert settle_backfill_mlb(store, fixture_dir) == 1
    rows = settled_rows(store)
    assert rows[0]["result"] == "LOST"
    assert rows[0]["notes"] == "mlbstats-settled"


def test_mlb_backfill_settles_legacy_doubleheader_when_result_is_consistent(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    event_key = "baseball_mlb:2026-06-18:atlanta braves:san francisco giants:3"
    place_bets(store, [
        ev_opp(
            "UNDER",
            "0.50",
            "TOTAL",
            "8.5",
            event_key_value=event_key,
            start=datetime(2026, 6, 18, 23, 15, tzinfo=timezone.utc),
        ),
        ev_opp(
            "san francisco giants",
            "0.50",
            event_key_value=event_key,
            start=datetime(2026, 6, 18, 23, 15, tzinfo=timezone.utc),
        ),
    ])
    strip_leg_start_times(store)
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    (fixture_dir / "mlbstats_schedule_2026-06-17.json").write_text(json.dumps({
        "dates": [{
            "games": [
                {
                    "gameDate": "2026-06-17T18:00:00Z",
                    "gameNumber": 1,
                    "status": {"abstractGameState": "Final"},
                    "teams": {
                        "away": {"team": {"name": "San Francisco Giants"}, "score": 7},
                        "home": {"team": {"name": "Atlanta Braves"}, "score": 2},
                    },
                },
                {
                    "gameDate": "2026-06-17T23:15:00Z",
                    "gameNumber": 2,
                    "status": {"abstractGameState": "Final"},
                    "teams": {
                        "away": {"team": {"name": "San Francisco Giants"}, "score": 7},
                        "home": {"team": {"name": "Atlanta Braves"}, "score": 5},
                    },
                },
            ]
        }]
    }))

    assert settle_backfill_mlb(store, fixture_dir) == 2
    rows = settled_rows(store)
    assert {row["result"] for row in rows} == {"WON", "LOST"}
    assert all(row["notes"] == "mlbstats-settled; doubleheader-consistent-result" for row in rows)


def test_mlb_backfill_leaves_doubleheader_open_when_result_conflicts(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    event_key = "baseball_mlb:2026-06-18:atlanta braves:san francisco giants:3"
    place_bets(store, [
        ev_opp(
            "UNDER",
            "0.50",
            "TOTAL",
            "10.5",
            event_key_value=event_key,
            start=datetime(2026, 6, 18, 23, 15, tzinfo=timezone.utc),
        )
    ])
    strip_leg_start_times(store)
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    (fixture_dir / "mlbstats_schedule_2026-06-17.json").write_text(json.dumps({
        "dates": [{
            "games": [
                {
                    "gameDate": "2026-06-17T18:00:00Z",
                    "gameNumber": 1,
                    "status": {"abstractGameState": "Final"},
                    "teams": {
                        "away": {"team": {"name": "San Francisco Giants"}, "score": 7},
                        "home": {"team": {"name": "Atlanta Braves"}, "score": 2},
                    },
                },
                {
                    "gameDate": "2026-06-17T23:15:00Z",
                    "gameNumber": 2,
                    "status": {"abstractGameState": "Final"},
                    "teams": {
                        "away": {"team": {"name": "San Francisco Giants"}, "score": 7},
                        "home": {"team": {"name": "Atlanta Braves"}, "score": 5},
                    },
                },
            ]
        }]
    }))

    assert settle_backfill_mlb(store, fixture_dir) == 0
    assert settled_rows(store) == []


def test_mlb_backfill_supports_legacy_event_keys_without_bucket(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    event_key = "baseball_mlb:2026-06-20:los angeles dodgers:new york yankees"
    place_bets(store, [
        ev_opp(
            "los angeles dodgers",
            "0.50",
            event_key_value=event_key,
            start=datetime(2026, 6, 20, 23, 10, tzinfo=timezone.utc),
        )
    ])
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    (fixture_dir / "mlbstats_schedule_2026-06-20.json").write_text(json.dumps({
        "dates": [{
            "games": [{
                "gameDate": "2026-06-20T23:10:00Z",
                "gameNumber": 1,
                "status": {"abstractGameState": "Final"},
                "teams": {
                    "away": {"team": {"name": "Los Angeles Dodgers"}, "score": 6},
                    "home": {"team": {"name": "New York Yankees"}, "score": 4},
                },
            }]
        }]
    }))

    assert settle_backfill_mlb(store, fixture_dir) == 1
    assert settled_rows(store)[0]["result"] == "WON"


# --- ARB settlement audit fixes (A1 REVIEW lane, A3 timing guard) -----------------------

def arb_opp(status: str, *, start: datetime = START, venue_b: str = "polymarket") -> Opportunity:
    """Two-leg cross-venue arb on the settled KC/TEX fixture event.

    status="OK" -> trusted=1 (whitelisted pair); status="REVIEW" -> trusted=0 (hard rule #6).
    """
    legs = [
        Quote(
            event_key=SETTLED_EVENT, venue="kalshi", book="KX-KC", market_type="WINNER",
            line=None, outcome_label="kansas city royals", raw_price=Decimal("0.48"),
            cost=Decimal("0.48"), fee_cost=Decimal("0"), depth_usd=Decimal("500"),
            live=False, fetched_at=datetime.now(timezone.utc), start_utc=start,
        ),
        Quote(
            event_key=SETTLED_EVENT, venue=venue_b, book="PM-TEX", market_type="WINNER",
            line=None, outcome_label="texas rangers", raw_price=Decimal("0.49"),
            cost=Decimal("0.49"), fee_cost=Decimal("0"), depth_usd=Decimal("500"),
            live=False, fetched_at=datetime.now(timezone.utc), start_utc=start,
        ),
    ]
    total = sum((leg.effective_cost for leg in legs), Decimal("0"))
    event = Event(
        event_key=SETTLED_EVENT, category="sports", sport="baseball_mlb",
        home="kansas city royals", away="texas rangers", start_utc=start,
    )
    return Opportunity(
        kind="ARB", category="sports", event=event, legs=legs,
        edge=Decimal("1") / total - Decimal("1"), stakes={}, status=status,
    )


def test_review_arb_settles_into_flagged_lane_not_clean_pnl(tmp_path) -> None:
    # A1: an un-whitelisted cross-venue arb (trusted=0) still settles for observability,
    # but is tagged quality_flag='REVIEW' so headline/sizing/capacity all exclude it.
    store = Store(tmp_path / "arb.sqlite")
    place_bets(store, [arb_opp("REVIEW")])
    assert settle_sports(store, load_config(), Path("tests/fixtures")) == 1
    row = settled_rows(store)[0]
    assert row["kind"] == "ARB"
    assert int(row["trusted"]) == 0
    assert row["quality_flag"] == "REVIEW"
    assert row["result"] == "RESOLVED"


def test_trusted_arb_settles_clean_with_no_review_flag(tmp_path) -> None:
    # A1: a whitelisted pair (status OK -> trusted=1) keeps quality_flag NULL and is
    # eligible for the clean/headline P&L lane.
    store = Store(tmp_path / "arb.sqlite")
    place_bets(store, [arb_opp("OK")])
    assert settle_sports(store, load_config(), Path("tests/fixtures")) == 1
    row = settled_rows(store)[0]
    assert int(row["trusted"]) == 1
    assert row["quality_flag"] is None
    assert row["result"] == "RESOLVED"


def test_arb_placed_after_event_start_is_invalidated(tmp_path) -> None:
    # A3: a quote captured at/after the event start is a time artifact, not a lock.
    store = Store(tmp_path / "arb.sqlite")
    place_bets(store, [arb_opp("REVIEW")])
    with store.connect() as con:
        con.execute("UPDATE flat_bets SET placed_at = ? WHERE status = 'OPEN'",
                    ((START + timedelta(minutes=1)).isoformat(),))
    assert settle_sports(store, load_config(), Path("tests/fixtures")) == 1
    with store.connect() as con:
        rows = list(con.execute("SELECT status, result, quality_flag FROM flat_bets ORDER BY id"))
    assert [r["status"] for r in rows] == ["OPEN", "CLOSED"]
    assert rows[-1]["result"] == "INVALID_IN_PLAY"
    assert rows[-1]["quality_flag"] == "invalid-in-play"
    # No RESOLVED lock was booked.
    assert not settled_rows(store)
