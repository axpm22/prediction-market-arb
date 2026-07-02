# FIX: settled-fingerprint re-book loop — one finished game booked 24 times in 90 minutes

Read `SPEC.md` and `CLAUDE.md` first. All hard rules apply. Rule 9 is central here: `flat_bets`
is append-only — the cleanup below uses annotations, never UPDATE/DELETE.

## The incident (observed live, 2026-07-02 ~17:00–19:00 UTC)

The tracker booked 47 "settled" EV rows in 90 minutes that were really 5 positions:

- Fingerprint `c2f39d1b04a7...` (Braves–Cardinals 2026-07-02, SX leg, `below-e-min`) was booked,
  settled WON +$1.12, and re-booked **24 times**, once per ~2m45s poll cycle. Each row's
  `reappearance_of` points at the previous row. `placed_at` → `settled_at` gap: **~0.4 seconds**.
- Three `uneconomic-horizon` fingerprints did the same on the LOST side (23 rows).

Mechanism: the game had **already finished**. SX kept a stale book up. Each poll: scanner sees
the stale quote → flags EV → tracker treats the previous (settled) row as closed and books a
"reappearance" → settle immediately resolves it from the known final score → next poll repeats.
An infinite bet-settle loop printing fake P&L into the raw lane (+$3.95 net today is ~all loop).

Two guards failed simultaneously:
1. **A3 timing guard** (`placed_at < start`) is not enforced at placement for these lanes — the
   model bet on a game that had already ended.
2. **Reappearance logic treats SETTLED as re-bookable.** A `terminal-fingerprint-skipped` flag
   already exists in the codebase but did not cover this path.

## Fixes (root cause, in order)

### 1. A3 at placement, hard

Before ANY `flat_bets` insert (all kinds — EV, ARB, DIVERGENCE, CLOSE, micro, future ai_matched):
if the event has a known `start_utc` and `now >= start_utc`, do NOT book. Write a diagnostic
row/log line (`rejected:in-play-or-finished`) instead, so the rejection is visible and countable.
No config knob for this — it is not tunable behavior, it is correctness (a pregame lane must
never open a position after start). Live lanes, if ever added, get their own explicit path.

### 2. Settled fingerprints are terminal

Once a fingerprint has ANY row with `settled_at IS NOT NULL` (or status SETTLED), it is terminal
forever: no re-book, no reappearance, no CLOSE row. Reappearance remains valid ONLY for
fingerprints whose latest row is CLOSED-unsettled (opportunity vanished, then returned).
Extend the existing terminal-fingerprint check to include settled state, and make the tracker
consult it on every insert path, not just the one that currently has it.

### 3. Instant-settle tripwire (generic, catches the next variant)

Any row where `settled_at - placed_at` < one poll interval means the outcome was knowable at
placement — the bet was never a forecast. In `settle`, flag such rows
`quality_flag='instant-settle'` (if no more specific flag) and log loudly. This is a tripwire,
not a filter: it catches future bugs of this family (stale venue books, backfilled scores,
clock skew) regardless of which upstream guard failed.

### 4. Stale-book guard for venues that leave finished markets up

SX kept quoting a finished game. In the pipeline, drop any quote whose event `start_utc` is in
the past for pregame lanes (this also cuts scan noise). Do not special-case SX; apply to all
venues. (The quote still persists to `quotes` per rule 7 — persistence and eligibility are
different things.)

## Data cleanup (append-only, rule 9)

- Annotate every loop row via the immutable annotation table (the V1 `bug:in-play-pricing`
  mechanism): tag `bug:settled-rebook-loop` on all rows from 2026-07-02 whose fingerprint is in
  the loop set (identify as: fingerprint with >1 settled row, or settled_at − placed_at < 60s,
  on 2026-07-02). Keep the FIRST genuine settled row of each fingerprint un-annotated only if
  it was placed before the event start; otherwise annotate it too.
- Daily reports and dashboard recompute from the raw log (rule 9) and must exclude annotated
  rows from every cell of the honest-variant matrix. Verify today's raw-optimistic total drops
  to the ~5 real positions.

## Future-mistake insurance (cheap invariants, not a framework)

Add one `python -m arb check-invariants` command (or extend an existing report) that scans the
ledger and prints violations, zero = silent:
1. No fingerprint has >1 settled row.
2. No row has `placed_at >= event start_utc` (where start is known).
3. No row settled faster than the poll interval without an `instant-settle` flag.
4. No reappearance chain longer than N (config `tracker.max_reappearances`, default 10) in 24h.
5. Every settled row's P&L is Decimal-clean (no float drift).

Run it at the end of every settle cycle; a violation logs CRITICAL but never blocks settlement
(observability, not a new failure mode). These five checks would each have caught today's bug.

## Tests (fixtures only — no live calls, no Odds API credits)

1. Finished-game fixture (start_utc in the past): pipeline yields quote rows but tracker books
   nothing; diagnostic rejection logged.
2. Settle a fingerprint, re-present the same opportunity next poll: no new row (terminal).
3. CLOSED-unsettled fingerprint re-presents: reappearance row IS booked (don't break the
   legitimate path).
4. Row settling within poll interval gets `instant-settle` flag.
5. `check-invariants` on a synthetic ledger containing today's pattern reports violations 1–3;
   on a clean ledger reports nothing.
6. Annotated rows are excluded from every honest-variant matrix cell in the report recompute.
7. Existing SPEC §8/§9 vectors untouched and passing.

## Acceptance

1. `pytest` green.
2. Run the watch loop live through at least 3 poll cycles during/after an MLB game day:
   zero new rows on finished games, rejection diagnostics visible, no reappearance chains on
   settled fingerprints.
3. `check-invariants` on the production DB: reports exactly the annotated 2026-07-02 loop rows
   as historical violations (they remain in the log — append-only), zero new ones.
4. Dashboard/daily report shows today's corrected totals with the loop rows excluded and
   labeled.

## Out of scope

- No schema migration; annotations and flags only.
- No live in-play betting lane.
- No changes to the AI matcher / nonsports prompts (they inherit fix #1 automatically because
  it sits on the shared insert path — verify with test 1 only).
