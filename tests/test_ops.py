"""Phase 7 ops: markout hook, supervisor lock/stop-flag semantics, canary."""
import copy
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
    cfg["scan_times"] = ["06:15"]     # the shipped list is empty in whale-only mode (2026-09-04);
                                      # this test needs a scan slot to be meaningful
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


def test_pytest_logging_stays_out_of_repo_logs():
    # logs/supervisor.log 2026-07-17 16:33/16:36: TEST runs wrote fake
    # "supervisor up" events into production forensics. conftest sets
    # ARB_LOG_DIR before any arb import; get_logger must honor it.
    import os

    import arb.heartbeat  # noqa: F401 — module-level logger under the redirect
    from arb.logutil import get_logger
    root = Path(os.environ["ARB_LOG_DIR"])
    repo_logs = ROOT / "logs"
    before = {p: (p.stat().st_mtime_ns, p.stat().st_size)
              for p in repo_logs.rglob("*")} if repo_logs.exists() else {}
    log = get_logger("arb.test_redirect_probe", "logs/redirect_probe.log")
    log.info("redirect probe")
    for h in log.handlers:
        h.flush()
    out = root / "logs" / "redirect_probe.log"
    assert out.exists() and "redirect probe" in out.read_text()
    after = {p: (p.stat().st_mtime_ns, p.stat().st_size)
             for p in repo_logs.rglob("*")} if repo_logs.exists() else {}
    assert after == before  # production logs untouched


def test_backup_truncates_wal(cfg, tmp_path, capsys):
    # 15.2 GB WAL on the live box (2026-07-17): checkpoints never completed.
    # cmd_backup must run PRAGMA wal_checkpoint(TRUNCATE) after the backup
    # and log the (busy, log, checkpointed) triple.
    import sqlite3

    from arb.commands import cmd_backup
    db = tmp_path / "b.sqlite"
    store = Store(db)
    for i in range(200):
        store.insert("polls", {"started_at": f"2026-07-17T00:00:{i % 60:02d}",
                               "kind": "watch", "n_quotes": 0, "n_opps": 0})
    # a second reader held open across writes starves auto-checkpoint; close it
    reader = sqlite3.connect(db)
    reader.execute("SELECT COUNT(*) FROM polls").fetchall()
    reader.close()
    assert (tmp_path / "b.sqlite-wal").stat().st_size > 0
    c = {**cfg, "db_backup": {**cfg["db_backup"], "dest": str(tmp_path / "bk")}}
    assert cmd_backup(c, store, None) == 0
    assert (tmp_path / "b.sqlite-wal").stat().st_size == 0
    assert "wal_checkpoint" in capsys.readouterr().out


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


def test_redacting_filter_keeps_numeric_args_formattable(caplog):
    """The filter used to str() every arg, so any '%d'/'%f' in the codebase
    raised TypeError at emit time — a crash inside the logging call, not a log
    line. Observed live 2026-07-29 on matching.py's start-unknown counter;
    settlement.py and reconcile.py have the same shape on money paths."""
    import logging

    from arb.logutil import get_logger
    lg = get_logger("arb.test_numeric")
    with caplog.at_level(logging.INFO, logger="arb.test_numeric"):
        lg.info("dropped %d rows in %.1f s for bet %d", 22, 1.5, 7)
    assert any(r.getMessage() == "dropped 22 rows in 1.5 s for bet 7"
               for r in caplog.records)
    # strings are still scrubbed
    with caplog.at_level(logging.INFO, logger="arb.test_numeric"):
        lg.info("url %s", "https://x.test/v4?apiKey=hunter2")
    assert not any("hunter2" in r.getMessage() for r in caplog.records)
    # bool is an int subclass but formats as True/False under %s — keep it a str
    with caplog.at_level(logging.INFO, logger="arb.test_numeric"):
        lg.info("ok %s", True)
    assert any(r.getMessage() == "ok True" for r in caplog.records)


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
    # Part A, rule 2: consensus_anchor.enabled=false must never construct the
    # extra eu anchor call (Part C flipped the shipped default to true once
    # regrade existed to score it, so this test pins its own disabled config).
    from arb.commands import cmd_scan
    from arb.store import Store
    import arb.venues.oddsapi as oa

    calls = []
    monkeypatch.setattr(oa, "fetch_odds", lambda c, s: [])
    monkeypatch.setattr(oa, "fetch_anchor_odds", lambda c, s: calls.append(s) or [])
    monkeypatch.setattr(oa, "api_key", lambda: "k")
    cfg2 = dict(cfg, consensus_anchor=dict(cfg["consensus_anchor"], enabled=False))
    store = Store(tmp_path / "c.sqlite")
    from types import SimpleNamespace
    cmd_scan(cfg2, store, SimpleNamespace())
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


def test_verify_lane_is_supervised(cfg):
    """verify is implemented (cmd_verify), registered in the CLI, and has a
    cadence branch in heartbeat - but was never in supervisor.processes, so the
    branch was unreachable and the lane never ran: ONE annotation in the
    production DB, from 2026-07-08. It is the only sub-cadence arb-lifetime
    measurement in the repo and costs zero Odds API credits.

    Self-contained: verify was flipped off 2026-09-04 (whale-lane-only
    operator decision), so this test enables it on its own deepcopy rather
    than depending on which lanes are currently shipped on."""
    from arb.heartbeat import lane_interval
    cfg = copy.deepcopy(cfg)
    cfg["supervisor"]["processes"]["verify"] = True
    procs = cfg["supervisor"]["processes"]
    assert procs.get("verify") is True
    assert cfg["supervisor"]["commands"]["verify"] == "verify --once"
    assert lane_interval(cfg, "verify") == float(cfg["verify"]["poll_secs"])


def test_every_supervised_process_has_a_command(cfg):
    """A process enabled with no command entry KeyErrors the supervisor loop."""
    for name, on in cfg["supervisor"]["processes"].items():
        if on:
            assert name in cfg["supervisor"]["commands"], name


def test_next_grid_run_is_strictly_future_and_drift_free():
    """Anchoring to now+interval accumulates the supervisor's 2s tick overshoot;
    an absolute grid cannot drift no matter how late the tick arrives."""
    from arb.heartbeat import next_grid_run
    assert next_grid_run(now=0.0, interval=60.0, offset=0.0) == 60.0
    assert next_grid_run(now=59.9, interval=60.0, offset=0.0) == 60.0
    assert next_grid_run(now=60.0, interval=60.0, offset=0.0) == 120.0
    # a late tick still lands on the grid, it does not push the phase out
    assert next_grid_run(now=119.5, interval=60.0, offset=0.0) == 120.0
    assert next_grid_run(now=0.0, interval=60.0, offset=12.0) == 12.0
    assert next_grid_run(now=12.0, interval=60.0, offset=12.0) == 72.0


def test_lane_offsets_never_collide_within_a_period(cfg):
    """stagger_secs 15 x 5 lanes = 60s = watch_interval_secs, so lane 5 booted in
    exact lockstep with lane 1 (and Task 4 made it 6 lanes). Spread inside each
    lane's own period instead."""
    from arb.heartbeat import lane_interval, lane_offsets
    offs = lane_offsets(cfg)
    enabled = [n for n, on in cfg["supervisor"]["processes"].items() if on]
    assert set(offs) == set(enabled)
    for name in enabled:
        assert 0 <= offs[name] < lane_interval(cfg, name)
    assert len(set(offs.values())) == len(enabled), offs


def test_watch_and_live_are_never_scheduled_simultaneously(cfg):
    """live.cadence_secs 20 divides watch_interval_secs 60 exactly, so a shared
    phase is permanent, not transient. Measured 2026-07-29: 355 of 1,078 watch
    polls had a live poll start within 5s. Simulate an hour of grid slots.

    Self-contained: live was flipped off 2026-09-04 (whale-lane-only operator
    decision); enable it on a deepcopy so this test doesn't depend on which
    lanes are currently shipped on."""
    from arb.heartbeat import lane_interval, lane_offsets, next_grid_run
    cfg = copy.deepcopy(cfg)
    cfg["supervisor"]["processes"]["live"] = True
    offs = lane_offsets(cfg)

    def slots(name, horizon=3600.0):
        interval, off, t, out = lane_interval(cfg, name), offs[name], 0.0, []
        while t < horizon:
            t = next_grid_run(t, interval, off)
            out.append(round(t, 3))
        return out

    watch, live = set(slots("watch")), set(slots("live"))
    assert not (watch & live), sorted(watch & live)[:5]


def test_stale_lanes_flags_a_silent_lane(cfg, tmp_path):
    """negrisk and political were dark for 9.7 days and nothing said so: the
    supervisor beat covers the supervisor, not each lane. Reproduced 2026-07-29
    — max inter-poll gap for both lanes is 232.2 h.

    Self-contained: negrisk was flipped off 2026-09-04 (whale-lane-only
    operator decision), and stale_lanes only reports enabled lanes — enable it
    on a deepcopy so this test doesn't depend on which lanes ship on."""
    from arb.heartbeat import stale_lanes
    from arb.store import Store
    cfg = copy.deepcopy(cfg)
    cfg["supervisor"]["processes"]["negrisk"] = True
    store = Store(tmp_path / "t.sqlite")
    fresh = NOW - timedelta(seconds=30)
    store.insert("polls", {"started_at": fresh.isoformat(), "kind": "watch",
                           "n_quotes": 1, "n_opps": 0})
    store.insert("polls", {"started_at": (NOW - timedelta(days=9)).isoformat(),
                           "kind": "negrisk", "n_quotes": 1, "n_opps": 0})

    stale = dict(stale_lanes(cfg, store, NOW))
    assert "negrisk" in stale
    assert stale["negrisk"] > 9 * 86400 - 60
    assert "watch" not in stale


def test_stale_lanes_ignores_lanes_that_never_ran(cfg, tmp_path):
    """A lane with no polls at all is a cold start, not a silence: alarming on it
    would fire on every fresh DB."""
    from arb.heartbeat import stale_lanes
    from arb.store import Store
    assert stale_lanes(cfg, Store(tmp_path / "t.sqlite"), NOW) == []


def test_stale_lanes_skips_lanes_that_write_no_poll_rows(cfg, tmp_path):
    """verify writes annotations, not polls — it must never be reported silent."""
    from arb.heartbeat import POLL_KIND, stale_lanes
    from arb.store import Store
    assert "verify" not in POLL_KIND
    store = Store(tmp_path / "t.sqlite")
    store.insert("polls", {"started_at": (NOW - timedelta(days=9)).isoformat(),
                           "kind": "watch", "n_quotes": 1, "n_opps": 0})
    assert "verify" not in dict(stale_lanes(cfg, store, NOW))


def test_stale_lanes_counts_a_heartbeat_as_liveness(cfg, tmp_path):
    """A lane that ran and correctly found nothing writes NO poll row — the live
    lane skips its poll on an empty shortlist. Polls measure work, heartbeats
    measure running; silence means neither. Without this, enabling the empty-
    shortlist skip would make the alarm scream all night.

    Self-contained: live was flipped off 2026-09-04 (whale-lane-only operator
    decision), and stale_lanes only reports enabled lanes — enable it on a
    deepcopy so this test doesn't depend on which lanes ship on."""
    from arb.heartbeat import stale_lanes
    from arb.store import Store
    cfg = copy.deepcopy(cfg)
    cfg["supervisor"]["processes"]["live"] = True
    store = Store(tmp_path / "t.sqlite")
    store.insert("polls", {"started_at": (NOW - timedelta(hours=3)).isoformat(),
                           "kind": "live", "n_quotes": 1, "n_opps": 0})
    assert "live" in dict(stale_lanes(cfg, store, NOW))   # stale on polls alone
    store.insert("heartbeats", {"process": "live",
                                "beat_at": (NOW - timedelta(seconds=5)).isoformat(),
                                "head_sha": "abc", "pid": 1})
    assert "live" not in dict(stale_lanes(cfg, store, NOW))


def test_cadence_windows_default_to_a_no_op(cfg):
    """Ships as a mechanism. The numbers are a pre-registered policy decision."""
    from arb.heartbeat import lane_interval
    assert cfg["supervisor"]["cadence_windows"] == {}
    evening = datetime(2026, 7, 28, 20, 0)
    assert lane_interval(cfg, "watch", evening) == float(cfg["watch_interval_secs"])


def test_cadence_window_overrides_inside_its_hours(cfg):
    from arb.heartbeat import lane_interval
    c = {**cfg, "supervisor": {**cfg["supervisor"],
                               "cadence_windows": {"watch": [[19, 23, 20]]}}}
    assert lane_interval(c, "watch", datetime(2026, 7, 28, 19, 0)) == 20.0
    assert lane_interval(c, "watch", datetime(2026, 7, 28, 22, 59)) == 20.0
    assert lane_interval(c, "watch", datetime(2026, 7, 28, 23, 0)) == float(c["watch_interval_secs"])
    assert lane_interval(c, "watch", datetime(2026, 7, 28, 5, 0)) == float(c["watch_interval_secs"])
    # a lane with no window is untouched
    assert lane_interval(c, "mapped", datetime(2026, 7, 28, 20, 0)) == \
        float(c["nonsports"]["watch_interval_secs"])


def test_lane_interval_without_a_clock_returns_the_base_cadence(cfg):
    """Every existing call site passes no clock and must keep the base cadence."""
    from arb.heartbeat import lane_interval
    c = {**cfg, "supervisor": {**cfg["supervisor"],
                               "cadence_windows": {"watch": [[0, 24, 5]]}}}
    assert lane_interval(c, "watch") == float(c["watch_interval_secs"])
