# CLEANUP: purge ALL loop-generated P&L from every report basis

Read `SPEC.md` and `CLAUDE.md` first. Rule 9 is absolute: `flat_bets` is append-only. This task
removes loop P&L from every REPORTED total via the immutable annotation table — zero UPDATE or
DELETE statements anywhere. If you find yourself writing one, stop.

This is the data-cleanup companion to `FIX_SETTLED_REBOOK_LOOP_PROMPT.md` (the code fix). It
can run before or after it, but the fix must land the same day — otherwise the loop keeps
minting rows and this cleanup is Sisyphean.

## Background

On 2026-07-02 the tracker looped on stale quotes for finished games: the same fingerprint was
re-booked and instantly re-settled once per poll cycle (24× WON +$1.12 on one Braves–Cardinals
SX leg; 23× LOST −$1.00 across three others). Every such row is a bet placed after the outcome
was decided — not a forecast, not evidence, regardless of sign. The whole ledger must be swept,
not just today: the bug may have fired before, and V1-era data feeds the backtests.

## Task

### 1. Detect loop rows — whole ledger, both directions

Write a one-shot detector (`arb/experimental/analysis/loop_audit.py` or extend an existing
analysis module) that scans `data/arb.sqlite` (and, read-only, the archived
`data/arb-v2-baseline-20260701.sqlite` + anything under `data/archive-pre-v2` — report findings
there, annotate only the live DB) for rows matching ANY of:

1. **Duplicate settlement**: fingerprint with more than one `settled_at IS NOT NULL` row —
   every settled row after the first is a loop row; the first is kept only if it passes
   check 2 and 3.
2. **Post-start placement**: `placed_at >= event start_utc` (where start is derivable from
   `legs_json` event_key or the quotes table) on a pregame-lane row.
3. **Instant settle**: `settled_at - placed_at` shorter than the lane's poll interval
   (`watch_interval_secs` / the EV loop's ~165s cadence — read from config, don't hardcode).

A row matching any check is a loop row. **Wins and losses are treated identically** — purging
only profitable loops (or only losing ones) would bias the ledger; the point is that none of
these rows are forecasts. Print the full candidate set with fingerprint, dates, kind,
quality_flag, P&L, and which check(s) fired, BEFORE annotating anything.

### 2. Annotate, append-only

For every detected row in the live DB, append an annotation (the V1 `bug:in-play-pricing`
mechanism) with tag `bug:settled-rebook-loop`. Idempotent: re-running the detector must not
double-annotate (skip rows already carrying the tag). Include a `notes` field naming the
check(s) that fired.

### 3. Recompute every reporting surface

Daily reports, `dashboard.json`, equity curves, drawdowns, the honest-variant matrix (all
cells: optimistic/confirmed × raw/clean × flat/Kelly), per-kind and per-venue-pair tables,
CLV experiment inputs, and any backtest reader — all must exclude annotated rows. Rule 9 makes
this cheap: reports already recompute from the raw log, so the only change is that every
reader honors annotations. Audit each reader; the CLV/forecast pipeline especially must not
train or grade on loop rows.

### 4. Show the delta

Produce a small before/after table (stdout or the report): for each matrix cell, total P&L and
row count with vs without loop rows, plus the same for today (2026-07-02) alone. Expected shape
for today: settled rows drop from ~47 to ~5, raw-optimistic net moves from ≈ +$3.95 to ≈ $0.
If the sweep finds pre-2026-07-02 loop rows, list them separately with dates — that's new
information about how long the bug has existed.

## Tests (fixtures only)

1. Synthetic ledger containing: a clean settled bet, a 3-row loop chain (WON), a 2-row loop
   chain (LOST), a post-start single row, and an instant-settle row → detector flags exactly
   the right rows; the clean bet survives.
2. Idempotency: running annotate twice yields one annotation per row.
3. Report recompute on the synthetic ledger excludes annotated rows from every cell; the
   excluded sum equals the loop rows' P&L exactly (Decimal, no drift).
4. Loser-loops are excluded symmetrically with winner-loops (explicit assertion — this is the
   integrity property of the whole cleanup).

## Acceptance

1. `pytest` green.
2. Detector output on the production DB shown in full; annotation count reported.
3. Recomputed dashboard/daily report shown: no cell anywhere still contains loop P&L; today's
   headline reflects only real positions.
4. `flat_bets` row count UNCHANGED before vs after (prove it — append-only was honored).

## Out of scope

- No code-path fixes (that's FIX_SETTLED_REBOOK_LOOP_PROMPT.md).
- No schema changes.
- No editing of archived/baseline DBs — read, report, leave untouched.
