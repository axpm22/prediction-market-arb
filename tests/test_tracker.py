from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

from arb.models import Event, Opportunity, Quote
from arb.store import Store
from arb.tracker import record_tracker_poll, tracker_bet_view, write_daily_report


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
        stakes={},
        status="OK",
    )


def test_ev_reappearance_creates_new_linked_row_first_confirmed_null(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    poll1 = store.start_poll("watch")
    record_tracker_poll(store, poll1, [ev_opp("0.50")])
    poll2 = store.start_poll("watch")
    record_tracker_poll(store, poll2, [])  # absent for one completed poll → lifetime closed
    poll3 = store.start_poll("watch")
    record_tracker_poll(store, poll3, [ev_opp("0.49")])

    with store.connect() as con:
        rows = list(con.execute("SELECT * FROM flat_bets ORDER BY id"))
    assert [row["status"] for row in rows] == ["OPEN", "CLOSED", "OPEN"]
    assert rows[0]["pnl_confirmed"] is None  # never observed twice → confirmed fill missed
    assert rows[2]["reappearance_of"] == rows[1]["id"]
    assert "reappeared" in rows[2]["notes"].split(",")


def test_tracker_arb_optimistic_and_confirmed_append_only(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    poll1 = store.start_poll("watch")
    record_tracker_poll(store, poll1, [arb_opp("0.48", "0.48")])
    poll2 = store.start_poll("watch")
    record_tracker_poll(store, poll2, [arb_opp("0.485", "0.485")])

    with store.connect() as con:
        rows = list(con.execute("SELECT * FROM flat_bets ORDER BY id"))
    assert [row["status"] for row in rows] == ["OPEN", "CONFIRMED"]

    view = tracker_bet_view(store)
    assert len(view) == 1
    bet = next(iter(view.values()))
    assert bet["optimistic"] == Decimal("0.04")
    assert bet["confirmed"] == Decimal("0.03")


def test_daily_report_summarizes_base_bets_not_append_rows(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    poll1 = store.start_poll("watch")
    record_tracker_poll(store, poll1, [arb_opp("0.48", "0.48")])
    poll2 = store.start_poll("watch")
    record_tracker_poll(store, poll2, [arb_opp("0.485", "0.485")])

    csv_path, md_path = write_daily_report(store, datetime.now(timezone.utc).date(), tmp_path)

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
    record_tracker_poll(store, poll1, [opp])

    with store.connect() as con:
        [row] = list(con.execute("SELECT notes, quality_flag, kelly_stake_usd FROM flat_bets"))
    notes = row["notes"].split(",")
    assert "first-observed" in notes
    assert "segment-blocked" in notes
    assert row["quality_flag"] == "segment-blocked"
    assert row["kelly_stake_usd"] == "0.00"
