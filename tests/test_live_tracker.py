"""L4: live booking + confirmation carve-out + headline protection.

The v3.4 rule "an in-play second sighting never confirms" STANDS for
pregame-booked bets; the carve-out is scoped strictly to poll_kind='live'
(dated pre-registration amendment)."""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from arb.config import load
from arb.models import Event, Opportunity, Quote
from arb.report import headline
from arb.store import Store
from arb.tracker import process_poll

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 7, 9, 1, 0, tzinfo=timezone.utc)
STARTED = NOW - timedelta(minutes=40)
VENUES_OK = {"kalshi": {"ok": True, "n": 1, "latency_ms": 1.0},
             "polymarket": {"ok": True, "n": 1, "latency_ms": 1.0},
             "sx": {"ok": True, "n": 1, "latency_ms": 1.0}}


@pytest.fixture
def cfg():
    return load(ROOT / "config.yaml")


def live_xspread(fetched=NOW):
    ev = Event(event_key="baseball_mlb:2026-07-08:mets:royals", category="sports",
               sport="baseball_mlb", home="mets", away="royals",
               start_utc=STARTED, start_grade="nominal")
    legs = []
    for venue, outcome, cost in (("kalshi", "mets", "0.47"),
                                 ("polymarket", "royals", "0.49")):
        c = Decimal(cost)
        legs.append(Quote(event_key=ev.event_key, venue=venue, book=venue,
                          market_type="WINNER", line=None, outcome_label=outcome,
                          side="YES", venue_side="YES", raw_price=c, cost=c,
                          fee_cost=Decimal("0"), depth_usd=Decimal("100"),
                          ladder_json=None, live=True, fetched_at=fetched,
                          category="sports", sport="baseball_mlb",
                          bid=c - Decimal("0.02"), volume_24h=Decimal(5),
                          home="mets", away="royals", start_utc=STARTED,
                          start_grade="nominal"))
    return Opportunity(kind="XSPREAD", category="sports", event=ev, legs=legs,
                       edge=Decimal("0.04"), stakes=[], status="REVIEW",
                       notes="live; cross-venue", live=True)


def test_live_two_poll_book_and_confirm(cfg, tmp_path):
    store = Store(tmp_path / "t.sqlite")
    p1 = store.insert("polls", {"started_at": NOW.isoformat(), "kind": "live",
                                "n_quotes": 2, "n_opps": 1})
    process_poll(cfg, store, p1, "live", [live_xspread()], VENUES_OK, now=NOW)
    bets = store.query("SELECT * FROM flat_bets")
    assert len(bets) == 1
    bet = bets[0]
    assert bet["lane"] == "review"           # classify: live -> review, forever
    assert bet["poll_kind"] == "live"
    assert Decimal(bet["pnl_optimistic"]) == Decimal("0.041667")  # locked at entry

    # in-play second sighting CONFIRMS for live-booked bets (the carve-out)
    t2 = NOW + timedelta(seconds=20)
    p2 = store.insert("polls", {"started_at": t2.isoformat(), "kind": "live",
                                "n_quotes": 2, "n_opps": 1})
    process_poll(cfg, store, p2, "live", [live_xspread(fetched=t2)], VENUES_OK, now=t2)
    conf = store.query(
        "SELECT payload_json FROM bet_events WHERE bet_id=? AND kind='confirmed'",
        (bet["id"],))
    assert len(conf) == 1
    assert json.loads(conf[0]["payload_json"])["live"] is True  # tagged live

    # headline protection: the live bet can NEVER appear in the headline
    hl, n = headline(cfg, store)
    assert (hl, n) == (Decimal(0), 0)
    sql = (ROOT / "scripts" / "headline.sql").read_text()
    row = store.conn.execute(sql).fetchone()
    assert row["n_bets"] == 0                # SQL recompute agrees


def test_pregame_booked_bet_still_never_confirms_in_play(cfg, tmp_path):
    # the v3.4 rule stands for watch-kind bets that drift in-play
    store = Store(tmp_path / "t.sqlite")
    pre_start = NOW + timedelta(minutes=10)  # pregame at booking
    opp = live_xspread()
    opp.live = False
    for leg in opp.legs:
        leg.live = False
        leg.start_utc = pre_start
    opp.event.start_utc = pre_start
    p1 = store.insert("polls", {"started_at": NOW.isoformat(), "kind": "watch",
                                "n_quotes": 2, "n_opps": 1})
    process_poll(cfg, store, p1, "watch", [opp], VENUES_OK, now=NOW)
    bet = store.query("SELECT * FROM flat_bets")[0]
    # second sighting AFTER the start: never confirms; annotation records why
    t2 = pre_start + timedelta(minutes=5)
    opp2 = live_xspread(fetched=t2)
    opp2.event.start_utc = pre_start
    for leg in opp2.legs:
        leg.start_utc = pre_start
    p2 = store.insert("polls", {"started_at": t2.isoformat(), "kind": "watch",
                                "n_quotes": 2, "n_opps": 1})
    process_poll(cfg, store, p2, "watch", [opp2], VENUES_OK, now=t2)
    assert not store.query(
        "SELECT 1 FROM bet_events WHERE bet_id=? AND kind='confirmed'", (bet["id"],))
    notes = store.query(
        "SELECT kind FROM annotations WHERE target_kind='bet' AND target_id=?",
        (str(bet["id"]),))
    assert "in-play-at-confirm" in {r["kind"] for r in notes}
