# FIX: status.sh false "not running" + heartbeat false-DEAD — verify, test, commit

Read `CLAUDE.md` first. Hard rules that bind here: paper only; no Odds API credits in tests
(fixtures only); `flat_bets` append-only; no magic numbers; `Decimal` money. This task
touches no money path — it is observability + shell only.

## Context

`status.sh` always reported "not running" even when the system was healthy. Two stacked
causes, both already diagnosed:

1. **status.sh (lines 12–18)** ps-grepped for pre-supervisor process names
   (`arb daemon`, `arb.experimental focus/verify/hot/political`). Under the Phase D
   supervisor (`arb/heartbeat.py::run_supervisor`), config `supervisor.processes` enables
   only `watch`, run as a short-lived `watch --once` relaunched ~every cycle — a name the
   list never contained and a process ps can rarely catch. The correct surface is
   `python -m arb status` (reads the `heartbeats` table; also prints the
   `data_stale_alarm` freshness line).
2. **`arb/heartbeat.py::format_status`** had the same bug one layer down: state keyed off
   `pid_alive(child_pid)`, but a one-shot child's pid is EXPECTED to be dead between
   cycles — so `arb status` itself printed DEAD for a healthy loop. Correct semantics:
   fresh beat OR live pid → ALIVE; stale beat while the supervisor lock
   (`.locks/supervisor.pid`) is held by a live pid → STALE (it will relaunch); DEAD only
   when stale AND no live supervisor. Also: the pid/code columns rendered collided
   (`94001e057aa0*`).

## Current tree state — verify, don't redo

A working-tree fix **already exists, uncommitted** (authored 2026-07-06 in a Cowork
session; the sandbox there could not run pytest or see host pids, so it is unverified):

- `status.sh`: the ps-grep block replaced with `$PY -m arb status`; plus
  `PY=.venv/bin/python; [ -x "$PY" ] || PY=python3` fallback.
- `arb/heartbeat.py`: new `supervisor_alive(lock_dir)` helper; `format_status` gains
  `supervisor_up: bool | None = None` and the ALIVE/STALE/DEAD semantics above; pid/code
  column spacing fixed.
- `tests/test_heartbeat.py`: two new tests —
  `test_format_status_one_shot_child_states` (state matrix incl. fresh-beat+dead-pid →
  ALIVE, stale+supervisor → STALE, stale+no-supervisor → DEAD, live-pid+stale-beat →
  ALIVE, column-spacing assert) and `test_supervisor_alive_reads_lock`.

Read each diff (`git diff`, `git status`) and confirm it matches the description. Fix
anything missing or wrong; do not rewrite what is already correct.

## Task

1. Run the **full** `pytest`. All green, including the two new heartbeat tests. Do not
   weaken any existing test.
2. Live check on this machine (the supervisor should be running; if not, start it per
   `prompts/queue/CONTINUATION.md` §1): run `./status.sh` and confirm
   - the processes section prints the heartbeat table with `watch` as **ALIVE** (or an
     honest STALE if the last beat is > 3× interval — never the old blanket "not running"),
   - the `data:` freshness line appears,
   - the sqlite P&L section and CLV section still render unchanged.
3. Edge check: `touch data/stop-all`, wait one supervisor tick (~30 s) until it exits,
   run `./status.sh` again → watch should read **DEAD** (stale beat, no live supervisor
   lock). Then remove `data/stop-all` and restart the supervisor
   (`nohup env PYTHONUNBUFFERED=1 .venv/bin/python -m arb supervisor >> logs/supervisor.log 2>&1 &`),
   confirm status returns to ALIVE within a cycle.
4. Commit (one commit): message explains the two-layer false-negative and the new state
   semantics; paste the `./status.sh` output into the commit body. If
   `prompts/queue/CONTINUATION.md` still lists the "status shows watch DEAD between
   cycles" known-artifact note (§1) / Phase-D polish item (§4), mark those lines resolved
   with a one-line reference to this commit.

## Acceptance (show all output)

1. `pytest` full-suite green.
2. `./status.sh` with the supervisor up: watch ALIVE, freshness line present.
3. `./status.sh` after stop-all: watch DEAD; after restart: ALIVE again.
4. `git status` clean after the commit.

## Out of scope

- No changes to `run_supervisor`, the beat cadence, or any detection/money path.
- No new config keys (the `data_stale_alarm_secs` wiring already exists).
- Do not touch the sqlite P&L / CLV sections of status.sh.
