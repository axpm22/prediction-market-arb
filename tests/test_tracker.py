"""§10.7 tracker vector + ledger discipline (SPEC §10). Asserted to the cent."""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from arb.config import load
from arb.matching import MatchedGroup
from arb.models import Event, Opportunity, Quote
from arb.store import Store
from arb.tracker import process_poll
from arb.venues.fees import leg_fee_cost

NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
START = NOW + timedelta(hours=6)
VENUE_OK = {"kalshi": {"ok": True, "n": 1, "latency_ms": 1.0},
            "polymarket": {"ok": True, "n": 1, "latency_ms": 1.0},
            "sx": {"ok": True, "n": 1, "latency_ms": 1.0}}


@pytest.fixture
def cfg():
    c = load(Path(__file__).resolve().parent.parent / "config.yaml")
    c["ev_enabled"] = True  # the freeze is a production default, not a removed code path
    return c


def q(venue, outcome, cost, fee="0", side="YES", fetched=NOW):
    c = Decimal(cost)
    return Quote(event_key="baseball_mlb:2026-07-08:mets:royals", venue=venue, book=venue,
                 market_type="WINNER", line=None, outcome_label=outcome, side=side,
                 raw_price=c, cost=c, fee_cost=Decimal(fee), depth_usd=Decimal("100"),
                 ladder_json=None, live=False, fetched_at=fetched, category="sports",
                 sport="baseball_mlb", bid=c - Decimal("0.02"), volume_24h=Decimal(9),
                 home="mets", away="royals", start_utc=START, start_grade="verified")


def ev_event():
    return Event(event_key="baseball_mlb:2026-07-08:mets:royals", category="sports",
                 sport="baseball_mlb", home="mets", away="royals",
                 start_utc=START, start_grade="verified")


def arb_opp(cfg, s1, s2, fetched=NOW):
    legs = [q("kalshi", "mets", s1, fetched=fetched),
            q("kalshi", "royals", s2, fetched=fetched)]
    s = Decimal(s1) + Decimal(s2)
    return Opportunity(kind="ARB", category="sports", event=ev_event(), legs=legs,
                       edge=1 - s, stakes=[], status="OK", notes="")


def ev_opp(cfg, ask, fetched=NOW):
    fee = leg_fee_cost(cfg, "kalshi", "sports", Decimal(ask))
    leg = q("kalshi", "mets", ask, fee=str(fee), fetched=fetched)
    c = Decimal(ask) + fee
    return Opportunity(kind="EV", category="sports", event=ev_event(), legs=[leg],
                       edge=Decimal("0.55") - c, stakes=[], status="OK",
                       notes="p_hat=0.55")


def test_tracker_vector_10_7(cfg, tmp_path):
    store = Store(tmp_path / "t.sqlite")
    # poll1: ARB (S=0.96) + EV (ask 0.50)
    p1 = store.insert("polls", {"started_at": NOW.isoformat(), "kind": "watch",
                                "n_quotes": 4, "n_opps": 2})
    process_poll(cfg, store, p1, "watch",
                 [arb_opp(cfg, "0.47", "0.49"), ev_opp(cfg, "0.50")], VENUE_OK, now=NOW)
    bets = store.query("SELECT * FROM flat_bets ORDER BY id")
    assert len(bets) == 2
    arb = [b for b in bets if b["kind"] == "ARB"][0]
    # optimistic pnl = 1/0.96 - 1 = 0.041667, locked at entry
    assert Decimal(arb["pnl_optimistic"]) == Decimal("0.041667")
    assert arb["lane"] == "review"  # 4.17% ROI trips arb_stale_warn — deliberately (§2)
    # ARB settles at entry: one settled bet_event, exempt from the tripwire
    ev_rows = store.query("SELECT kind FROM bet_events WHERE bet_id=?", (arb["id"],))
    assert "settled" in {r["kind"] for r in ev_rows}

    # poll2: ARB persists @0.97 -> confirmed; EV gone -> lifetime closed, missed
    t2 = NOW + timedelta(seconds=60)
    p2 = store.insert("polls", {"started_at": t2.isoformat(), "kind": "watch",
                                "n_quotes": 2, "n_opps": 1})
    process_poll(cfg, store, p2, "watch",
                 [arb_opp(cfg, "0.475", "0.495", fetched=t2)], VENUE_OK, now=t2)
    conf = store.query(
        "SELECT payload_json FROM bet_events WHERE bet_id=? AND kind='confirmed'",
        (arb["id"],))
    assert len(conf) == 1
    payload = json.loads(conf[0]["payload_json"])
    assert Decimal(payload["cost_sum"]) == Decimal("0.97")
    ev_bet = [b for b in bets if b["kind"] == "EV"][0]
    closed = store.query(
        "SELECT kind FROM bet_events WHERE bet_id=? AND kind='lifetime_closed'",
        (ev_bet["id"],))
    # SPEC §10 amended 2026-07-29: closure needs dead_checks CONSECUTIVE misses,
    # not one. One absence records a sighting_missed and nothing else.
    assert not closed, "one absence must not close a lifetime (amended rule)"
    assert store.query(
        "SELECT 1 FROM bet_events WHERE bet_id=? AND kind='sighting_missed'",
        (ev_bet["id"],)), "the absence must still be recorded"
    for k in range(2, int(cfg["tracker"]["dead_checks"]) + 1):
        tk = NOW + timedelta(seconds=60 * k)
        pk = store.insert("polls", {"started_at": tk.isoformat(), "kind": "watch",
                                    "n_quotes": 2, "n_opps": 1})
        process_poll(cfg, store, pk, "watch",
                     [arb_opp(cfg, "0.475", "0.495", fetched=tk)], VENUE_OK, now=tk)
    closed = store.query(
        "SELECT kind FROM bet_events WHERE bet_id=? AND kind='lifetime_closed'",
        (ev_bet["id"],))
    assert closed, "EV bet absent dead_checks consecutive polls must lifetime-close"

    # poll3: EV reappears @0.49 -> NEW row with reappearance_of
    t3 = NOW + timedelta(seconds=120)
    p3 = store.insert("polls", {"started_at": t3.isoformat(), "kind": "watch",
                                "n_quotes": 2, "n_opps": 2})
    process_poll(cfg, store, p3, "watch",
                 [arb_opp(cfg, "0.475", "0.495", fetched=t3), ev_opp(cfg, "0.49", fetched=t3)],
                 VENUE_OK, now=t3)
    evs = store.query("SELECT * FROM flat_bets WHERE kind='EV' ORDER BY id")
    assert len(evs) == 2
    assert evs[1]["reappearance_of"] == evs[0]["id"]

    # settle YES (mets win) -> EV row 1 optimistic pnl = (1-0.5175)/0.5175
    from arb.settlement import settle_manual
    settle_manual(cfg, store, "baseball_mlb:2026-07-08:mets:royals", "mets",
                  now=START + timedelta(hours=4))
    from arb.metrics import bet_pnl_optimistic
    pnl = bet_pnl_optimistic(store, dict(evs[0]))
    assert pnl == Decimal("0.932367")


def test_ev_frozen_books_nothing_writes_left_on_table(tmp_path):
    frozen = load(Path(__file__).resolve().parent.parent / "config.yaml")
    assert frozen["ev_enabled"] is False
    store = Store(tmp_path / "t.sqlite")
    p1 = store.insert("polls", {"started_at": NOW.isoformat(), "kind": "watch",
                                "n_quotes": 1, "n_opps": 1})
    process_poll(frozen, store, p1, "watch", [ev_opp(frozen, "0.50")], VENUE_OK, now=NOW)
    assert store.query("SELECT COUNT(*) c FROM flat_bets")[0]["c"] == 0
    lot = store.query("SELECT reason FROM left_on_table")
    assert [r["reason"] for r in lot] == ["ev-frozen"]


def test_position_dedup_absorbs_and_records(cfg, tmp_path):
    store = Store(tmp_path / "t.sqlite")
    p1 = store.insert("polls", {"started_at": NOW.isoformat(), "kind": "watch",
                                "n_quotes": 2, "n_opps": 1})
    process_poll(cfg, store, p1, "watch", [arb_opp(cfg, "0.47", "0.49")], VENUE_OK, now=NOW)
    # same economic position re-quoted cross-venue: absorbed, never a second bet
    legs = [q("kalshi", "mets", "0.46"), q("polymarket", "royals", "0.50")]
    xs = Opportunity(kind="XSPREAD", category="sports", event=ev_event(), legs=legs,
                     edge=Decimal("0.04"), stakes=[], status="REVIEW", notes="cross-venue")
    t2 = NOW + timedelta(seconds=60)
    p2 = store.insert("polls", {"started_at": t2.isoformat(), "kind": "watch",
                                "n_quotes": 2, "n_opps": 2})
    process_poll(cfg, store, p2, "watch", [arb_opp(cfg, "0.47", "0.49", fetched=t2), xs],
                 VENUE_OK, now=t2)
    assert store.query("SELECT COUNT(*) c FROM flat_bets")[0]["c"] == 1
    lot = store.query("SELECT reason, venue_pair FROM left_on_table")
    assert ("position-dedup", "kalshi:polymarket") in [(r["reason"], r["venue_pair"])
                                                       for r in lot]


def test_venue_outage_never_mass_closes(cfg, tmp_path):
    store = Store(tmp_path / "t.sqlite")
    p1 = store.insert("polls", {"started_at": NOW.isoformat(), "kind": "watch",
                                "n_quotes": 2, "n_opps": 1})
    process_poll(cfg, store, p1, "watch", [arb_opp(cfg, "0.47", "0.49")], VENUE_OK, now=NOW)
    down = dict(VENUE_OK, kalshi={"ok": False, "n": 0, "latency_ms": 0.0})
    t2 = NOW + timedelta(seconds=60)
    p2 = store.insert("polls", {"started_at": t2.isoformat(), "kind": "watch",
                                "n_quotes": 0, "n_opps": 0})
    process_poll(cfg, store, p2, "watch", [], down, now=t2)
    closed = store.query("SELECT * FROM bet_events WHERE kind='lifetime_closed'")
    assert closed == []  # carried, not closed (§10.2)


def test_poll_kind_ownership(cfg, tmp_path):
    store = Store(tmp_path / "t.sqlite")
    p1 = store.insert("polls", {"started_at": NOW.isoformat(), "kind": "watch",
                                "n_quotes": 2, "n_opps": 1})
    process_poll(cfg, store, p1, "watch", [arb_opp(cfg, "0.47", "0.49")], VENUE_OK, now=NOW)
    # a 'verify' poll must not close a 'watch' bet (bug I1: 315 duplicate bets)
    t2 = NOW + timedelta(seconds=60)
    p2 = store.insert("polls", {"started_at": t2.isoformat(), "kind": "verify",
                                "n_quotes": 0, "n_opps": 0})
    process_poll(cfg, store, p2, "verify", [], VENUE_OK, now=t2)
    assert store.query("SELECT * FROM bet_events WHERE kind='lifetime_closed'") == []


def test_reappearance_storm_lots_and_annotates_instead_of_booking(cfg, tmp_path):
    # H6: the (max_reappearances+1)-th sighting of a fingerprint in 24h must NOT
    # book a flat_bets row — it goes to left_on_table (reappearance-storm) and
    # writes the reappearance-storm annotation that invariant 4 looks for.
    from arb.tracker import candidate_from_opp
    store = Store(tmp_path / "storm.sqlite")
    cand = candidate_from_opp(cfg, ev_opp(cfg, "0.50"))
    cap = int(cfg["tracker"]["max_reappearances"])
    for _ in range(cap):  # cap prior sightings within 24h, all closed
        bid = store.insert("flat_bets", {
            "created_at": NOW.isoformat(), "fingerprint": cand["fingerprint"],
            "position_key": cand["position_key"], "kind": "EV",
            "legs_json": json.dumps(cand["legs"]), "stake_usd": "1",
            "status": "OPEN", "event_key": cand["event_key"]})
        store.insert("bet_events", {"bet_id": bid, "kind": "lifetime_closed",
                                    "payload_json": "{}"})
    p = store.insert("polls", {"started_at": NOW.isoformat(), "kind": "watch",
                               "n_quotes": 1, "n_opps": 1})
    process_poll(cfg, store, p, "watch", [ev_opp(cfg, "0.50")], VENUE_OK, now=NOW)
    n = store.query("SELECT COUNT(*) c FROM flat_bets WHERE fingerprint=?",
                    (cand["fingerprint"],))[0]["c"]
    assert n == cap  # no (cap+1)-th row booked
    assert store.query(
        "SELECT 1 FROM left_on_table WHERE reason='reappearance-storm'")
    assert store.query(
        "SELECT 1 FROM annotations WHERE kind='reappearance-storm' AND detail LIKE ?",
        (f"%{cand['fingerprint']}%",))


def test_duplicate_fingerprint_books_once_per_poll(cfg, tmp_path):
    # H10 belt: two candidates with the same fingerprint in ONE poll (overlapping
    # tags) must book a single flat_bets row, not one per duplicate.
    store = Store(tmp_path / "dup.sqlite")
    p = store.insert("polls", {"started_at": NOW.isoformat(), "kind": "watch",
                               "n_quotes": 2, "n_opps": 2})
    opp = ev_opp(cfg, "0.50")
    process_poll(cfg, store, p, "watch", [opp, opp], VENUE_OK, now=NOW)
    assert store.query("SELECT COUNT(*) c FROM flat_bets WHERE kind='EV'")[0]["c"] == 1


def _poll(store, kind, when):
    return store.insert("polls", {"started_at": when.isoformat(), "kind": kind,
                                  "n_quotes": 2, "n_opps": 1})


def _events(store, bet_id, kind):
    return store.query("SELECT * FROM bet_events WHERE bet_id=? AND kind=? ORDER BY id",
                       (bet_id, kind))


def test_one_missed_sighting_does_not_close_the_lifetime(cfg, tmp_path):
    """Measured 2026-07-29: 15 of 103 watch-lane closures (14.6%) re-minted a new
    flat_bets row for the same fingerprint within ~2 polls. One absence is noise,
    not death."""
    store = Store(tmp_path / "t.sqlite")
    opp = arb_opp(cfg, "0.47", "0.49")
    process_poll(cfg, store, _poll(store, "watch", NOW), "watch", [opp], VENUE_OK, now=NOW)
    bet_id = store.query("SELECT id FROM flat_bets")[0]["id"]

    t1 = NOW + timedelta(seconds=60)
    process_poll(cfg, store, _poll(store, "watch", t1), "watch", [], VENUE_OK, now=t1)

    assert _events(store, bet_id, "lifetime_closed") == []
    assert len(_events(store, bet_id, "sighting_missed")) == 1


def test_dead_checks_consecutive_misses_close_the_lifetime(cfg, tmp_path):
    store = Store(tmp_path / "t.sqlite")
    opp = arb_opp(cfg, "0.47", "0.49")
    process_poll(cfg, store, _poll(store, "watch", NOW), "watch", [opp], VENUE_OK, now=NOW)
    bet_id = store.query("SELECT id FROM flat_bets")[0]["id"]

    for i in range(1, int(cfg["tracker"]["dead_checks"]) + 1):
        t = NOW + timedelta(seconds=60 * i)
        process_poll(cfg, store, _poll(store, "watch", t), "watch", [], VENUE_OK, now=t)

    closed = _events(store, bet_id, "lifetime_closed")
    assert len(closed) == 1
    assert json.loads(closed[0]["payload_json"])["reason"] == "absent-n-polls"
    assert json.loads(closed[0]["payload_json"])["misses"] == 3


def test_reappearance_resets_the_miss_counter(cfg, tmp_path):
    """bet_events is append-only (rule 9), so 'consecutive' must be DERIVED from
    a recovery marker, not stored as a counter."""
    store = Store(tmp_path / "t.sqlite")
    opp = arb_opp(cfg, "0.47", "0.49")
    process_poll(cfg, store, _poll(store, "watch", NOW), "watch", [opp], VENUE_OK, now=NOW)
    bet_id = store.query("SELECT id FROM flat_bets")[0]["id"]

    # miss, miss, SEEN, miss, miss -> still open (never 3 in a row)
    for i, sightings in enumerate(([], [], [opp], [], []), start=1):
        t = NOW + timedelta(seconds=60 * i)
        process_poll(cfg, store, _poll(store, "watch", t), "watch", sightings,
                     VENUE_OK, now=t)

    assert _events(store, bet_id, "lifetime_closed") == []
    assert len(_events(store, bet_id, "sighting_seen")) == 1
    # and no second flat_bets row was minted for the reappearance
    assert len(store.query("SELECT id FROM flat_bets")) == 1


def test_venue_gap_carry_is_bounded(cfg, tmp_path):
    """bet 126 was carried open 300 polls by a chronically-down sx. The carry is
    a grace period, not an indefinite hold."""
    store = Store(tmp_path / "t.sqlite")
    opp = arb_opp(cfg, "0.47", "0.49")
    process_poll(cfg, store, _poll(store, "watch", NOW), "watch", [opp], VENUE_OK, now=NOW)
    bet_id = store.query("SELECT id FROM flat_bets")[0]["id"]

    down = {**VENUE_OK, "kalshi": {"ok": False, "n": 0, "latency_ms": 1.0}}
    for i in range(1, int(cfg["tracker"]["dead_checks"]) + 1):
        t = NOW + timedelta(seconds=60 * i)
        process_poll(cfg, store, _poll(store, "watch", t), "watch", [], down, now=t)

    closed = _events(store, bet_id, "lifetime_closed")
    assert len(closed) == 1
    assert json.loads(closed[0]["payload_json"])["reason"] == "venue-gap-timeout"


def test_reappearance_cap_scales_with_cadence(cfg):
    """The cap is a rows/24h rate. Tripling the poll rate triples row production;
    an unscaled cap would start suppressing flags with no code change."""
    from arb.tracker import reappearance_cap
    c = {**cfg, "watch_interval_secs": 60}
    assert reappearance_cap(c) == 10
    assert reappearance_cap({**c, "watch_interval_secs": 20}) == 30
    assert reappearance_cap({**c, "watch_interval_secs": 120}) == 5
    # never zero: a pathological interval must not suppress every flag
    assert reappearance_cap({**c, "watch_interval_secs": 100000}) == 1


def test_reappearance_storm_suppresses_booking(cfg, tmp_path):
    """Pins the behaviour the config comment used to deny: this is a booking kill,
    not a log line. 73 storm annotations exist in production."""
    from arb.tracker import reappearance_cap
    store = Store(tmp_path / "t.sqlite")
    opp = arb_opp(cfg, "0.47", "0.49")
    rounds = 12
    fp = None
    for i in range(rounds):
        t = NOW + timedelta(seconds=60 * i)
        process_poll(cfg, store, _poll(store, "watch", t), "watch", [opp], VENUE_OK, now=t)
        rows = store.query("SELECT id, fingerprint FROM flat_bets ORDER BY id")
        fp = rows[-1]["fingerprint"]
        # force a fresh lifetime each round so the next poll books a new row
        store.insert("bet_events", {
            "bet_id": rows[-1]["id"], "created_at": t.isoformat(),
            "kind": "lifetime_closed",
            "payload_json": json.dumps({"reason": "test-forced"})})

    booked = store.query("SELECT COUNT(*) c FROM flat_bets WHERE fingerprint=?",
                         (fp,))[0]["c"]
    assert booked == reappearance_cap(cfg)
    assert store.query(
        "SELECT COUNT(*) c FROM left_on_table WHERE reason='reappearance-storm'"
    )[0]["c"] == rounds - reappearance_cap(cfg)


def test_candidate_legs_carry_depth_usd(cfg):
    """flat_bets.legs_json dropped depth_usd, so capacity could not be measured
    from the ledger: 74% of backtest positions fell back to an imputed median,
    and `depth_usd or 0` read the missing field as ZERO depth rather than as
    unknown — which produced a spurious 'every leg has zero depth' conclusion.
    arb/projection.py and arb/banksim.py already read this key."""
    from arb.tracker import candidate_from_opp
    cand = candidate_from_opp(cfg, arb_opp(cfg, "0.47", "0.49"))
    depths = [leg["depth_usd"] for leg in cand["legs"]]
    assert depths == ["100", "100"], f"depth not persisted: {depths}"
    assert all(Decimal(d) > 0 for d in depths)
