"""L14 Task 4 — Polymarket-native resolution reading and WHALE settlement.

No network: `get` is injected and every Gamma body is built from the Task 4
step-0 fixture (`whale_gamma_resolved.json`, recorded live 2026-09-03). The
tape is a tmp_path SQLite file; the ledger is a tmp_path Store.
"""
import copy
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from arb import invariants, reconcile, tracker, whale_resolve, whale_tape
from arb.config import load
from arb.metrics import bet_pnl_optimistic
from arb.settlement import settle_bet
from arb.store import Store

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).resolve().parent / "fixtures"
RESOLVED = json.loads((FIX / "whale_gamma_resolved.json").read_text())
COND = RESOLVED["conditionId"]
TOKENS = json.loads(RESOLVED["clobTokenIds"])
LABELS = json.loads(RESOLVED["outcomes"])
START = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)     # market start
NOW = datetime(2026, 9, 3, 20, 0, tzinfo=timezone.utc)       # 8h later


@pytest.fixture(autouse=True)
def _fresh_bucket():
    whale_tape._BUCKET = None
    yield
    whale_tape._BUCKET = None


@pytest.fixture
def cfg():
    return copy.deepcopy(load(ROOT / "config.yaml"))


class FakeGet:
    """Injected for arb.venues.base.get_json; records every URL it was given."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.urls = []

    def __call__(self, url, cfg, **kw):
        self.urls.append(url)
        r = self.responses[min(len(self.urls) - 1, len(self.responses) - 1)]
        if isinstance(r, Exception):
            raise r
        return r


def _market(**over):
    """The recorded resolved market, with field overrides."""
    m = dict(RESOLVED)
    m.update(over)
    return m


def _tape(tmp_path, start=START):
    con = whale_tape.open_tape(tmp_path / "tape.sqlite")
    con.execute("INSERT INTO markets (condition_id, sport, market_type, start_utc, "
                "tokens_json, first_seen, last_seen) VALUES (?,?,?,?,?,?,?)",
                (COND, "baseball_mlb", "WINNER", start.isoformat() if start else None,
                 json.dumps([[t, o] for t, o in zip(TOKENS, LABELS)]),
                 START.isoformat(), START.isoformat()))
    con.commit()
    return con


def _bet(store, *, kind="WHALE", token=TOKENS[0], outcome=None, created_at=None,
         start=START, cost="0.50", legs=1):
    return store.insert("flat_bets", {
        "created_at": (created_at or START - timedelta(hours=1)).isoformat(),
        "poll_id": 1, "poll_kind": "whale", "fingerprint": f"fp-{token[:8]}",
        "kind": kind, "category": "sports", "sport": "baseball_mlb",
        "event_key": "pm:" + COND, "event_start_utc": start.isoformat(),
        "start_grade": "verified", "lane": "review", "trusted": 0,
        "stake_usd": "1", "payout": "1", "status": "OPEN",
        "legs_json": json.dumps([{
            "venue": "polymarket", "market_type": "WINNER", "line": None,
            "outcome": outcome or LABELS[TOKENS.index(token)], "side": "YES",
            "cost": cost, "fee_cost": "0",
            "venue_market_id": f"{COND}:{token}"}] * legs)})


def _settled(store, bet_id):
    rows = store.query("SELECT payload_json FROM bet_events WHERE bet_id=? "
                       "AND kind='settled'", (bet_id,))
    return [json.loads(r["payload_json"]) for r in rows]


# --- the reader -------------------------------------------------------------

def test_resolved_market_yields_winner_outcome_index(cfg, tmp_path):
    con = _tape(tmp_path)
    get = FakeGet([_market()])
    assert whale_resolve.read_resolutions(cfg, con, [COND], NOW, get=get) == 1
    r = whale_resolve.resolution_for(con, COND)
    assert (r.status, r.winner_outcome_index) == ("resolved", LABELS.index("Down"))
    assert f"condition_ids={COND}" in get.urls[0] and "closed=true" in get.urls[0]


def test_half_and_half_outcome_prices_are_void(cfg, tmp_path):
    con = _tape(tmp_path)
    get = FakeGet([_market(outcomePrices='["0.5", "0.5"]')])
    assert whale_resolve.read_resolutions(cfg, con, [COND], NOW, get=get) == 1
    r = whale_resolve.resolution_for(con, COND)
    assert (r.status, r.winner_outcome_index) == ("void", None)


def test_open_market_is_open_and_persists_no_row(cfg, tmp_path):
    con = _tape(tmp_path)
    get = FakeGet([_market(closed=False, outcomePrices='["0.445", "0.555"]')])
    assert whale_resolve.read_resolutions(cfg, con, [COND], NOW, get=get) == 0
    assert whale_resolve.resolution_for(con, COND) is None


def test_market_before_resolve_after_hours_is_not_read(cfg, tmp_path):
    """A market that started an hour ago is not worth a Gamma call yet."""
    con = _tape(tmp_path, start=NOW - timedelta(hours=1))
    get = FakeGet([_market()])
    assert whale_resolve.read_resolutions(cfg, con, [COND], NOW, get=get) == 0
    assert get.urls == []


# --- settlement -------------------------------------------------------------

@pytest.mark.parametrize("prices,token,result,pnl", [
    ('["0", "1"]', TOKENS[1], "WON", Decimal("1")),     # bought the winner
    ('["0", "1"]', TOKENS[0], "LOST", Decimal("-1")),   # bought the loser
    ('["0.5", "0.5"]', TOKENS[0], "VOID", Decimal("0")),
])
def test_settle_auto_pm_appends_one_settled_event_with_pnl(
        cfg, tmp_path, prices, token, result, pnl):
    con, store = _tape(tmp_path), Store(tmp_path / "arb.sqlite")
    bid = _bet(store, token=token)
    get = FakeGet([_market(outcomePrices=prices)])
    assert whale_resolve.settle_auto_pm(cfg, store, con, NOW, get=get) == 1
    events = _settled(store, bid)
    assert len(events) == 1
    assert events[0]["per_leg"] == [result]
    assert events[0]["source"] == f"pm-gamma:{COND}"
    bet = dict(store.query("SELECT * FROM flat_bets WHERE id=?", (bid,))[0])
    assert bet_pnl_optimistic(store, bet) == pnl
    # a second pass settles nothing more (open_resolution_bets excludes it)
    assert whale_resolve.settle_auto_pm(cfg, store, con, NOW, get=get) == 0
    assert len(_settled(store, bid)) == 1


def test_settle_auto_pm_refuses_a_multi_leg_bet(cfg, tmp_path):
    """One market's winner must never settle a leg it does not price."""
    con, store = _tape(tmp_path), Store(tmp_path / "arb.sqlite")
    bid = _bet(store, token=TOKENS[1], legs=2)
    get = FakeGet([_market()])
    assert whale_resolve.settle_auto_pm(cfg, store, con, NOW, get=get) == 0
    assert _settled(store, bid) == []


def test_settle_auto_pm_leaves_ev_bets_alone(cfg, tmp_path):
    con, store = _tape(tmp_path), Store(tmp_path / "arb.sqlite")
    ev = _bet(store, kind="EV", token=TOKENS[1])
    get = FakeGet([_market()])
    assert whale_resolve.settle_auto_pm(cfg, store, con, NOW, get=get) == 0
    assert _settled(store, ev) == []


def test_unresolved_past_giveup_annotates_once_and_never_settles(cfg, tmp_path):
    """Closed with no winner ("0"/"0" — a real shape, see step 0 call 2)."""
    start = NOW - timedelta(days=int(cfg["whale"]["resolve_giveup_days"]) + 1)
    con, store = _tape(tmp_path, start=start), Store(tmp_path / "arb.sqlite")
    bid = _bet(store, token=TOKENS[0], start=start)
    get = FakeGet([_market(outcomePrices='["0", "0"]')])
    assert whale_resolve.settle_auto_pm(cfg, store, con, NOW, get=get) == 0
    assert _settled(store, bid) == []
    ann = store.query("SELECT * FROM annotations WHERE kind='resolution-giveup'")
    assert len(ann) == 1 and ann[0]["target_id"] == str(bid)
    # same day: no second annotation and no second Gamma call
    calls = len(get.urls)
    assert whale_resolve.settle_auto_pm(cfg, store, con, NOW, get=get) == 0
    assert len(get.urls) == calls
    assert len(store.query(
        "SELECT * FROM annotations WHERE kind='resolution-giveup'")) == 1


def test_exit_source_is_exempt_from_the_instant_settle_tripwire(cfg, tmp_path):
    store = Store(tmp_path / "arb.sqlite")
    now = START - timedelta(hours=1) + timedelta(seconds=30)  # 30s after entry
    exited = _bet(store, token=TOKENS[0], start=now - timedelta(hours=3))
    settle_bet(cfg, store, dict(store.query(
        "SELECT * FROM flat_bets WHERE id=?", (exited,))[0]),
        LABELS[0], "exit:flip", now)
    assert store.query("SELECT * FROM annotations WHERE kind='settle-tripwire' "
                       "AND target_id=?", (str(exited),)) == []
    assert store.query("SELECT * FROM blacklist") == []
    # the same timing under a resolution source IS the tripwire
    resolved = _bet(store, token=TOKENS[1], start=now - timedelta(hours=3))
    settle_bet(cfg, store, dict(store.query(
        "SELECT * FROM flat_bets WHERE id=?", (resolved,))[0]),
        LABELS[1], "pm-gamma:x", now)
    assert len(store.query("SELECT * FROM annotations WHERE kind='settle-tripwire' "
                           "AND target_id=?", (str(resolved),))) == 1


# --- the kind is resolution-settled everywhere ------------------------------

def test_settled_whale_bet_is_no_longer_active_in_the_tracker(cfg, tmp_path):
    store = Store(tmp_path / "arb.sqlite")
    bid = _bet(store, token=TOKENS[0])
    assert [b["id"] for b in tracker._active_bets(store, "whale")] == [bid]
    store.insert("bet_events", {
        "bet_id": bid, "created_at": NOW.isoformat(), "kind": "settled",
        "payload_json": json.dumps({"result": "EXITED", "source": "exit:flip"})})
    assert tracker._active_bets(store, "whale") == []


def test_invariants_check3_treats_whale_as_resolution_settled(cfg, tmp_path):
    store = Store(tmp_path / "arb.sqlite")
    bid = _bet(store, token=TOKENS[0])
    store.insert("bet_events", {   # settled BEFORE the event started
        "bet_id": bid, "created_at": (START - timedelta(minutes=5)).isoformat(),
        "kind": "settled", "payload_json": json.dumps({"result": "WON"})})
    assert any(f"invariant-3: bet {bid} resolution-settled before start" in v
               for v in invariants.check(cfg, store))


def test_reconcile_skips_whale_bets(cfg, tmp_path, monkeypatch):
    # reconcile swallows resolver exceptions, so a raising stub would not
    # discriminate: count the calls instead.
    seen = []
    monkeypatch.setitem(reconcile.RESOLVERS, "polymarket",
                        lambda cfg, leg: seen.append(leg))
    store = Store(tmp_path / "arb.sqlite")
    for bid, payload in ((_bet(store, token=TOKENS[0]),
                          {"result": "WON", "per_leg": ["WON"]}),
                         (_bet(store, token=TOKENS[1]),
                          {"result": "EXITED", "source": "exit:flip"})):
        store.insert("bet_events", {
            "bet_id": bid, "created_at": NOW.isoformat(), "kind": "settled",
            "payload_json": json.dumps(payload)})
    assert reconcile.reconcile(cfg, store, now=NOW) == 0
    assert seen == []   # no Gamma read for a WHALE bet at all
    assert store.query("SELECT * FROM pair_demotions") == []
    assert store.query("SELECT * FROM bet_events WHERE kind='reconciled'") == []


def test_cmd_settle_runs_check_even_when_settle_auto_pm_raises(cfg, tmp_path,
                                                              monkeypatch):
    from types import SimpleNamespace

    from arb import commands
    cfg["whale"]["tape_db"] = str(tmp_path / "tape.sqlite")
    store = Store(tmp_path / "arb.sqlite")
    _bet(store, token=TOKENS[0])
    called, ran = [], []

    def boom(*a, **k):
        called.append(1)
        raise RuntimeError("gamma down")
    monkeypatch.setattr(whale_resolve, "settle_auto_pm", boom)
    monkeypatch.setattr(invariants, "check", lambda c, s, *a, **k: ran.append(1) or [])
    rc = commands.cmd_settle(cfg, store, SimpleNamespace(manual=None, dry_run=False,
                                                         yes=True))
    assert called == [1]   # cmd_settle actually calls it
    assert ran == [1] and rc == 0
