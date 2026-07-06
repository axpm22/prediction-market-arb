"""MODEL_FOUNDATIONS Phase D — one supervisor, one heartbeat, no stale processes.

Visibility + one rule, not orchestration (no Docker/systemd/queues). Every loop beats each cycle
into the operational `heartbeats` table; a per-process pid lock makes a second copy refuse to
double-run; the supervisor warns (loudly, once) when the working tree changes under a running
loop. `arb status` is the morning glance: who's alive, on what code, last beat/poll.
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .store import Store

LOCK_DIR = Path(".locks")


def git_head() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                             text=True, timeout=5)
        return out.stdout.strip() or None if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def is_dirty() -> bool:
    try:
        out = subprocess.run(["git", "status", "--porcelain"], capture_output=True,
                             text=True, timeout=5)
        return out.returncode == 0 and bool(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return False


def beat(store: Store, process: str, *, pid: int | None = None, polls_done: int = 0,
         interval_secs: int | None = None, head: str | None = None, dirty: bool | None = None,
         now: datetime | None = None) -> None:
    """UPSERT the process's heartbeat. Operational (UPDATE allowed): started_at is kept from the
    first beat, last_beat_at/polls_done advance each cycle."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    pid = pid if pid is not None else os.getpid()
    head = head if head is not None else git_head()
    dirty_i = int(is_dirty() if dirty is None else dirty)
    with store.connect() as con:
        con.executescript("CREATE TABLE IF NOT EXISTS heartbeats(process TEXT PRIMARY KEY, pid INTEGER NOT NULL, "
                          "git_head TEXT, dirty INTEGER NOT NULL DEFAULT 0, started_at TEXT NOT NULL, "
                          "last_beat_at TEXT NOT NULL, polls_done INTEGER NOT NULL DEFAULT 0, interval_secs INTEGER);")
        con.execute(
            """INSERT INTO heartbeats(process, pid, git_head, dirty, started_at, last_beat_at, polls_done, interval_secs)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(process) DO UPDATE SET
                   pid=excluded.pid, git_head=excluded.git_head, dirty=excluded.dirty,
                   last_beat_at=excluded.last_beat_at, polls_done=excluded.polls_done,
                   interval_secs=excluded.interval_secs""",
            (process, pid, head, dirty_i, now, now, polls_done, interval_secs),
        )


def newest_quote_age_secs(store, now: datetime | None = None) -> float | None:
    """Seconds since the most recent persisted quote — the true 'is data flowing?' signal. None
    if no quotes yet. Alarms on this catch silent outages (host asleep, DNS down, supervisor
    kill-loop) that heartbeats alone miss: a loop can 'beat' while persisting zero quotes."""
    now = now or datetime.now(timezone.utc)
    with store.connect() as con:
        try:
            row = con.execute("SELECT MAX(fetched_at) AS m FROM quotes").fetchone()
        except Exception:  # noqa: BLE001
            return None
    if not row or not row["m"]:
        return None
    last = datetime.fromisoformat(str(row["m"]).replace("Z", "+00:00"))
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last).total_seconds()


def data_stale_alarm(store, alarm_secs: int, now: datetime | None = None) -> str | None:
    """A loud one-liner when data collection has stalled (or None when fresh). Used by `arb status`
    and the daily report so a silent outage is never invisible."""
    age = newest_quote_age_secs(store, now)
    if age is None:
        return "⚠ DATA ALARM: no quotes persisted yet."
    if age > alarm_secs:
        return (f"⚠ DATA ALARM: newest quote is {int(age)}s old (> {alarm_secs}s) — "
                "data collection STALLED (check the supervisor / host sleep / connectivity).")
    return None


def read_heartbeats(store: Store) -> list[dict]:
    with store.connect() as con:
        con.executescript("CREATE TABLE IF NOT EXISTS heartbeats(process TEXT PRIMARY KEY, pid INTEGER NOT NULL, "
                          "git_head TEXT, dirty INTEGER NOT NULL DEFAULT 0, started_at TEXT NOT NULL, "
                          "last_beat_at TEXT NOT NULL, polls_done INTEGER NOT NULL DEFAULT 0, interval_secs INTEGER);")
        return [dict(r) for r in con.execute("SELECT * FROM heartbeats ORDER BY process")]


def _parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def is_stale(hb: dict, now: datetime | None = None, factor: int = 3) -> bool:
    """A loop is stale when it hasn't beaten in `factor`× its own interval (default 3×)."""
    now = now or datetime.now(timezone.utc)
    interval = hb.get("interval_secs") or 60
    return (now - _parse(hb["last_beat_at"])).total_seconds() > factor * interval


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    return True


def supervisor_stop_requested() -> bool:
    """The supervisor honors the same shutdown flags as the daemon: data/stop-all or
    data/stop-supervisor. Its watch children are one-shot (they exit each cycle), so the
    stop check lives HERE — it stops relaunching and terminates the current child. Without
    this, stop-all could not stop a supervised system (the documented safety control)."""
    return Path("data/stop-all").exists() or Path("data/stop-supervisor").exists()


def acquire_lock(process: str, pid: int | None = None, lock_dir: Path = LOCK_DIR) -> bool:
    """Per-process single-run lock. Returns False if a LIVE copy already holds it. Stale locks
    (dead pid) are reclaimed."""
    pid = pid if pid is not None else os.getpid()
    lock_dir.mkdir(exist_ok=True)
    path = lock_dir / f"{process}.pid"
    if path.exists():
        try:
            holder = int(path.read_text().strip())
        except (ValueError, OSError):
            holder = None
        if holder is not None and holder != pid and pid_alive(holder):
            return False
    path.write_text(str(pid))
    return True


def release_lock(process: str, lock_dir: Path = LOCK_DIR) -> None:
    (lock_dir / f"{process}.pid").unlink(missing_ok=True)


def supervisor_alive(lock_dir: Path = LOCK_DIR) -> bool:
    """True iff the supervisor's pid lock is held by a live process."""
    try:
        holder = int((lock_dir / "supervisor.pid").read_text().strip())
    except (OSError, ValueError):
        return False
    return pid_alive(holder)


def format_status(heartbeats: list[dict], now: datetime | None = None,
                  current_head: str | None = None, supervisor_up: bool | None = None) -> str:
    """Under the supervisor, watch runs as one-shot `watch --once` relaunches: its recorded child
    pid is EXPECTED to be dead between cycles, so pid-liveness alone must never mean DEAD.
    A fresh beat OR a live pid = ALIVE; stale beat with the supervisor lock held = STALE (it will
    relaunch); DEAD only when the beat is stale and no live supervisor holds the lock."""
    now = now or datetime.now(timezone.utc)
    if supervisor_up is None:
        supervisor_up = supervisor_alive()
    lines = [f"  {'process':<14}{'state':<8}{'pid':>7} {'code':<10}{'polls':>7}  last beat            interval"]
    for hb in heartbeats:
        fresh, running = not is_stale(hb, now), pid_alive(hb["pid"])
        state = "ALIVE" if (fresh or running) else ("STALE" if supervisor_up else "DEAD")
        code = str(hb.get("git_head") or "?") + ("*" if hb.get("dirty") else "")
        drift = "  <-- code changed" if current_head and hb.get("git_head") and hb["git_head"] != current_head else ""
        lines.append(f"  {hb['process']:<14}{state:<8}{hb['pid']:>7} {code:<10}"
                     f"{hb['polls_done']:>7}  {str(hb['last_beat_at'])[:19]}  {hb.get('interval_secs') or '?'}{drift}")
    if not heartbeats:
        lines.append("  (no heartbeats recorded — no loop has run yet)")
    return "\n".join(lines)


def run_supervisor(config, store: Store) -> int:
    """Start every enabled loop from config `processes` as a subprocess and restart each when it
    EXITS (one-shot `watch --once` cadence); a still-alive child is force-restarted only if it hangs
    past `supervisor.max_child_runtime_secs`. Heartbeat staleness NEVER triggers a restart — a
    healthy live cycle can outlast the staleness window, and killing it mid-fetch was an infinite
    kill-restart loop that collected zero quotes 2026-07-04..06. A working-tree change under a
    running loop warns once (no auto-restart — a rebuild is a human decision)."""
    import sys
    import time

    processes = {k: v for k, v in (config.supervisor_processes or {}).items() if v}
    if not processes:
        print("supervisor: no processes enabled in config `supervisor.processes`.")
        return 1
    if not acquire_lock("supervisor"):
        print("supervisor: another supervisor is already running (lock held). Exiting.")
        return 1
    start_head = git_head()
    children: dict[str, subprocess.Popen] = {}

    launched_at: dict[str, float] = {}

    def launch(name: str) -> None:
        cmd = config.supervisor_commands.get(name, name)
        children[name] = subprocess.Popen([sys.executable, "-m", "arb", *cmd.split()])
        launched_at[name] = time.monotonic()
        print(f"supervisor: launched {name} (pid {children[name].pid})")

    # A one-shot child (watch --once) beats only at the END of its cycle, and a full live cycle can
    # exceed the heartbeat-staleness window — so heartbeat staleness must NOT trigger a restart
    # (it would kill a healthy in-progress cycle before it persists, an infinite kill-restart loop
    # that collected zero quotes 2026-07-04..06). Restart only on actual EXIT; force-restart a still-
    # alive child only if it exceeds a generous max runtime (genuinely hung).
    max_runtime = getattr(config, "supervisor_max_child_runtime_secs", 600)
    if supervisor_stop_requested():
        print("supervisor: data/stop-all present — refusing to start (remove it to run).")
        release_lock("supervisor")
        return 1
    for name in processes:
        launch(name)
    try:
        while True:
            time.sleep(30)
            if supervisor_stop_requested():
                print("supervisor: stop-all requested — terminating loops and exiting.")
                for child in children.values():
                    child.poll() is None and child.terminate()
                release_lock("supervisor")
                return 0
            if git_head() != start_head:
                print(f"supervisor: WARNING working tree changed ({start_head} -> {git_head()}); "
                      "loops still run OLD code until you restart them.")
                start_head = git_head()
            for name, child in list(children.items()):
                exited = child.poll() is not None
                hung = (not exited) and (time.monotonic() - launched_at.get(name, 0.0)) > max_runtime
                if exited:
                    print(f"supervisor: {name} exited — restarting.")
                    launch(name)
                elif hung:
                    print(f"supervisor: {name} exceeded {max_runtime}s (hung) — killing + restarting.")
                    child.terminate()
                    launch(name)
    except KeyboardInterrupt:
        for child in children.values():
            child.terminate()
        release_lock("supervisor")
        return 0
