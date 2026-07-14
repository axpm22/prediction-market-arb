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


# ---------- H9: supervisor zombies + boot-time schedule burst ----------
from zoneinfo import ZoneInfo
NY = ZoneInfo("America/New_York")


def test_startup_grace_marks_only_stale_slots(cfg):
    from arb.heartbeat import initial_last_fired
    cfg["supervisor"]["schedule_catchup_grace_mins"] = 30
    now = datetime(2026, 7, 8, 6, 20, tzinfo=NY)  # 06:20 ET
    marks = initial_last_fired(cfg, now)
    assert "backup" in marks          # 04:10 -> >30min ago -> suppressed
    assert "scan-06:15" not in marks  # 5min ago -> within grace -> allowed to catch up
    assert "settle" not in marks      # 09:30 -> future -> not marked


def test_due_fires_within_grace_but_suppresses_stale(cfg):
    from arb.heartbeat import _due, initial_last_fired
    cfg["supervisor"]["schedule_catchup_grace_mins"] = 30
    now = datetime(2026, 7, 8, 6, 20, tzinfo=NY)
    marks = initial_last_fired(cfg, now)
    assert _due(marks, "scan-06:15", "06:15", now) is True      # catch up once
    assert _due(marks, "backup", str(cfg["db_backup"]["time"]), now) is False  # stale


def test_reap_finished_drops_exited_children():
    from arb.heartbeat import _reap_finished

    class Stub:
        def __init__(self, code):
            self._c = code

        def poll(self):
            return self._c

    a, b = Stub(None), Stub(0)  # a still running, b exited
    assert _reap_finished([a, b]) == [a]


def test_terminate_kills_and_reaps_on_timeout():
    import subprocess

    from arb.heartbeat import _terminate
    state = {"killed": False, "waits": 0}

    class Stub:
        def wait(self, timeout=None):
            state["waits"] += 1
            if timeout is not None and state["waits"] == 1:
                raise subprocess.TimeoutExpired("cmd", timeout)
            return 0

        def kill(self):
            state["killed"] = True

    _terminate(Stub(), 1.0)
    assert state["killed"] is True and state["waits"] >= 2  # timed out -> kill -> reap


def test_sntp_offset_parses_real_macos_output():
    # the OLD parse read split()[-2] — the server hostname — so float() raised,
    # the blanket except swallowed it, and the clock-skew alarm could NEVER fire
    from arb.commands import _sntp_offset
    out = "+0.014657 +/- 0.009361 time.apple.com 2620:149:a33:4000::21"
    assert abs(_sntp_offset(out) - 0.014657) < 1e-9
    assert abs(_sntp_offset("sntp 4.x\n-0.5 +/- 0.1 time.apple.com") + 0.5) < 1e-9
    assert _sntp_offset("no offset here") is None
    assert _sntp_offset("") is None


def test_backup_is_idempotent_same_day(cfg, tmp_path):
    # cli.py:3 contract: every command is safe to run twice. VACUUM INTO
    # refuses an existing target, so a second same-day backup crashed.
    from arb.commands import cmd_backup
    from arb.store import Store
    store = Store(tmp_path / "b.sqlite")
    c = {**cfg, "db_backup": {**cfg["db_backup"], "dest": str(tmp_path / "bk")}}
    assert cmd_backup(c, store, None) == 0
    assert cmd_backup(c, store, None) == 0


def test_supervisor_children_inherit_db_and_config(cfg, tmp_path, monkeypatch):
    # children spawned as `python -m arb <cmd>` read the DEFAULT db/config —
    # a supervisor on a non-default DB silently split data across two databases
    import arb.heartbeat as hb
    argvs = []

    class FakeProc:
        def __init__(self, argv):
            argvs.append(argv)

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(hb, "LOCK_DIR", tmp_path / "lock")
    monkeypatch.setattr(hb, "STOP_FLAG", tmp_path / "absent-stop")
    monkeypatch.setattr(hb.subprocess, "Popen", lambda argv: FakeProc(argv))
    hb.run_supervisor(cfg, once=True, db="other.sqlite", config="alt.yaml")
    assert argvs, "no children spawned"
    for argv in argvs:
        assert argv[argv.index("--db") + 1] == "other.sqlite", argv
        assert argv[argv.index("--config") + 1] == "alt.yaml", argv


def test_one_rotating_handler_per_logfile():
    # every module logger attached its OWN TimedRotatingFileHandler to
    # logs/scan.log; at midnight each handler renames the file independently,
    # clobbering backups (well-known TimedRotatingFileHandler failure mode)
    import logging

    from arb.logutil import get_logger
    a = get_logger("arb.test_rot_a", "logs/scan.log")
    b = get_logger("arb.test_rot_b", "logs/scan.log")
    fa = [h for h in a.handlers if isinstance(h, logging.handlers.TimedRotatingFileHandler)]
    fb = [h for h in b.handlers if isinstance(h, logging.handlers.TimedRotatingFileHandler)]
    assert fa and fb and fa[0] is fb[0]


def test_negrisk_scan_pages_config_is_wired(monkeypatch):
    # negrisk.scan_pages existed to fix a named V1 bug but was never read —
    # the adapter hardcoded max_pages=10
    import arb.venues.polymarket as pm
    seen = []
    monkeypatch.setattr(
        pm, "fetch_gamma_events",
        lambda cfg, tag, max_pages=10, deadline=None: seen.append(max_pages) or [])
    pm.fetch_negrisk_events({"negrisk": {"scan_pages": 7}}, ["politics"])
    assert seen == [7]


def test_log_retention_days_config_is_wired(tmp_path, monkeypatch):
    # log_retention_days was read by nothing; every logger hardcoded 90
    import logging.handlers as lh

    import arb.logutil as lu
    monkeypatch.setattr(lu, "RETENTION_DAYS", 7)
    lg = lu.get_logger("arb.test_retention", tmp_path / "r.log")
    h = [h for h in lg.handlers if isinstance(h, lh.TimedRotatingFileHandler)][0]
    assert h.backupCount == 7


def test_redacting_filter_handles_single_dict_arg(caplog):
    # logging special-cases a single dict arg (record.args IS the dict); the
    # filter's tuple(...) turned it into a tuple of KEYS and broke formatting
    import logging

    from arb.logutil import get_logger
    lg = get_logger("arb.test_dictarg")
    with caplog.at_level(logging.INFO, logger="arb.test_dictarg"):
        lg.info("summary: %s", {"a": 1, "b": "x?secret"})
    assert any("'a': 1" in r.getMessage() for r in caplog.records)
    assert not any("secret" in r.getMessage() for r in caplog.records)  # redacted


def test_cmd_scan_aborts_gracefully_on_credits_low(cfg, tmp_path, monkeypatch):
    # M4: CreditsLow used to propagate as a raw traceback and skip the
    # oddsapi-credits annotation exactly when the count mattered most
    from types import SimpleNamespace

    from arb.commands import cmd_scan
    from arb.store import Store
    import arb.venues.oddsapi as oa

    def blow(c, s):
        oa.LAST_REMAINING = 49
        raise oa.CreditsLow("49 credits left — aborting (rule 2)")

    monkeypatch.setattr(oa, "fetch_odds", blow)
    monkeypatch.setattr(oa, "api_key", lambda: "k")
    store = Store(tmp_path / "c.sqlite")
    assert cmd_scan(cfg, store, SimpleNamespace()) == 1
    row = store.query("SELECT detail FROM annotations WHERE kind='oddsapi-credits'")
    assert row and row[0]["detail"] == "49"


def test_cmd_scan_anchor_disabled_makes_zero_anchor_calls(cfg, tmp_path, monkeypatch):
    # Part A, rule 2: consensus_anchor.enabled=false (the shipped default) must
    # never construct the extra eu anchor call.
    from arb.commands import cmd_scan
    from arb.store import Store
    import arb.venues.oddsapi as oa

    calls = []
    monkeypatch.setattr(oa, "fetch_odds", lambda c, s: [])
    monkeypatch.setattr(oa, "fetch_anchor_odds", lambda c, s: calls.append(s) or [])
    monkeypatch.setattr(oa, "api_key", lambda: "k")
    assert cfg["consensus_anchor"]["enabled"] is False
    store = Store(tmp_path / "c.sqlite")
    from types import SimpleNamespace
    cmd_scan(cfg, store, SimpleNamespace())
    assert calls == []


def test_cmd_scan_anchor_enabled_fetches_once_per_sport(cfg, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from arb.commands import cmd_scan
    from arb.store import Store
    import arb.venues.oddsapi as oa

    calls = []
    monkeypatch.setattr(oa, "fetch_odds", lambda c, s: [])
    monkeypatch.setattr(oa, "fetch_anchor_odds", lambda c, s: calls.append(s) or [])
    monkeypatch.setattr(oa, "api_key", lambda: "k")
    cfg2 = dict(cfg, consensus_anchor=dict(cfg["consensus_anchor"], enabled=True))
    store = Store(tmp_path / "c2.sqlite")
    cmd_scan(cfg2, store, SimpleNamespace())
    assert calls == list(cfg["consensus_sports"])
