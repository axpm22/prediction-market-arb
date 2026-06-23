from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from arb.models import Event, Opportunity, Quote
from arb.store import Store
from arb.tracker import record_tracker_poll, write_daily_report
from arb.validation import DROP, KEEP, PENDING_CONFIRM, ValidationRule, classify_validation


EVENT = Event(
    event_key="baseball_mlb:2026-06-20:los angeles dodgers:new york yankees",
    category="sports",
    sport="baseball_mlb",
    home="los angeles dodgers",
    away="new york yankees",
    start_utc=datetime(2026, 6, 20, 23, 5, tzinfo=timezone.utc),
)


def ev_opp(cost: str, edge: str = "0.010000") -> Opportunity:
    quote = Quote(
        event_key=EVENT.event_key,
        venue="kalshi",
        book="KXMLB-DODGERS",
        market_type="WINNER",
        line=None,
        outcome_label="los angeles dodgers",
        raw_price=Decimal(cost),
        cost=Decimal(cost),
        fee_cost=Decimal("0"),
        depth_usd=Decimal("500"),
        live=False,
        fetched_at=datetime.now(timezone.utc),
    )
    return Opportunity(
        kind="EV",
        category="sports",
        event=EVENT,
        legs=[quote],
        edge=Decimal(edge),
        edge_points=Decimal(edge),
        stakes={},
        status="OK",
    )


def test_validation_rule_pending_keep_and_drop_vectors() -> None:
    rule = ValidationRule()
    rule_id, tag, notes = classify_validation(ev_opp("0.50"), "OPEN", rule)
    assert rule_id == "H-EVFILTER"
    assert tag == PENDING_CONFIRM
    assert notes == ("validation-pending-confirm",)

    _rule_id, tag, notes = classify_validation(ev_opp("0.50"), "CONFIRMED", rule)
    assert tag == KEEP
    assert notes == ("validation-keep",)

    _rule_id, tag, notes = classify_validation(ev_opp("0.35"), "CONFIRMED", rule)
    assert tag == DROP
    assert "validation-drop:cost-band" in notes

    _rule_id, tag, notes = classify_validation(ev_opp("0.50", edge="0.020000"), "CONFIRMED", rule)
    assert tag == DROP
    assert "validation-drop:flagged-edge" in notes


def test_tracker_tags_validation_keep_on_second_observation(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    now = EVENT.start_utc - timedelta(hours=1)
    record_tracker_poll(store, store.start_poll("watch"), [ev_opp("0.50")], now=now)
    record_tracker_poll(store, store.start_poll("watch"), [ev_opp("0.50")], now=now)

    with store.connect() as con:
        rows = list(con.execute("SELECT status, validation_rule, validation_tag, notes FROM flat_bets ORDER BY id"))

    assert [(row["status"], row["validation_tag"]) for row in rows] == [
        ("OPEN", PENDING_CONFIRM),
        ("CONFIRMED", KEEP),
    ]
    assert rows[0]["validation_rule"] == "H-EVFILTER"
    assert "validation-pending-confirm" in rows[0]["notes"]
    assert "validation-keep" in rows[1]["notes"]


def test_daily_report_surfaces_validation_forward_test(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    now = EVENT.start_utc - timedelta(hours=1)
    record_tracker_poll(store, store.start_poll("watch"), [ev_opp("0.50")], now=now)
    record_tracker_poll(store, store.start_poll("watch"), [ev_opp("0.50")], now=now)

    _csv_path, md_path = write_daily_report(store, now.date(), tmp_path)
    md = md_path.read_text()

    assert "### Validation Forward Test" in md
    assert "| H-EVFILTER | KEEP | 1 | 0 | $0.00 | 1 |" in md


def test_settled_rows_copy_final_validation_tag(tmp_path) -> None:
    from arb.settlement import settle_manual

    store = Store(tmp_path / "arb.sqlite")
    opp = replace(ev_opp("0.50"), event=replace(EVENT, event_key="manual:event"), legs=[
        replace(ev_opp("0.50").legs[0], event_key="manual:event")
    ])
    now = EVENT.start_utc - timedelta(hours=1)
    record_tracker_poll(store, store.start_poll("watch"), [opp], now=now)
    record_tracker_poll(store, store.start_poll("watch"), [opp], now=now)

    assert settle_manual(store, "manual:event", "los angeles dodgers") == 1
    with store.connect() as con:
        [settled] = list(con.execute("SELECT validation_rule, validation_tag FROM flat_bets WHERE status='SETTLED'"))

    assert settled["validation_rule"] == "H-EVFILTER"
    assert settled["validation_tag"] == KEEP
