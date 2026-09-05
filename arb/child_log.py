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

Fix pass 2026-09-05 (review of 9cb356b, C1): a child handed the log file's
fd directly writes its raw, unredacted bytes to disk — and a `requests`
exception embeds the Odds API key in the URL (rule 2 / SPEC §6.4). `spawn`
below pipes the child's stdout+stderr instead and drains it through a
daemon thread that line-redacts before writing, so the on-disk file is never
allowed to carry a raw query string in the first place.

Fix pass 2 2026-09-05 (review of 798aae6): I1 the log file was block-
buffered, so tailing a hung child mid-run — the reason this file exists —
showed nothing until it exited; opened unbuffered now. I2 a DB error out of
record_exit used to skip closing the logfile; reap's close is now a
`finally`. I3 both supervisor shutdown paths terminated children without
reaping them (no child_exits row, drain thread unjoined, fd never closed);
reap is now also safe to call speculatively on a still-running proc (M5),
so heartbeat.py's shutdown helper can call it unconditionally. M8 the log
filename is now date-stamped so an active lane's file goes inactive daily
and the existing retention purge can reclaim it (previously it never did,
since an ACTIVE file's mtime never stops moving).
"""
from __future__ import annotations

import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import IO

from arb.logutil import get_logger, purge_stale, redact, resolve_log_path

log = get_logger("arb.supervisor", "logs/supervisor.log")

# reap()'s bound on joining a child's drain thread (M4: was a bare inline
# 5.0) — never let a wedged reader hang the 2s supervisor tick. A timeout
# here means the join gave up before the thread finished writing, so
# record_exit's tail-lines read right after can be truncated.
_DRAIN_JOIN_SECS = 5.0


def log_path(cfg, name: str) -> Path:
    """Where `name`'s captured stdout+stderr lives — date-stamped
    (<name>.<YYYY-MM-DD>.log, UTC day) so each day's file goes inactive on
    its own and log_retention_days' purge (mtime-based, and an ACTIVE
    file's mtime never stops moving) can actually reclaim it (M8:
    logs/children/watch.log grew ~0.5-2 MB/h forever)."""
    day = datetime.now(timezone.utc).date().isoformat()
    return resolve_log_path(cfg["supervisor"]["child_log_dir"]) / f"{name}.{day}.log"


def open_child_log(cfg, path: Path) -> IO:
    """Unbuffered (I1: block buffering meant the on-disk file stayed empty
    until the child exited, since _drain only flushes at EOF — defeating
    the reason this file exists, tailing a HUNG child mid-run) append-mode
    file at `path`. Creates the directory and purges sibling files a
    disabled/removed lane or a previous day (M8) left behind
    (log_retention_days) — cheap (one directory listing), so doing it on
    every spawn rather than on its own schedule is simplest."""
    path.parent.mkdir(parents=True, exist_ok=True)
    purge_stale(path.parent, int(cfg["log_retention_days"]))
    return open(path, "ab", buffering=0)


def _drain(pipe, logfile) -> None:
    """Reader-thread body: redact each line as it arrives off the child's
    piped stdout+stderr and append it to logfile. Runs until the pipe hits
    EOF (the child closing its end on exit) — a daemon thread, so a wedged
    read can never keep the supervisor process alive on its own; `reap`
    below still joins with a timeout so a wedged read can't silently leak a
    thread/fd on every spawn either. No explicit flush: logfile is opened
    unbuffered (I1), so every write already lands on disk immediately."""
    try:
        for raw in pipe:
            logfile.write(redact(raw.decode("utf-8", "replace")).encode("utf-8"))
    except (ValueError, OSError):
        pass  # logfile/pipe closed out from under us at shutdown — fine, best-effort
    finally:
        pipe.close()


class Capture:
    """One spawned child: its Popen, the drain thread piping its (redacted)
    stdout+stderr into logpath, and the bookkeeping `reap` needs — one
    object instead of the three parallel dicts/monkey-patched Popen attrs
    heartbeat.py used to carry per child. logpath is resolved ONCE at spawn
    (M8: log_path is date-stamped), so a reap landing after UTC midnight
    still targets the SAME file the child actually wrote to, not a fresh
    empty one for the new day."""
    __slots__ = ("proc", "thread", "logfile", "name", "started", "logpath")

    def __init__(self, proc, thread, logfile, name, started, logpath):
        self.proc, self.thread, self.logfile = proc, thread, logfile
        self.name, self.started, self.logpath = name, started, logpath


def spawn(cfg, name: str, argv: list) -> Capture:
    """Spawn argv as `name`'s child (the child_exits key and the log
    filename's stem), capturing stdout+stderr through a pipe drained by
    `_drain` into name's log file — never hand the child the log file's fd
    directly (see module docstring)."""
    path = log_path(cfg, name)
    logfile = open_child_log(cfg, path)
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    thread = threading.Thread(target=_drain, args=(proc.stdout, logfile), daemon=True)
    thread.start()
    return Capture(proc, thread, logfile, name, time.monotonic(), path)


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


def reap(cfg, store, cap: Capture, killed: bool = False) -> None:
    """Everything heartbeat.py's three near-identical reap sites used to do
    inline: join cap's drain thread (bounded by _DRAIN_JOIN_SECS, so a
    wedged reader can never hang the 2s supervisor tick or leak across
    thousands of spawns — though a timed-out join means the file isn't
    fully drained yet, so record_exit's tail read right after can be
    truncated), THEN record_exit (which tails the file for its WARNING/
    CRITICAL — the join must land first, or it can read a still-draining
    file), then close the logfile — even if record_exit itself raises (I2:
    a DB error used to skip the close and propagate straight out of the
    supervisor tick, leaking the fd on every crash it couldn't even
    record).

    A no-op on a still-running proc (M5): its pipe won't hit EOF until it
    exits, so joining the drain thread here would block the caller forever.
    Callers (both supervisor shutdown paths, I3) can therefore call this
    unconditionally right after terminating a child, without first having
    to prove it actually exited."""
    if cap.proc.poll() is None:
        return
    cap.thread.join(timeout=_DRAIN_JOIN_SECS)
    try:
        record_exit(cfg, store, cap.name, cap.proc.returncode,
                   time.monotonic() - cap.started, cap.logpath, killed=killed)
    finally:
        cap.logfile.close()


def last_status(store, process: str) -> tuple[int, int] | None:
    """(last rc, consecutive-failure streak) for `process` — None with no
    exit history yet. `arb status`."""
    rows = store.query(
        "SELECT rc FROM child_exits WHERE process=? ORDER BY id DESC LIMIT 1",
        (process,))
    if not rows:
        return None
    return rows[0]["rc"], _consecutive_failures(store, process)
