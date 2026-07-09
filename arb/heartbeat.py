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


def run_supervisor(cfg, once: bool = False) -> int:
    if not acquire_lock():
        log.critical("another supervisor is running; refusing to double-run")
        return 1
    log.info("supervisor up")
    children: dict[str, subprocess.Popen] = {}
    started: dict[str, float] = {}
    last_fired: dict[str, str] = {}
    next_run: dict[str, float] = {}

    def _interval(name: str) -> float:
        if name == "live":
            return float(cfg["live_lane"]["cadence_secs"])
        return float(cfg["watch_interval_secs"])
    try:
        while True:
            if STOP_FLAG.exists():
                log.info("stop-all flag present — halting cleanly")
                for name, proc in children.items():
                    proc.wait(timeout=_cap(cfg, name))
                return 0
            now = time.monotonic()
            now_ny = datetime.now(NY)

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
                        continue
                    children.pop(name)
                if now >= next_run.get(name, 0):
                    cmd = cfg["supervisor"]["commands"][name].split()
                    children[name] = subprocess.Popen([sys.executable, "-m", "arb", *cmd])
                    started[name] = now
                    next_run[name] = now + _interval(name)

            # supervisor-owned clock (the `schedule` dependency is dropped)
            for t in cfg["scan_times"]:
                if _due(last_fired, f"scan-{t}", str(t), now_ny):
                    subprocess.Popen([sys.executable, "-m", "arb", "scan"])
            if _due(last_fired, "settle", str(cfg["settle_time"]), now_ny):
                subprocess.Popen([sys.executable, "-m", "arb", "settle"])
            if _due(last_fired, "report", str(cfg["daily_report_time"]), now_ny):
                subprocess.Popen([sys.executable, "-m", "arb", "report"])
            if _due(last_fired, "backup", str(cfg["db_backup"]["time"]), now_ny):
                subprocess.Popen([sys.executable, "-m", "arb", "backup"])
            if _due(last_fired, "archive", str(cfg["archive_time"]), now_ny):
                subprocess.Popen([sys.executable, "-m", "arb", "archive"])
            if now_ny.weekday() == 0 and _due(last_fired, "shape", "05:00", now_ny):
                subprocess.Popen([sys.executable, "-m", "arb", "shape-check"])
            if cfg["canary"]["enabled"] and \
                    now_ny.day == int(cfg["canary"]["day_of_month"]) and \
                    _due(last_fired, "canary", "05:30", now_ny):
                subprocess.Popen([sys.executable, "-m", "arb", "canary"])

            if once:
                for name, proc in children.items():
                    proc.wait(timeout=_cap(cfg, name))
                return 0
            time.sleep(2)
    finally:
        release_lock()
