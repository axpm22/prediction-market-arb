# SETTLEMENT_RUNBOOK.md — making sure bets settle when they should

Goal: every bet whose event has finished should become `SETTLED` (WON/LOST/VOID) within a
short, known delay — never orphaned in `OPEN`. This runbook is the operating procedure plus
the 8-hour live test that proves it works.

---

## 1. Why bets orphan (the failure we are fixing)

A bet settles only if a settlement run sees its final score while that score is still
reachable. Two things broke that before:

1. **The Odds API `/scores` window is 3 days max.** A game older than 3 days can never be
   settled from the Odds API again.
2. **Settlement ran at most once a day, and the daemon had uptime gaps.** Any game that
   finished during a gap aged past the 3-day window and orphaned forever.

Result: 352 of 678 pending EV bets became permanently unsettleable from the Odds API.

## 2. The architecture that prevents it (already in the code)

- **Two sources.** `settle` (Odds API, `daysFrom=3`) + `settle-backfill` (MLB Stats API —
  free, keyless, **no lookback window**, all history). The MLB Stats source can always reach
  any past game, so nothing is ever permanently orphaned.
- **Settle every cycle, not once a day.** The daemon now runs `settle_backfill_mlb` on every
  watch cycle (free, so no credit cost). A finished game settles within one cycle
  (~`watch_interval_secs`).
- **Isolated failures.** Each source runs in its own try/except and logs success/failure, so
  one failing source can never silently block the other (the old silent-failure trap).

## 3. The rule of thumb

> A bet should be `SETTLED` within **one watch cycle after its game goes Final**, and never
> more than a few hours after the scheduled game end. Anything `OPEN` whose event ended more
> than ~6 hours ago is an **orphan** and must be investigated.

## 4. How to ensure settlement, day to day

### Preferred: run the daemon (auto-settles every cycle)
```
cd ~/Downloads/Codex/ArbitrageModel && source .venv/bin/activate
python -m arb daemon
```
The daemon: watches every cycle, runs the free MLB backfill every cycle, runs the Odds API
settle + report on their daily schedule. Keep it running; if the machine sleeps, that's an
uptime gap — see §7.

### Manual / cron fallback (if not running the daemon continuously)
Run settlement at least every few hours so nothing approaches the 3-day Odds API edge:
```
python -m arb settle            # Odds API (≤3-day games)
python -m arb settle-backfill   # MLB Stats (everything, no window)
```
A cron entry every 2 hours is plenty:
```
0 */2 * * *  cd ~/Downloads/Codex/ArbitrageModel && ./.venv/bin/python -m arb settle-backfill >> logs/settle.log 2>&1
```

## 5. Monitoring — the orphan check (run this to KNOW it's working)

Until the `settle-health` command lands (§8), use this one-liner. It prints how many open
bets are overdue (event date already past) and the oldest overdue date:

```
./.venv/bin/python - <<'PY'
import sqlite3, json, datetime as dt
c=sqlite3.connect('data/arb.sqlite')
today=dt.date.today()
rows=c.execute("select legs_json from flat_bets where kind in('EV','ARB') and status in('OPEN','CONFIRMED')").fetchall()
overdue=[]; total=len(rows)
for (legs,) in rows:
    ek=json.loads(legs)[0].get('event_key','')
    parts=ek.split(':')
    if len(parts)>=2:
        d=dt.date.fromisoformat(parts[1])
        if d < today: overdue.append(d)          # game day already passed -> should be settled
print(f"open bets: {total} | OVERDUE (event day in the past, still open): {len(overdue)}")
if overdue: print(f"oldest overdue event day: {min(overdue)}  (investigate if not 0)")
last=c.execute("select max(settled_at) from flat_bets where status='SETTLED'").fetchone()[0]
print(f"last settlement timestamp: {last}")
PY
```
**Healthy = OVERDUE is 0 (or only today's not-yet-final games).** A non-zero, non-shrinking
overdue count means settlement is not keeping up — go to §7.

## 6. The 8-hour live test (proves settlement keeps up)

Purpose: confirm that over a live run, (a) the existing backlog drains to zero, and (b) every
game that finishes during the window settles within one cycle.

### Before you start
1. Apply the §8 code change (`settle-health` command) if doing it; not required, the §5
   one-liner works.
2. Set a short cycle so you get many settle passes in 8 hours, e.g. in `config.yaml`:
   `watch_interval_secs: 300` (5 min). Note the value you used.
3. Record a **baseline** and save it:
   ```
   ./.venv/bin/python -m arb report --daily > logs/test_baseline_report.txt
   # then run the §5 orphan check and paste its output into logs/test_notes.txt
   ```

### Start the run
```
mkdir -p logs
nohup ./.venv/bin/python -m arb daemon >> logs/daemon_8h_test.log 2>&1 &
echo $! > logs/daemon_8h_test.pid     # save the PID so you can stop it cleanly
date >> logs/test_notes.txt
```

### Checkpoints — every ~1 hour, run the §5 orphan check and record the line
Watch three things over the 8 hours:
- **Backlog drains:** OVERDUE should drop to ~0 within the first 1–2 cycles (the backfill
  clears the old orphans immediately) and stay there.
- **New finals settle:** as MLB games end during the window, the daemon log shows
  `daemon: settled N MLB bets via MLB Stats backfill.` lines. The OPEN count for finished
  games should not accumulate.
- **No source silently dies:** grep the log for failures:
  ```
  grep -iE "settle.*failed|backfill failed|credits below|abort" logs/daemon_8h_test.log
  ```

### Stop cleanly after 8 hours
```
kill "$(cat logs/daemon_8h_test.pid)"
```

### Success criteria (all must hold)
- [ ] OVERDUE orphan count is **0** at the end (no finished game left OPEN).
- [ ] Every MLB game that went Final during the window is `SETTLED` (spot-check 3 games by
      event_key against the box score).
- [ ] The daemon log shows recurring `settled N MLB bets` lines — settlement actually fired
      each relevant cycle, not just once.
- [ ] No `settle ... failed` / credit-abort lines in the log.
- [ ] `last settlement timestamp` (from §5) is recent (within one cycle of the stop time).

### If a criterion fails → §7.

## 7. Troubleshooting (what to do when settlement lags)

| Symptom | Likely cause | Fix |
|---|---|---|
| OVERDUE count > 0 and not shrinking | daemon not running / machine slept | restart daemon; check uptime; add cron fallback (§4) |
| A specific game stays OPEN, others settle | team-name mismatch vs `aliases.json` | add the alias mapping; re-run `settle-backfill` |
| Two same-day games vs same teams stay OPEN | doubleheader (left OPEN on purpose) | settle by hand: `python -m arb settle --manual EVENT_KEY OUTCOME` |
| `credits below ... abort` in log | Odds API credits low | settlement still works via `settle-backfill` (free); top up Odds API only for consensus |
| `settle ... failed` once, then recovers | transient network blip | fine — isolated try/except retries next cycle; investigate only if persistent |
| Non-MLB sport bets stuck | backfill is MLB-only today | use `python -m arb settle` (Odds API) within 3 days, or add a sport-specific source |

## 8. Code change to make before the test (small, recommended)

Add a `settle-health` CLI command that wraps the §5 check, so monitoring is one command
instead of a pasted script. Spec:

- `python -m arb settle-health` prints: open count, overdue count (event day in the past),
  oldest overdue event day, and last settlement timestamp.
- Exit code non-zero if overdue > `settle_health_overdue_max` (config, default 0) — so it can
  be used in cron/CI as an alarm.
- Implementation: read open EV/ARB bets, parse `event_key` date from `legs_json`, compare to
  today (with a grace window `settle_grace_hours`, default 6, for same-day late finals).

This is the only code we still need for reliable monitoring; settlement itself (two sources,
every-cycle backfill, isolated failures) is already in place.

## 9. One-line mental model
**Two sources, settle every cycle, and watch the OVERDUE count.** If OVERDUE is 0, every
finished game is settled. If it climbs, settlement isn't running — restart the daemon and
run `settle-backfill`.
