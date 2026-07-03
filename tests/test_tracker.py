from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

from arb.models import Event, Opportunity, Quote
from arb.store import Store
from arb.tracker import record_tracker_poll, tracker_bet_view, write_daily_report

NOW = datetime(2026, 6, 20, 20, 0, tzinfo=timezone.utc)


def arb_opp(cost_a: str, cost_b: str) -> Opportunity:
    event = Event(
        event_key="baseball_mlb:2026-06-20:los angeles dodgers:new york yankees",
        category="sports",
        sport="baseball_mlb",
        home="los angeles dodgers",
        away="new york yankees",
        start_utc=datetime(2026, 6, 20, 23, 5, tzinfo=timezone.utc),
    )
    legs = [
        quote("los angeles dodgers", cost_a),
        quote("new york yankees", cost_b),
    ]
    total = sum((leg.effective_cost for leg in legs), Decimal("0"))
    return Opportunity(
        kind="ARB",
        category="sports",
        event=event,
        legs=legs,
        edge=Decimal("1") / total - Decimal("1"),
        stakes={},
        status="OK",
    )


def quote(outcome: str, cost: str) -> Quote:
    return Quote(
        event_key="baseball_mlb:2026-06-20:los angeles dodgers:new york yankees",
        venue="kalshi",
        book=f"KXMLB-{outcome}",
        market_type="WINNER",
        line=None,
        outcome_label=outcome,
        raw_price=Decimal(cost),
        cost=Decimal(cost),
        fee_cost=Decimal("0"),
        depth_usd=Decimal("100"),
        live=False,
        fetched_at=datetime.now(timezone.utc),
    )


def ev_opp(cost: str) -> Opportunity:
    event = Event(
        event_key="baseball_mlb:2026-06-20:los angeles dodgers:new york yankees",
        category="sports",
        sport="baseball_mlb",
        home="los angeles dodgers",
        away="new york yankees",
        start_utc=datetime(2026, 6, 20, 23, 5, tzinfo=timezone.utc),
    )
    return Opportunity(
        kind="EV",
        category="sports",
        event=event,
        legs=[quote("los angeles dodgers", cost)],
        edge=Decimal("0.03"),
        edge_points=Decimal("0.01"),
        stakes={},
        status="OK",
    )


def test_ev_reappearance_creates_new_linked_row_first_confirmed_null(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    poll1 = store.start_poll("watch")
    record_tracker_poll(store, poll1, [ev_opp("0.50")], now=NOW)
    poll2 = store.start_poll("watch")
    record_tracker_poll(store, poll2, [], now=NOW)  # absent for one completed poll → lifetime closed
    poll3 = store.start_poll("watch")
    record_tracker_poll(store, poll3, [ev_opp("0.49")], now=NOW)

    with store.connect() as con:
        rows = list(con.execute("SELECT * FROM flat_bets ORDER BY id"))
    assert [row["status"] for row in rows] == ["OPEN", "CLOSED", "OPEN"]
    assert rows[0]["pnl_confirmed"] is None  # never observed twice → confirmed fill missed
    assert rows[2]["reappearance_of"] == rows[1]["id"]
    assert "reappeared" in rows[2]["notes"].split(",")


def test_tracker_arb_optimistic_and_confirmed_append_only(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    poll1 = store.start_poll("watch")
    record_tracker_poll(store, poll1, [arb_opp("0.48", "0.48")], now=NOW)
    poll2 = store.start_poll("watch")
    record_tracker_poll(store, poll2, [arb_opp("0.485", "0.485")], now=NOW)

    with store.connect() as con:
        rows = list(con.execute("SELECT * FROM flat_bets ORDER BY id"))
    assert [row["status"] for row in rows] == ["OPEN", "CONFIRMED"]

    view = tracker_bet_view(store)
    assert len(view) == 1
    bet = next(iter(view.values()))
    assert bet["optimistic"] == Decimal("0.041667")  # K10: stored at q6, not cents
    assert bet["confirmed"] == Decimal("0.030928")


def test_daily_report_summarizes_base_bets_not_append_rows(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    poll1 = store.start_poll("watch")
    record_tracker_poll(store, poll1, [arb_opp("0.48", "0.48")], now=NOW)
    poll2 = store.start_poll("watch")
    record_tracker_poll(store, poll2, [arb_opp("0.485", "0.485")], now=NOW)

    csv_path, md_path = write_daily_report(store, NOW.date(), tmp_path)

    # One base bet was placed; the CONFIRMED append row should enrich it, not count as
    # a second daily bet.
    assert len(csv_path.read_text().strip().splitlines()) == 2
    md = md_path.read_text()
    assert "## Class x Fill x Trust" in md
    assert "## Kelly" in md
    assert "| ARB | optimistic | trusted | 1 |" in md
    assert "| ARB | confirmed | trusted | 1 |" in md


def test_tracker_persists_opportunity_notes_with_lifecycle_note(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    poll1 = store.start_poll("watch")
    opp = replace(
        ev_opp("0.50"),
        notes=("segment-blocked",),
        quality_flag="segment-blocked",
        kelly_stake=Decimal("0.00"),
    )
    record_tracker_poll(store, poll1, [opp], now=NOW)

    with store.connect() as con:
        [row] = list(con.execute("SELECT notes, quality_flag, kelly_stake_usd FROM flat_bets"))
    notes = row["notes"].split(",")
    assert "first-observed" in notes
    assert "segment-blocked" in notes
    assert row["quality_flag"] == "segment-blocked"
    assert row["kelly_stake_usd"] == "0.00"


def test_tracker_records_ev_stake_from_opportunity_sizing(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    poll1 = store.start_poll("watch")
    opp = replace(
        ev_opp("0.50"),
        stakes={"los angeles dodgers": Decimal("7.50")},
        quality_flag="micro",
    )
    record_tracker_poll(store, poll1, [opp], now=NOW)

    with store.connect() as con:
        [row] = list(con.execute("SELECT stake_usd, quality_flag FROM flat_bets"))
    assert row["stake_usd"] == "7.50"
    assert row["quality_flag"] == "micro"


def test_tracker_rejects_started_pregame_opportunity(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    record_tracker_poll(store, store.start_poll("watch"), [ev_opp("0.50")], now=datetime(2026, 6, 21, 0, 0, tzinfo=timezone.utc))

    with store.connect() as con:
        [row] = list(con.execute("SELECT status, stake_usd, quality_flag, notes FROM flat_bets"))
    assert row["status"] == "CLOSED"
    assert row["stake_usd"] == "0"
    assert row["quality_flag"] == "in-play-skipped"
    assert "skip-started-event" in row["notes"]


# Event start in ev_opp is 2026-06-20 23:05; NOW (20:00) is BEFORE it. OVER is well after the
# event start + the default 180-min mute grace, so the event has genuinely ended by then.
OVER = datetime(2026, 6, 21, 3, 0, tzinfo=timezone.utc)


def _insert_terminal(con, base, status, result="WON"):
    con.execute(
        """INSERT INTO flat_bets(fingerprint, reappearance_of, opened_poll_id, category, sport, kind,
               trusted, live, edge, stake_usd, legs_json, status, placed_at, settled_at, result, notes)
           VALUES (?, ?, ?, 'sports', 'baseball_mlb', 'EV', 1, 0, '0.01', '1.00', ?, ?, ?, ?, ?, 'test')""",
        (base["fingerprint"], base["id"], base["opened_poll_id"], base["legs_json"], status,
         base["placed_at"], NOW.isoformat(), result),
    )


def _open_then_close(store, opp):
    """Track an opp so it OPENs, then let it go absent so a CLOSED lifecycle row exists."""
    record_tracker_poll(store, store.start_poll("watch"), [opp], now=NOW)
    record_tracker_poll(store, store.start_poll("watch"), [], now=NOW)  # absent → CLOSED
    with store.connect() as con:
        return con.execute("SELECT * FROM flat_bets WHERE status='OPEN'").fetchone()


def test_settled_future_event_reopens_as_new_bet(tmp_path) -> None:
    # THE BUG: a SETTLED row for a still-future event is premature/own-settlement, so a later
    # re-sighting must re-open via the reappearance path — NOT be terminal-fingerprint-skipped.
    store = Store(tmp_path / "arb.sqlite")
    opp = ev_opp("0.50")
    base = _open_then_close(store, opp)
    with store.connect() as con:
        _insert_terminal(con, base, "SETTLED")
    record_tracker_poll(store, store.start_poll("watch"), [opp], now=NOW)
    with store.connect() as con:
        last = con.execute("SELECT * FROM flat_bets ORDER BY id DESC LIMIT 1").fetchone()
    assert last["status"] == "OPEN" and last["reappearance_of"] is not None
    assert last["quality_flag"] is None
    assert "terminal-fingerprint-skipped" not in (last["notes"] or "")


def test_exited_fingerprint_reopens_and_never_mutes(tmp_path) -> None:
    # EXITED (an ev-exit take-profit/stop mid-life) must reappear as a new bet, never be muted.
    store = Store(tmp_path / "arb.sqlite")
    opp = ev_opp("0.50")
    record_tracker_poll(store, store.start_poll("watch"), [opp], now=NOW)
    with store.connect() as con:
        base = con.execute("SELECT * FROM flat_bets WHERE status='OPEN'").fetchone()
        _insert_terminal(con, base, "EXITED", result="SOLD")
    record_tracker_poll(store, store.start_poll("watch"), [opp], now=NOW)
    with store.connect() as con:
        last = con.execute("SELECT * FROM flat_bets ORDER BY id DESC LIMIT 1").fetchone()
    assert last["status"] == "OPEN" and last["reappearance_of"] is not None
    assert "terminal-fingerprint-skipped" not in (last["notes"] or "")


def test_settled_fingerprint_still_muted_once_event_is_over(tmp_path) -> None:
    # Valid case preserved: once the event has genuinely ended, a SETTLED fingerprint's
    # re-sighting IS skipped (no resurrecting a finished market).
    store = Store(tmp_path / "arb.sqlite")
    opp = ev_opp("0.50")
    record_tracker_poll(store, store.start_poll("watch"), [opp], now=NOW)
    with store.connect() as con:
        base = con.execute("SELECT * FROM flat_bets WHERE status='OPEN'").fetchone()
        _insert_terminal(con, base, "SETTLED")
    record_tracker_poll(store, store.start_poll("watch"), [opp], now=OVER)
    with store.connect() as con:
        last = con.execute("SELECT status, quality_flag, notes FROM flat_bets ORDER BY id DESC LIMIT 1").fetchone()
    assert last["status"] == "CLOSED" and last["quality_flag"] == "terminal-fingerprint-skipped"
    assert "skip-terminal-fingerprint:SETTLED" in last["notes"]


def test_active_position_is_not_double_opened(tmp_path) -> None:
    # Weakness #4 preserved: a currently-active OPEN position re-sighted is not opened twice.
    store = Store(tmp_path / "arb.sqlite")
    opp = ev_opp("0.50")
    record_tracker_poll(store, store.start_poll("watch"), [opp], now=NOW)
    record_tracker_poll(store, store.start_poll("watch"), [opp], now=NOW)  # re-sighted, still active
    with store.connect() as con:
        opens = con.execute("SELECT COUNT(*) n FROM flat_bets WHERE status='OPEN' AND fingerprint=?",
                            (opp.fingerprint,)).fetchone()["n"]
    assert opens == 1  # the re-sighting confirmed/linked, it did not create a second OPEN


def test_tracker_blocks_duplicate_position_same_poll(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    base = ev_opp("0.50")
    alt_leg = replace(base.legs[0], venue="polymarket", book="poly-los-angeles-dodgers")
    duplicate_venue = replace(base, legs=[alt_leg], fingerprint="")

    record_tracker_poll(store, store.start_poll("watch"), [base, duplicate_venue], now=NOW)

    with store.connect() as con:
        rows = list(con.execute("SELECT status, quality_flag, notes FROM flat_bets ORDER BY id"))
    assert [row["status"] for row in rows] == ["OPEN", "CLOSED"]
    assert rows[-1]["quality_flag"] == "duplicate-position-skipped"
    assert "skip-duplicate-position" in rows[-1]["notes"]


def test_absent_close_only_touches_owning_cadence(tmp_path) -> None:
    # Interleaving regression: cadences (watch/focus/ev-exit) poll on their own schedules. A
    # fingerprint opened by ONE cadence must not be absent-closed by ANOTHER cadence's poll that
    # never emits it — that cross-cadence close/reopen churn is what duplicated the ledger. Only
    # the owning cadence's own absent poll closes it.
    store = Store(tmp_path / "arb.sqlite")
    opp = ev_opp("0.50")
    record_tracker_poll(store, store.start_poll("focus"), [opp], now=NOW)  # owned by focus
    record_tracker_poll(store, store.start_poll("watch"), [], now=NOW)     # watch poll: opp absent
    with store.connect() as con:
        statuses = [r["status"] for r in con.execute(
            "SELECT status FROM flat_bets WHERE fingerprint=? ORDER BY id", (opp.fingerprint,))]
    assert statuses == ["OPEN"], "a watch poll must not close a focus-owned fingerprint"

    record_tracker_poll(store, store.start_poll("focus"), [], now=NOW)     # focus poll: opp absent
    with store.connect() as con:
        last = con.execute(
            "SELECT status FROM flat_bets WHERE fingerprint=? ORDER BY id DESC LIMIT 1",
            (opp.fingerprint,)).fetchone()
    assert last["status"] == "CLOSED", "the owning cadence's own absent poll still closes it"
