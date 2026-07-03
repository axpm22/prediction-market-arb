# 03 — Baseline-cutover decision + supervised restart (ends the outage)

Read `CLAUDE.md`, `SPEC.md`, `prompts/incident_recovery_master.md` (Steps 3–5), and
`MODEL_FOUNDATIONS_FIX_SPEC.md` Phase D first. Executor: Opus 4.8.
Requires prompts 01 and 02 complete and reviewed.

## Hard rules (restated, binding here)

- **Paper trading only.**
- **`flat_bets` is append-only** — a "reset" is a baseline **cutover** (archive + fresh
  DB), never row deletion.
- **No Odds API credits in tests.** The restart's live cycle logs
  `x-requests-remaining`; abort below 50 (last observed: 492).
- **Persist every quote.**
- The stop flag is removed **only at the final step of this prompt**, after every gate.

## Context

`data/stop-all` has been up since 2026-07-02 ~16:25 local. The incident-recovery master
prompt specified a baseline cutover (Step 3: archive to
`data/arb-incident-baseline-20260702.sqlite`, fresh `data/arb.sqlite`) that was **never
executed** — the live DB still holds the (annotated) incident rows. Since then, the
annotation mechanism has proven it fully excludes the junk (clean settled P&L reads 0 rows
/ $0.00), so the cutover is now a **choice**, not a necessity:

- **Option A — cutover anyway** (the user's standing 2026-07-02 decision): cleanest
  restart-from-zero optics; archive keeps the 616k quotes queryable.
- **Option B — keep the DB, rely on annotations**: preserves one continuous DB; the daily
  report already excludes annotated rows; less churn.

Default to **Option A** (it is the recorded user decision from
`prompts/incident_recovery_master.md`; note: the date suffix in the archive filename should
be the date you run this). If the user is present, confirm; otherwise proceed with A and
say so in the report.

## Task

1. Verify `data/stop-all` still exists (abort if removed — someone restarted early).
2. Verify prompts 01–02 landed: clean tree, `check-invariants` available, `pytest` green.
3. Execute the cutover (Option A): copy `data/arb.sqlite` →
   `data/arb-incident-baseline-<YYYYMMDD>.sqlite`; initialize a fresh `data/arb.sqlite`
   through the existing schema path. Aliases/series caches/`nonsports_map.yaml`/
   `data/settlement_matrix.yaml` are files, untouched.
4. `arb report` on the fresh DB: zeros everywhere.
5. Record the cutover in `PREREGISTRATION_V2.md` (dated amendment): reason, archive
   filename, and the rule that pre-cutover data is analysis-only, never blended into
   post-cutover P&L.
6. **Restart under the Phase D supervisor, not as loose nohup processes:**
   `python -m arb supervisor` with the `supervisor.processes` config enabling the loops
   that should run (watch/daemon cadence, ev-exit, focus, negrisk, political per config).
   Confirm the per-process locks refuse a double-run (start a second copy of one loop by
   hand; it must exit with a message).
7. Remove `data/stop-all`. Watch **three full watch cycles** live:
   - quotes persisting; zero new rows on finished games; no reappearance chains on settled
     fingerprints; any `inplay-bet-refused` counts visible;
   - `python -m arb status`: every enabled process alive, beating, on the committed HEAD,
     dirty=false;
   - `python -m arb check-invariants`: zero new violations.

## Acceptance (show output)

1. Archive file exists and opens read-only; fresh DB `arb report` all-zero (Option A), or
   the recorded justification for Option B.
2. `arb status` table: all enabled loops alive on a clean HEAD.
3. Three-cycle live observation summary + `check-invariants` clean.
4. `PREREGISTRATION_V2.md` amendment diff.

## Out of scope

- No analysis runs (04+). No config value changes beyond `supervisor.processes`.
- Never edit or delete rows in any DB, including archives.

**STOP for review after this prompt** — show the status table and the first clean daily
report header. Stages 1+ only start on a running, supervised, clean-HEAD system.
