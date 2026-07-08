"""Phase 7 ops: markout hook, supervisor lock/stop-flag semantics, canary."""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from arb.config import load
from arb.markout import persist_marks
from arb.store import Store

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def cfg():
    return load(ROOT / "config.yaml")


def test_markouts_use_nearest_sample_within_tolerance(cfg, tmp_path):
    store = Store(tmp_path / "t.sqlite")
    start = NOW + timedelta(hours=6)
    bet_id = store.insert("flat_bets", {
        "created_at": NOW.isoformat(), "poll_id": 1, "poll_kind": "watch",
        "fingerprint": "f", "position_key": "p", "kind": "ARB", "category": "sports",
        "sport": "baseball_mlb", "venue_pair": "kalshi:self", "event_key": "e",
        "event_start_utc": start.isoformat(), "start_grade": "verified",
        "legs_json": json.dumps([{"venue": "kalshi", "market_type": "WINNER",
                                  "line": None, "outcome": "mets", "side": "YES",
                                  "cost": "0.48", "mid0": "0.47"}]),
        "stake_usd": "1", "edge_points": "0.02", "trusted": 1, "lane": "review",
        "quality_flag": "none", "leg_skew_secs": 0, "status": "OK",
        "pnl_optimistic": "0.041667", "notes": ""})
    # samples at +35s (inside ±50% of 30s) and +150s (inside ±50% of 120s)
    for offset, cost in ((35, "0.45"), (150, "0.52")):
        store.insert("quotes", {
            "poll_id": 1, "fetched_at": (NOW + timedelta(seconds=offset)).isoformat(),
            "event_key": "e", "category": "sports", "sport": "baseball_mlb",
            "venue": "kalshi", "book": "kalshi", "market_type": "WINNER",
            "line": None, "outcome": "mets", "side": "YES", "raw_price": cost,
            "cost": cost, "fee_cost": "0", "depth_usd": "100", "ladder_json": None,
            "live": 0, "bid": str(Decimal(cost) - Decimal("0.02")),
            "volume_24h": "5", "start_utc": start.isoformat(),
            "start_grade": "verified"})
    n = persist_marks(cfg, store, now=NOW + timedelta(seconds=400))
    marks = store.query("SELECT * FROM markouts ORDER BY horizon_secs")
    assert n == len(marks) >= 2
    m30 = [m for m in marks if m["horizon_secs"] == 30][0]
    assert m30["actual_offset_secs"] == 35.0
    assert Decimal(m30["mark_cost"]) == Decimal("0.44")  # mid of 0.45/0.43
    assert not m30["in_play"]
    # idempotent: re-run writes nothing new
    assert persist_marks(cfg, store, now=NOW + timedelta(seconds=401)) == 0


def test_supervisor_lock_refuses_double_run(cfg, tmp_path, monkeypatch):
    import arb.heartbeat as hb
    monkeypatch.setattr(hb, "LOCK_DIR", tmp_path / "lock")
    assert hb.acquire_lock() is True
    assert hb.acquire_lock() is False  # same live pid holds it
    hb.release_lock()
    assert hb.acquire_lock() is True
    hb.release_lock()


def test_supervisor_stop_flag_halts(cfg, tmp_path, monkeypatch):
    import arb.heartbeat as hb
    monkeypatch.setattr(hb, "LOCK_DIR", tmp_path / "lock")
    monkeypatch.setattr(hb, "STOP_FLAG", tmp_path / "stop-all")
    (tmp_path / "stop-all").touch()
    assert hb.run_supervisor(cfg, once=True) == 0  # exits before launching anything
    assert not (tmp_path / "lock").exists()


def test_canary_pass(cfg, tmp_path):
    from datetime import datetime, timezone

    from arb import metrics
    from arb.settlement import settle_manual
    from arb.tracker import process_poll
    from tests.canary_fixture import canary_opps
    store = Store(tmp_path / "t.sqlite")
    now = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
    cfg2 = dict(cfg, ev_enabled=True)
    opps, event_key = canary_opps("test", now)
    pid = store.insert("polls", {"started_at": now.isoformat(), "kind": "canary",
                                 "n_quotes": 0, "n_opps": 2})
    process_poll(cfg2, store, pid, "canary", opps, {}, now=now)
    settle_manual(cfg2, store, event_key, "canary-yes", now=now + timedelta(hours=10))
    bets = store.query("SELECT * FROM flat_bets")
    assert len(bets) == 2
    got = {b["kind"]: metrics.bet_pnl_optimistic(store, dict(b)) for b in bets}
    assert got == {"ARB": Decimal("0.041667"), "EV": Decimal("0.932367")}
    assert all(b["lane"] == "review" for b in bets)  # canary never touches headline
