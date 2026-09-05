"""Child-process log capture + exit bookkeeping for arb/heartbeat.py — split
out to keep heartbeat.py under ~300 lines (it was already over).

Incident 2026-09-05: heartbeat.py spawned every child with
subprocess.Popen([sys.executable, "-m", "arb", ...]) and no stdout/stderr
redirection, so a crashing one-shot child's traceback went to the
supervisor's own stdout — nowhere logs/ ever looked — and nothing recorded
the non-zero exit. This module gives every child's stdout+stderr a file
(supervisor.child_log_dir) and turns a bad exit into a WARNING (CRITICAL on
a repeated streak), persisted append-only (rule 9 style) so `arb status` and
the supervisor's own alarm can both read it back.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import IO

from arb.logutil import get_logger, purge_stale, resolve_log_path

log = get_logger("arb.supervisor", "logs/supervisor.log")


def log_path(cfg, name: str) -> Path:
    """Where `name`'s captured stdout+stderr lives."""
    return resolve_log_path(cfg["supervisor"]["child_log_dir"]) / f"{name}.log"


def open_child_log(cfg, name: str) -> IO:
    """Append-mode file for `name`'s stdout+stderr. Creates the directory and
    purges sibling files a disabled/removed lane left behind (log_retention_
    days) — cheap (one directory listing), so doing it on every spawn rather
    than on its own schedule is simplest."""
    p = log_path(cfg, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    purge_stale(p.parent, int(cfg["log_retention_days"]))
    return open(p, "ab")


def tail_lines(path: Path, n: int) -> str:
    """Last `n` lines of `path` ("" if it doesn't exist yet). Reads only the
    final 64 KiB, matching dashboard.py's own tail — these are raw child
    stdout/stderr, not log-formatted."""
    if not path.exists():
        return ""
    with path.open("rb") as f:
        f.seek(max(0, path.stat().st_size - 65536))
        text = f.read().decode("utf-8", "replace")
    return "\n".join(text.splitlines()[-n:])


def _consecutive_failures(store, process: str) -> int:
    """Failures in a row for `process`, newest first, from the append-only
    child_exits log — 0 if the latest exit was clean or there is no history.
    Capped at a 50-row lookback: enough for any realistic alarm threshold."""
    n = 0
    for r in store.query(
            "SELECT rc FROM child_exits WHERE process=? ORDER BY id DESC LIMIT 50",
            (process,)):
        if r["rc"] == 0:
            break
        n += 1
    return n


def record_exit(cfg, store, name: str, rc: int, runtime: float,
                child_logfile: Path, killed: bool = False) -> None:
    """Append one child_exits row for `name`'s just-reaped exit. A clean exit
    (rc==0, not killed) is persisted silently; a bad one logs a WARNING with
    the process, rc, runtime and the last ~20 lines of its captured output —
    CRITICAL once the SAME process has failed supervisor.child_fail_alarm
    times in a row (2026-09-05: whale-backfill crashed at the same step every
    cycle for ~90 min with nothing in logs/)."""
    store.insert("child_exits", {
        "process": name, "exited_at": datetime.now(timezone.utc).isoformat(),
        "rc": rc, "runtime_secs": runtime, "killed": int(killed)})
    if rc == 0 and not killed:
        return
    streak = _consecutive_failures(store, name)
    msg = "child %s exited rc=%s after %.1fs%s\n%s" % (
        name, rc, runtime, " (killed: past runtime cap)" if killed else "",
        tail_lines(child_logfile, 20))
    if streak >= int(cfg["supervisor"]["child_fail_alarm"]):
        log.critical(msg)
    else:
        log.warning(msg)


def last_status(store, process: str) -> tuple[int, int] | None:
    """(last rc, consecutive-failure streak) for `process` — None with no
    exit history yet. `arb status`."""
    rows = store.query(
        "SELECT rc FROM child_exits WHERE process=? ORDER BY id DESC LIMIT 1",
        (process,))
    if not rows:
        return None
    return rows[0]["rc"], _consecutive_failures(store, process)
