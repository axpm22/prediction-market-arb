# 07 — Phase C: make markouts real (persist hook, pnl_markout, flicker guard)

Read `CLAUDE.md`, `SPEC.md`, `MODEL_FOUNDATIONS_FIX_SPEC.md` **Phase C in full**, and
`arb/experimental/analysis/markout.py` first. Executor: Opus 4.8. Requires prompts 01–03.
Parallel-safe with 06/08.

## Hard rules (restated, binding here)

- **Paper trading only.** Markout sampling uses only the public quote reads the loops
  already perform.
- **`flat_bets` is append-only.** Markouts are operational/analysis data and live in the
  `markouts` table (UPDATE allowed there per the foundations cross-cutting rule); anything
  feeding P&L stays append-only.
- **No Odds API credits** — markouts come from venue quotes, never the consensus feed.
- **No magic numbers** — horizons and the flicker rule are config keys.

## Context

The optimistic/confirmed pair measures persistence, not adverse selection — and V1's
archive shows why that's not enough: settled EV reads −$19.13 optimistic but +$40.64
confirmed, a survivorship artifact (R2), not slippage truth. The Phase C analysis module
exists and is committed (prompt 01), the `markouts` table exists, but it has **0 rows**:
nothing persists markout samples yet. The hot/watch loops already re-sample fresh flags —
the data source is built; the hook isn't.

## Task

1. **Persist hook:** for every flat bet (EV and ARB legs), record the same outcome's best
   ask/bid at +30 s, +2 min, +10 min, +30 min after entry into `markouts`, sourced from the
   quotes the watch/hot loops already fetch (add a small scheduler to the hot-watcher
   machinery; do not add new HTTP load beyond what the loops already do — if a horizon
   sample isn't available from an existing poll within a tolerance window, record NULL with
   the gap, never fetch specially for it).
2. **`pnl_markout` column semantics:** third P&L basis — entry price = the +30 s re-quote
   ("you're never faster than 30 seconds"). Computed at read time from `markouts` +
   `flat_bets` (recompute-from-raw discipline); shown per class beside
   optimistic/confirmed in the daily report and dashboard, clearly labeled.
3. **Flicker/zombie guard:** before any paper lane counts a quote as fillable, require it
   seen at the same price on ≥2 consecutive polls of the owning cadence OR flash-verified
   once. Unverified single-appearance quotes get a `flicker` tag — recorded, never booked.
   This subsumes the `wr_ceiling` heuristic; keep `wr_ceiling` as a report-side sanity
   check only.
4. **Report:** median and mean markout per class, per venue-pair, per edge bucket, at each
   horizon; plus a one-line rolling summary in the daily report.
5. **Backfill dry-run (read-only):** run the markout analysis over the archive
   (`data/archive-pre-v2/arb.sqlite`) where the stored quotes happen to cover the horizons,
   to sanity-check the math on real data before live rows accumulate. Annotated rows
   excluded.

## Decision rule (pre-committed; foundations spec, verbatim)

If markout at +2 min eats more than half the average flagged edge for a class, that class's
headline switches to `pnl_markout` as its primary number, and any future "go live"
consideration prices the measured markout in as a per-bet cost. If markout ≈ 0, we have
evidence the edges are real and slow — record that prominently.

## Tests (fixtures only)

1. A synthetic bet + quote stream → markouts at all four horizons, correct signs; a missing
   horizon records NULL-with-gap.
2. `pnl_markout` computed from the +30 s re-quote to the cent (Decimal).
3. Flicker: single-appearance quote → tagged, not booked; two-consecutive-poll quote →
   bookable; flash-verified single appearance → bookable.
4. SPEC §9 tracker vector untouched.

## Acceptance (show output)

1. `pytest` green.
2. The archive dry-run markout table pasted (even if sparse).
3. After one live game day post-restart: `SELECT COUNT(*) FROM markouts` > 0 and the daily
   report shows the markout line. (If run before a game day, show the wiring with a
   fixture-driven end-to-end and leave the live check to the stage gate.)

## Out of scope

- No changes to optimistic/confirmed semantics; no headline switch until the decision rule
  actually fires on ≥ its own data; no new venue endpoints (the Polymarket WS feed is a
  separate future item).
