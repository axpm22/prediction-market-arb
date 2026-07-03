# 08 — Phase F completion: time gets the same rigor as money

Read `CLAUDE.md`, `SPEC.md`, `MODEL_FOUNDATIONS_FIX_SPEC.md` **Phase F in full**, and
`arb/experimental/analysis/cadence.py` first. Executor: Opus 4.8. Requires prompts 01–03.
Parallel-safe with 06/07.

## Hard rules (restated, binding here)

- **No invented endpoints.** The MLB Stats schedule API
  (`statsapi.mlb.com/api/v1/schedule`) is already an adapter dependency (settlement
  backfill) — reuse it; record a fixture for the schedule shape before parsing.
- **No magic numbers** — grades, windows, and per-(venue,sport) reliability live in
  `config.yaml`.
- **`flat_bets` append-only** — `consensus_age_secs` is stamped at bet creation (new
  column via the existing migration path), never backfilled onto old rows.
- **No Odds API credits in tests.**

## Context

Every incident this project has had was a clock problem: Gamma's fictional tennis start
times (fixed via `unreliable_start_venues` + event-level min-start), the frozen consensus
vs live prices, and the loop cadence — configured 60 s, measured **~140 s mean on the fresh
DB** (448 watch polls, 2026-07-02; audit-verified). Phase F is half-shipped: the cadence
analysis module exists (committed by prompt 01); the start-time truth *hierarchy*, the
per-bet consensus age, and the report integration do not.

## Task

1. **Start-time truth hierarchy** (generalizes the tennis fix): per event,
   `start_utc = min over reliable sources`, with reliability graded per (venue, sport):
   `verified | nominal | absent`. Extend the existing `unreliable_start_venues` config into
   this graded map. For MLB, the MLB Stats schedule outranks all venue times (free,
   keyless, already fetched for settlement). `nominal`-only events are non-actionable
   (existing E5 lane). The grade is stored on the event/quote path so later analysis can
   filter by it.
2. **Consensus age on every EV row:** stamp `consensus_age_secs` at bet creation; the
   edge-bucket report gains an age dimension (this is the input Phase A's monthly re-runs
   need to test edge-decay-with-age empirically).
3. **Loop-cadence honesty:** each poll records planned vs actual interval; the daily
   report prints p50/p95 cycle time per loop (wire `cadence.py` in). Then either make the
   60 s claim true — the supervisor can parallelize the venue fetches; they are independent
   HTTP calls — or correct `watch_interval_secs` to reality. No silent 2.3×.
4. **Leg-skew budget:** persist the measured skew on every multi-leg record (K5: a real
   column, not a note substring), so flash-verify prioritization can use it.

## Decision rule (pre-committed; foundations spec, verbatim)

After F ships, any bet created from a `nominal`-graded start time or an unmeasured
consensus age is a bug. The p95 cycle time printed in the daily report is the loop's real
spec; claims about "we scan every N seconds" cite it, not config.

## Tests (fixtures only)

1. Hierarchy: MLB Stats says 19:05, Kalshi 19:05, Polymarket 22:00 → event start 19:05
   grade `verified`; Polymarket-only tennis event → `nominal`, non-actionable.
2. `consensus_age_secs` stamped correctly from `computed_at` at creation.
3. Cadence: synthetic polls at 60/140/300 s → correct p50/p95 in the report.
4. Skew column populated on a two-leg fixture arb.

## Acceptance (show output)

1. `pytest` green.
2. One live daily report (or fixture-driven render pre-restart) showing: cadence p50/p95
   per loop, the start-grade counts, and EV rows carrying `consensus_age_secs`.
3. The explicit decision on step 3: parallelized-to-60s (show the measured improvement) or
   config corrected to measured reality (show the diff).

## Out of scope

- No new sports or schedule sources beyond MLB Stats; other sports keep venue-derived
  starts with grades. No changes to detection math.
