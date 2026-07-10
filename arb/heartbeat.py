"""Supervisor + heartbeats (SPEC §13.2). Children are ONE-SHOT subprocesses,
relaunched on exit; a second supervisor refuses to double-run (.locks/); the
stop flag halts between cycles only; the supervisor owns the clock (scan_times,
settle_time, daily_report_time, db backup, weekly shape-check, monthly canary).
Heartbeat staleness alone never restarts a child — only the per-process runtime
cap force-kills (V2 killed healthy children for three days)."""
from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from arb.logutil import get_logger

log = get_logger("arb.supervisor", "logs/supervisor.log")
NY = ZoneInfo("America/New_York")
LOCK_DIR = Path(".locks/supervisor")
STOP_FLAG = Path("data/stop-all")


def beat(store, process: str, head_sha: str) -> None:
    import os
    store.insert("heartbeats", {
        "process": process, "beat_at": datetime.now(timezone.utc).isoformat(),
        "head_sha": head_sha, "pid": os.getpid()})


def acquire_lock() -> bool:
    try:
        LOCK_DIR.mkdir(parents=True, exist_ok=False)
        (LOCK_DIR / "pid").write_text(str(__import__("os").getpid()))
        return True
    except FileExistsError:
        pid = (LOCK_DIR / "pid").read_text().strip() if (LOCK_DIR / "pid").exists() else "?"
        try:
            __import__("os").kill(int(pid), 0)
            return False  # live supervisor holds the lock
        except (ProcessLookupError, ValueError):
            log.warning("stale supervisor lock (pid %s gone) — reclaiming", pid)
            (LOCK_DIR / "pid").write_text(str(__import__("os").getpid()))
            return True
        except PermissionError:
            return False


def release_lock() -> None:
    if LOCK_DIR.exists():
        for p in LOCK_DIR.iterdir():
            p.unlink()
        LOCK_DIR.rmdir()


def _cap(cfg, name: str) -> float:
    caps = cfg["supervisor"]["max_child_runtime_secs"]
    return float(caps.get(name, caps["default"]))


def _due(last_fired: dict, key: str, hhmm: str, now_ny: datetime) -> bool:
    mark = f"{now_ny.date()}:{key}"
    if last_fired.get(key) == mark:
        return False
    h, m = map(int, hhmm.split(":"))
    if (now_ny.hour, now_ny.minute) >= (h, m):
        last_fired[key] = mark
        return True
    return False


def _schedule(cfg) -> list[tuple[str, str]]:
    """Every daily supervisor-owned slot as (key, HH:MM) — the single source of
    truth for both firing and startup burst-suppression (H9)."""
    slots = [(f"scan-{t}", str(t)) for t in cfg["scan_times"]]
    slots += [("settle", str(cfg["settle_time"])),
              ("report", str(cfg["daily_report_time"])),
              ("backup", str(cfg["db_backup"]["time"])),
              ("archive", str(cfg["archive_time"])),
              ("shape", "05:00"), ("canary", "05:30")]
    return slots


def initial_last_fired(cfg, now_ny: datetime) -> dict:
    """Pre-mark slots more than schedule_catchup_grace_mins in the past as already
    fired, so a supervisor started (or restarted) late does NOT burst-fire the
    whole day's scans/settle/report/backup at once (H9). Slots missed within the
    grace stay unmarked so _due catches them up exactly once; future slots fire
    when due."""
    grace = float(cfg["supervisor"]["schedule_catchup_grace_mins"]) * 60
    marks: dict[str, str] = {}
    for key, hhmm in _schedule(cfg):
        h, m = map(int, hhmm.split(":"))
        slot = now_ny.replace(hour=h, minute=m, second=0, microsecond=0)
        if (now_ny - slot).total_seconds() > grace:
            marks[key] = f"{now_ny.date()}:{key}"
    return marks


def _reap_finished(procs: list) -> list:
    """Return the still-running children; poll() reaps any that have exited, so
    one-shot scheduled children never linger as zombies (H9)."""
    return [p for p in procs if p.poll() is None]


def _terminate(proc, timeout: float) -> None:
    """Wait up to timeout for a child; on a hung child, kill and reap instead of
    letting TimeoutExpired crash the supervisor mid-shutdown (H9)."""
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def run_supervisor(cfg, once: bool = False, db: str | None = None,
                   config: str | None = None) -> int:
    if not acquire_lock():
        log.critical("another supervisor is running; refusing to double-run")
        return 1
    log.info("supervisor up")
    # children inherit the supervisor's --db/--config, or data silently splits
    flags = (["--db", db] if db else []) + (["--config", config] if config else [])
    children: dict[str, subprocess.Popen] = {}
    started: dict[str, float] = {}
    # suppress the boot-time burst: slots long past are pre-marked as fired (H9)
    last_fired: dict[str, str] = initial_last_fired(cfg, datetime.now(NY))
    next_run: dict[str, float] = {}
    scheduled: list[subprocess.Popen] = []  # one-shot clock children, reaped each tick

    def _spawn(*cmd):
        scheduled.append(subprocess.Popen([sys.executable, "-m", "arb", *flags, *cmd]))

    def _interval(name: str) -> float:
        if name == "live":
            return float(cfg["live_lane"]["cadence_secs"])
        return float(cfg["watch_interval_secs"])
    try:
        while True:
            if STOP_FLAG.exists():
                log.info("stop-all flag present — halting cleanly")
                for name, proc in children.items():
                    _terminate(proc, _cap(cfg, name))
                for proc in scheduled:
                    _terminate(proc, _cap(cfg, "default"))
                return 0
            now = time.monotonic()
            now_ny = datetime.now(NY)
            scheduled[:] = _reap_finished(scheduled)  # reap finished one-shots (H9)

            # one-shot loop children on their cadence
            for name, enabled in cfg["supervisor"]["processes"].items():
                if not enabled:
                    continue
                proc = children.get(name)
                if proc is not None:
                    if proc.poll() is None:
                        if now - started[name] > _cap(cfg, name):
                            log.critical("child %s past runtime cap — force restart",
                                         name)
                            proc.kill()
                            proc.wait()  # reap the killed child, don't leave a zombie (H9)
                            children.pop(name)
                        continue
                    children.pop(name)
                if now >= next_run.get(name, 0):
                    cmd = cfg["supervisor"]["commands"][name].split()
                    children[name] = subprocess.Popen(
                        [sys.executable, "-m", "arb", *flags, *cmd])
                    started[name] = now
                    next_run[name] = now + _interval(name)

            # supervisor-owned clock (the `schedule` dependency is dropped); every
            # one-shot goes through _spawn so it is tracked and reaped (H9)
            for t in cfg["scan_times"]:
                if _due(last_fired, f"scan-{t}", str(t), now_ny):
                    _spawn("scan")
            if _due(last_fired, "settle", str(cfg["settle_time"]), now_ny):
                _spawn("settle")
            if _due(last_fired, "report", str(cfg["daily_report_time"]), now_ny):
                _spawn("report")
            if _due(last_fired, "backup", str(cfg["db_backup"]["time"]), now_ny):
                _spawn("backup")
            if _due(last_fired, "archive", str(cfg["archive_time"]), now_ny):
                _spawn("archive")
            if now_ny.weekday() == 0 and _due(last_fired, "shape", "05:00", now_ny):
                _spawn("shape-check")
            if cfg["canary"]["enabled"] and \
                    now_ny.day == int(cfg["canary"]["day_of_month"]) and \
                    _due(last_fired, "canary", "05:30", now_ny):
                _spawn("canary")

            if once:
                for name, proc in children.items():
                    _terminate(proc, _cap(cfg, name))
                for proc in scheduled:
                    _terminate(proc, _cap(cfg, "default"))
                return 0
            time.sleep(2)
    finally:
        release_lock()
