"""L14 Task 4 — Polymarket-native resolution reading and WHALE settlement.

No network: `get` is injected and every Gamma body is built from the Task 4
step-0 fixture (`whale_gamma_resolved.json`, recorded live 2026-09-03). The
tape is a tmp_path SQLite file; the ledger is a tmp_path Store.
"""
import copy
import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from arb import invariants, reconcile, tracker, whale_resolve, whale_sweep, whale_tape
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
         start=START, cost="0.50", legs=1, side="YES"):
    return store.insert("flat_bets", {
        "created_at": (created_at or START - timedelta(hours=1)).isoformat(),
        "poll_id": 1, "poll_kind": "whale", "fingerprint": f"fp-{token[:8]}",
        "kind": kind, "category": "sports", "sport": "baseball_mlb",
        "event_key": "pm:" + COND, "event_start_utc": start.isoformat(),
        "start_grade": "verified", "lane": "review", "trusted": 0,
        "stake_usd": "1", "payout": "1", "status": "OPEN",
        "legs_json": json.dumps([{
            "venue": "polymarket", "market_type": "WINNER", "line": None,
            "outcome": outcome or LABELS[TOKENS.index(token)], "side": side,
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


def test_seed_starts_fills_a_null_start_but_never_overwrites_a_real_one(cfg, tmp_path):
    """INSERT OR IGNORE left a start_utc NULL forever once the row existed
    (bypassing the 4h resolve_after_hours gate, finding 3). The upsert must
    fill a NULL from the bet's own start, but a start_utc the tape already
    has (from Gamma, presumably more accurate) must survive untouched."""
    con = _tape(tmp_path)
    con.execute("UPDATE markets SET start_utc=NULL WHERE condition_id=?", (COND,))
    con.commit()
    bet = {"id": 1, "event_start_utc": START.isoformat()}
    whale_resolve._seed_starts(con, [bet], {1: COND}, NOW)
    row = con.execute("SELECT start_utc FROM markets WHERE condition_id=?",
                      (COND,)).fetchone()
    assert row["start_utc"] == START.isoformat()

    other_start = (START + timedelta(hours=5)).isoformat()
    whale_resolve._seed_starts(con, [{"id": 1, "event_start_utc": other_start}],
                               {1: COND}, NOW)
    row = con.execute("SELECT start_utc FROM markets WHERE condition_id=?",
                      (COND,)).fetchone()
    assert row["start_utc"] == START.isoformat()   # unchanged, not clobbered


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


@pytest.mark.parametrize("outcome,side,token,expected", [
    ("Down", "YES", TOKENS[1], "WON"),   # bought the winning token, own label
    ("Up", "YES", TOKENS[0], "LOST"),    # bought the losing token, own label
    ("Down", "NO", TOKENS[1], "LOST"),   # side=NO inversion flag flips a WHALE
                                          # leg even on the winning token — the
                                          # convention docs this still applies
])
def test_leg_convention_winner_decided_by_token_identity(
        cfg, tmp_path, outcome, side, token, expected):
    """Pinned leg convention (spec §8): a WHALE leg BUYS one token, `outcome`
    names that token's own label, `side=NO` is the ledger's ordinary
    complement-inversion flag. Down (index 1) wins in the recorded fixture."""
    con, store = _tape(tmp_path), Store(tmp_path / "arb.sqlite")
    bid = _bet(store, token=token, outcome=outcome, side=side)
    get = FakeGet([_market()])
    assert whale_resolve.settle_auto_pm(cfg, store, con, NOW, get=get) == 1
    events = _settled(store, bid)
    assert events[0]["per_leg"] == [expected]


def test_settle_auto_pm_counts_only_appended_settled_events(cfg, tmp_path):
    """settle_bet has refusal branches (future-start guard, terminal grace)
    that return without appending a bet_events row; n must reflect actual
    settlements, not every settle_bet call (finding 2)."""
    start = NOW - timedelta(hours=1)   # inside terminal_mute_grace_mins (180m)
    con, store = _tape(tmp_path, start=start), Store(tmp_path / "arb.sqlite")
    bid = _bet(store, token=TOKENS[1], start=start)
    con.execute(
        "INSERT INTO resolutions (condition_id, resolved_at, winner_outcome_index,"
        " status, source, raw_json, fetched_at) VALUES (?,?,?,?,?,?,?)",
        (COND, NOW.isoformat(), LABELS.index("Down"), "resolved", "gamma",
         json.dumps(RESOLVED), NOW.isoformat()))
    con.commit()
    get = FakeGet([])   # already resolved and not due — must not be called
    assert whale_resolve.settle_auto_pm(cfg, store, con, NOW, get=get) == 0
    assert get.urls == []
    assert _settled(store, bid) == []


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


def test_settle_auto_pm_survives_a_failed_gamma_call(cfg, tmp_path):
    """A failed Gamma call backs off (whale_tape._set_backoff) and the cycle
    still returns cleanly — no bet settled, nothing raised."""
    con, store = _tape(tmp_path), Store(tmp_path / "arb.sqlite")
    bid = _bet(store, token=TOKENS[0])
    get = FakeGet(RuntimeError("gamma 500"))
    assert whale_resolve.settle_auto_pm(cfg, store, con, NOW, get=get) == 0
    assert _settled(store, bid) == []
    assert whale_tape._meta(con, "backoff_until") is not None
    assert whale_resolve.resolution_for(con, COND) is None


def test_winner_label_collision_refuses_to_settle(cfg, tmp_path, caplog):
    """A leg whose recorded `outcome` matches the winner's label but whose
    token does not (bad data, or two outcomes sharing a label) must never
    settle silently: _winner_label refuses and only a warning is logged."""
    con, store = _tape(tmp_path), Store(tmp_path / "arb.sqlite")
    # bought the Up token but the leg's outcome is mislabeled "Down" — the
    # winning label — so token identity and label identity disagree
    bid = _bet(store, token=TOKENS[0], outcome="Down")
    get = FakeGet([_market()])   # Down (index 1) wins
    with caplog.at_level(logging.WARNING):
        assert whale_resolve.settle_auto_pm(cfg, store, con, NOW, get=get) == 0
    assert _settled(store, bid) == []
    assert any("refusing to settle" in r.getMessage() for r in caplog.records)


def test_settle_auto_pm_returns_zero_on_a_broken_tape(cfg, tmp_path):
    """sqlite3.OperationalError from the tape (e.g. a wiped/corrupt file
    caught mid-write) must never propagate out of the settle cycle."""
    con, store = _tape(tmp_path), Store(tmp_path / "arb.sqlite")
    bid = _bet(store, token=TOKENS[0])
    con.execute("DROP TABLE resolutions")
    con.commit()
    get = FakeGet([_market()])
    assert whale_resolve.settle_auto_pm(cfg, store, con, NOW, get=get) == 0
    assert _settled(store, bid) == []


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
    from arb.venues import mlbstats
    cfg["whale"]["tape_db"] = str(tmp_path / "tape.sqlite")
    store = Store(tmp_path / "arb.sqlite")
    _bet(store, token=TOKENS[0])
    called, ran, mlb_calls = [], [], []

    def boom(*a, **k):
        called.append(1)
        raise RuntimeError("gamma down")
    monkeypatch.setattr(whale_resolve, "settle_auto_pm", boom)
    monkeypatch.setattr(invariants, "check", lambda c, s, *a, **k: ran.append(1) or [])
    # finding 6: settle_auto_mlb runs FOR REAL here (only settle_auto_pm is
    # stubbed). It must never reach the live MLB Stats API for this bet — that
    # currently holds because it is kind=WHALE (settlement.py's own exclusion)
    # — but the test must catch it directly, not lean on that exclusion never
    # regressing and silently making a network call in the suite.
    monkeypatch.setattr(mlbstats, "fetch_schedule",
                        lambda *a, **k: mlb_calls.append(1) or [])
    rc = commands.cmd_settle(cfg, store, SimpleNamespace(manual=None, dry_run=False,
                                                         yes=True))
    assert called == [1]   # cmd_settle actually calls it
    assert ran == [1] and rc == 0
    assert mlb_calls == []


def test_cmd_settle_survives_a_whale_import_failure(cfg, tmp_path, monkeypatch):
    """finding 5: the whale imports moved inside the try (arb/commands.py) —
    an import-time failure must not take reconcile()/check() down with it,
    same as any other settle_auto_pm failure."""
    import sys
    from types import SimpleNamespace

    from arb import commands
    cfg["whale"]["tape_db"] = str(tmp_path / "tape.sqlite")
    store = Store(tmp_path / "arb.sqlite")
    ran = []
    monkeypatch.setattr(invariants, "check", lambda c, s, *a, **k: ran.append(1) or [])
    # simulate an import-time failure of arb.whale_resolve: drop the cached
    # attribute on the `arb` package AND mark the submodule broken in
    # sys.modules, so `from arb import whale_resolve` re-attempts the import
    # and raises ImportError (the standard sys.modules[name]=None technique).
    monkeypatch.delattr(sys.modules["arb"], "whale_resolve", raising=False)
    monkeypatch.setitem(sys.modules, "arb.whale_resolve", None)
    rc = commands.cmd_settle(cfg, store, SimpleNamespace(manual=None, dry_run=False,
                                                         yes=True))
    assert ran == [1] and rc == 0


def test_invariant_3_ignores_an_exit_settled_bet(cfg, tmp_path):
    """I1 (final review 2026-09-04): an EXITED WHALE bet is settled PREGAME by
    design (§9), so invariant 3 fired on every one of them at every daily
    `settle` — a CRITICAL log and an `invariant-violation` annotation per bet
    per day. Only ORACLE settlements can be early."""
    store = Store(tmp_path / "arb.sqlite")
    bid = _bet(store, token=TOKENS[0])
    store.insert("bet_events", {
        "bet_id": bid, "created_at": (START - timedelta(minutes=30)).isoformat(),
        "kind": "settled", "payload_json": json.dumps(
            {"result": "EXITED", "source": "exit:flip", "pnl": "-0.07"})})

    assert [v for v in invariants.check(cfg, store) if "invariant-3" in v] == []
    assert store.query("SELECT 1 FROM annotations WHERE kind='invariant-violation'") == []


def test_the_sweep_moved_out_to_its_own_module_and_the_file_stays_under_300(tmp_path):
    """V4 fix pass finding 5: whale_resolve.py hit 318 lines. SWEEP_SQL and
    resolve_sweep move to arb/whale_sweep.py (only those two — everything
    else about resolution reading stays here); this pins both the size and
    that the move actually happened, not just a re-export."""
    lines = Path("arb/whale_resolve.py").read_text().splitlines()
    assert len(lines) <= 300
    assert not hasattr(whale_resolve, "resolve_sweep")
    assert not hasattr(whale_resolve, "SWEEP_SQL")
    assert hasattr(whale_sweep, "resolve_sweep") and hasattr(whale_sweep, "SWEEP_SQL")
