"""Supervisor + heartbeats (SPEC §13.2). Children are ONE-SHOT subprocesses,
relaunched on exit; a second supervisor refuses to double-run (.locks/); the
stop flag halts between cycles only; the supervisor owns the clock (scan_times,
settle_time, daily_report_time, db backup, weekly shape-check, monthly canary).
Heartbeat staleness alone never restarts a child — only the per-process runtime
cap force-kills (V2 killed healthy children for three days)."""
from __future__ import annotations

import math
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
              ("shape", str(cfg["supervisor"]["shape_time"])),
              ("canary", str(cfg["canary"]["time"]))]
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


def lane_offsets(cfg) -> dict[str, float]:
    """Phase offsets spread evenly INSIDE each lane's own period. A flat boot
    stagger aliases — 5 lanes x stagger_secs 15 = 60s = watch_interval_secs, so
    lane 5 landed on lane 1 — and it decayed anyway, because next_run was
    anchored to the previous spawn rather than to a grid (measured 2026-07-29:
    355 of 1,078 watch polls had a live poll start within 5s; live's 20s divides
    watch's 60s exactly, so that phase collision is permanent, not transient)."""
    enabled = [n for n, on in cfg["supervisor"]["processes"].items() if on]
    n = max(1, len(enabled))
    offsets = {}
    want = float(cfg["supervisor"]["stagger_secs"])
    for i, name in enumerate(enabled):
        interval = lane_interval(cfg, name)
        offsets[name] = interval * i / n
        if interval / n < want:
            log.warning("lane %s phase separation %.1fs < stagger_secs %s — "
                        "cadence too fast for %d lanes on one IP",
                        name, interval / n, want, n)
    return offsets


def next_grid_run(now: float, interval: float, offset: float) -> float:
    """The next absolute-grid slot strictly after `now`. Phase is a property of
    the grid, so a late supervisor tick can never push a lane out of alignment."""
    return (math.floor((now - offset) / interval) + 1) * interval + offset


def initial_next_run(cfg, now_mono: float) -> dict[str, float]:
    """Boot-time schedule: now + each lane's phase offset, so the first lane
    starts immediately and the rest are separated. NOT the next grid slot —
    that is strictly future, which would idle a 300s lane for up to 300s after
    boot and make `supervisor --once` spawn nothing at all. Lanes snap onto the
    absolute grid at their first re-arm; monotonic time has an arbitrary origin,
    so no grid phase is more correct than another."""
    return {name: now_mono + off for name, off in lane_offsets(cfg).items()}


def _base_lane_interval(cfg, name: str) -> float:
    """Each lane's cadence knob (values live in config, rule 4); anything
    unlisted runs at the watch cadence."""
    if name == "live":
        return float(cfg["live_lane"]["cadence_secs"])
    if name in ("negrisk", "political", "nonsports", "mapped"):
        return float(cfg["nonsports"]["watch_interval_secs"])
    if name == "verify":
        return float(cfg["verify"]["poll_secs"])
    if name == "whale":
        return float(cfg["whale"]["poll_secs"])
    if name == "whale-backfill":
        # its own process: ~11 min of paced calls per cycle (design §5 step 4)
        return float(cfg["whale"]["backfill"]["cadence_secs"])
    return float(cfg["watch_interval_secs"])


def lane_interval(cfg, name: str, now_ny: datetime | None = None) -> float:
    """Base cadence, optionally overridden by a time-of-day window. Callers that
    pass no clock always get the base — every pre-existing call site does, and
    lane_offsets/stale_lanes deliberately keep doing so: offsets are a fixed
    phase plan, and a silence alarm must not tighten just because the hour
    changed. Measured yield is strongly diurnal for watch (hr20: 482 flags/1k
    polls vs 3 flags across 1,816 polls in hrs 04-08) and perfectly flat for
    negrisk and mapped, which have no time-of-day at all."""
    base = _base_lane_interval(cfg, name)
    if now_ny is None:
        return base
    for start_h, end_h, secs in cfg["supervisor"]["cadence_windows"].get(name) or []:
        if int(start_h) <= now_ny.hour < int(end_h):
            return float(secs)
    return base


# supervisor process name -> polls.kind. verify writes annotations, not polls,
# and whale-backfill writes only to the tape: both are absent here on purpose,
# so the silence alarm never asks for a poll row they can never produce.
POLL_KIND = {"watch": "watch", "live": "live", "negrisk": "negrisk",
             "political": "political", "mapped": "mapped", "whale": "whale"}


def stale_lanes(cfg, store, now_utc: datetime) -> list[tuple[str, float]]:
    """Enabled lanes with no sign of life for heartbeat_stale_polls x their own
    cadence. A lane that has NEVER run is a cold start, not a silence.

    negrisk+political were dark 2026-07-17..25 unreported — reproduced
    2026-07-29 as a 232.2 h max inter-poll gap on both, while watch, live and
    mapped kept running and heartbeat_stale_polls (a whole-SUPERVISOR beat)
    stayed green throughout.

    Liveness is the newest of a poll row OR a heartbeat, because neither alone
    covers every lane: negrisk and political write polls but never beat, and a
    lane that ran and correctly found nothing to do writes no poll row at all
    (the live lane skips its poll on an empty shortlist). Polls measure work;
    heartbeats measure running. Silence means neither."""
    mult = float(cfg["heartbeat_stale_polls"])
    out = []
    for name, on in cfg["supervisor"]["processes"].items():
        kind = POLL_KIND.get(name)
        if not on or kind is None:
            continue
        last = store.query(
            """SELECT MAX(t) m FROM (
                 SELECT MAX(started_at) t FROM polls WHERE kind=?
                 UNION ALL
                 SELECT MAX(beat_at) t FROM heartbeats WHERE process=?)""",
            (kind, name))[0]["m"]
        if last is None:
            continue
        age = (now_utc - datetime.fromisoformat(last)).total_seconds()
        if age > mult * lane_interval(cfg, name):
            out.append((name, age))
    return out


def lane_status_lines(cfg, beats) -> list[tuple[str, str]]:
    """Every configured supervisor process as (name, status) pairs, for
    `arb status`/dashboard heartbeat summaries — whale lanes first (2026-09-04
    operator decision: PM whales only, for now; `sorted` is stable, so ties
    keep `supervisor.processes`' own order). A disabled lane reads as
    'off (whale-only mode)' rather than silently vanishing from the printout."""
    beat_by = {b["process"]: b for b in beats}
    order = sorted(cfg["supervisor"]["processes"].items(),
                   key=lambda kv: not kv[0].startswith("whale"))
    out = []
    for name, enabled in order:
        if not enabled:
            out.append((name, "off (whale-only mode)"))
            continue
        b = beat_by.get(name)
        out.append((name, f"last beat {b['beat_at']} sha={b['head_sha']}"
                    if b else "no beat yet"))
    return out


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

    def _notify(event: str, payload: dict | None = None, key=None) -> None:
        if event not in cfg["notify"]["events"]:
            return
        try:
            from arb.notify import post
            post(cfg, event, payload or {}, key=key)
        except Exception as e:  # noqa: BLE001 — a notifier failure must never crash the supervisor
            log.warning("notify %s failed: %s", event, e)

    log.info("supervisor up")
    _notify("supervisor_started")
    # children inherit the supervisor's --db/--config, or data silently splits
    flags = (["--db", db] if db else []) + (["--config", config] if config else [])
    children: dict[str, subprocess.Popen] = {}
    started: dict[str, float] = {}
    # suppress the boot-time burst: slots long past are pre-marked as fired (H9)
    last_fired: dict[str, str] = initial_last_fired(cfg, datetime.now(NY))
    next_run: dict[str, float] = initial_next_run(cfg, time.monotonic())
    offsets = lane_offsets(cfg)   # fixed phase plan; re-armed against the grid
    # read-only handle for the per-lane silence alarm; the lanes remain the sole
    # writers (§13.3 one-writer-per-table). Checked at most once a minute, not
    # on every 2s tick.
    from arb.cli import DEFAULT_DB
    from arb.store import Store
    silence_store, next_silence_check = Store(db or DEFAULT_DB), 0.0
    scheduled: list[subprocess.Popen] = []  # one-shot clock children, reaped each tick

    def _spawn(*cmd):
        scheduled.append(subprocess.Popen([sys.executable, "-m", "arb", *flags, *cmd]))

    try:
        while True:
            if STOP_FLAG.exists():
                log.info("stop-all flag present — halting cleanly")
                for name, proc in children.items():
                    _terminate(proc, _cap(cfg, name))
                for proc in scheduled:
                    _terminate(proc, _cap(cfg, "default"))
                _notify("supervisor_stopped", {"reason": "stop-all"})
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
                    # absolute grid, never now+interval: the loop's 2s tick
                    # overshoots by 0-2s and that error used to accumulate
                    next_run[name] = next_grid_run(
                        now, lane_interval(cfg, name, now_ny), offsets[name])

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
            if now_ny.weekday() == 0 and _due(
                    last_fired, "shape", str(cfg["supervisor"]["shape_time"]), now_ny):
                _spawn("shape-check")
            if cfg["canary"]["enabled"] and \
                    now_ny.day == int(cfg["canary"]["day_of_month"]) and \
                    _due(last_fired, "canary", str(cfg["canary"]["time"]), now_ny):
                _spawn("canary")

            if now >= next_silence_check:
                next_silence_check = now + 60.0
                try:
                    for lane, age in stale_lanes(cfg, silence_store,
                                                 datetime.now(timezone.utc)):
                        log.critical("lane %s silent for %.0fs (%.2f h) — no "
                                     "poll rows and no heartbeat", lane, age,
                                     age / 3600)
                        _notify("lane_silent", {"lane": lane, "age_secs": age}, key=lane)
                except Exception as e:  # noqa: BLE001 — an alarm must never kill the loop
                    log.warning("stale-lane check failed: %s", e)

            if once:
                for name, proc in children.items():
                    _terminate(proc, _cap(cfg, name))
                for proc in scheduled:
                    _terminate(proc, _cap(cfg, "default"))
                _notify("supervisor_stopped", {"reason": "once"})
                return 0
            time.sleep(2)
    finally:
        release_lock()
