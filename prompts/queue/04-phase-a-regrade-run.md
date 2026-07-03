# 04 — Run Phase A: regrade the consensus vs the venues; execute the decision rule

Read `CLAUDE.md`, `SPEC.md`, and `MODEL_FOUNDATIONS_FIX_SPEC.md` **Phase A in full** first.
Executor: Opus 4.8. Requires prompt 01 (regrade module committed, tests green); does NOT
require 03 (this is read-only archive analysis and can run during the outage or in a
parallel worktree).

## Hard rules (restated, binding here)

- **Read-only.** This prompt writes no DB rows anywhere. Open every DB
  `file:<path>?mode=ro`. The archives are oracle data.
- **No Odds API credits** — no live calls at all in this prompt.
- **Decision rules are pre-committed** (below, verbatim from the foundations spec). When
  one fires, record the trigger numbers in `PREREGISTRATION_V2.md` (dated amendment) along
  with the action taken. Do not rationalize past a fired rule.
- Config changes that result (e.g. `devig_method`, `ev_enabled`) are **versioned changes**
  with a dated comment — never silent.

## Context

The EV lane assumes the de-vigged soft-book median is truth. The evidence it might be
inverted: V1 archive clean settled MLB EV is **−$24.07 over 627 rows** (audit-verified);
win rate falls as apparent edge rises (R5). The `arb regrade` command (built in Phase A,
committed by prompt 01) scores consensus p̂, Kalshi mid, and Polymarket mid as forecasters
on settled events, plus the EV-subset test. The fresh post-cutover DB has ~no settled
evidence — the data for this analysis lives in the **archives**:

- `data/archive-pre-v2/arb.sqlite` (Jun 10–Jul 2: 2.94M quotes, 881 settled EV, 627 clean)
- `data/arb-incident-baseline-<date>.sqlite` if prompt 03 has run (one day, all-annotated —
  exclude annotated rows; expect it to contribute ~nothing).

Also resolve the **de-vig contradiction**: `PREREGISTRATION_V2.md` froze
`devig_method: power` as the empirical winner (Brier 0.2231 vs proportional 0.2240,
n=191, margin within noise); shipped `config.yaml` still says `proportional`.

## Task

1. Point `arb regrade` (and the devig bake-off in
   `arb/experimental/analysis/devig_brier.py` / `test_devig_regrade.py` machinery) at the
   archive DB. If the command assumes the live DB path, add a `--db` argument rather than
   copying files.
2. Produce, per sport and overall: Brier, log-loss, and a calibration table
   (predicted-decile vs realized frequency) for (a) consensus p̂, (b) Kalshi mid,
   (c) Polymarket mid, at consensus time (venue quotes within ±5 min of `computed_at`).
3. EV-subset test: over every settled EV bet, did the outcome favor the venue side or the
   consensus side? Report win rate with a binomial CI. Exclude annotated rows.
4. De-vig bake-off: proportional / power / shin on the same settled data; report the
   Brier/log-loss table and n.
5. Execute the decision rule and record it.

## Decision rule (pre-committed; foundations spec Phase A, verbatim)

- Consensus beats venue mids on Brier AND EV-subset win rate > 50%: EV lane keeps running;
  tune `devig_method` to whichever wins the regrade.
- Venue mids beat consensus: **the EV lane's sign is wrong** — freeze EV bet creation
  (config `ev_enabled: false` or the equivalent existing gate), keep recording EV *quotes*
  for data, and repurpose the signal: consensus-vs-venue divergence becomes a
  *venue-leads-books* indicator to study, not bet.
- Inconclusive (CIs overlap): keep the lane but halve `ev_threshold` display prominence and
  re-run the decision monthly as settled N grows. No new EV-derived features until resolved.

Additionally: the daily report gains one line — rolling 30-day Brier for consensus vs venue
mids — so the premise stays continuously monitored (small code change, tests included).

## Acceptance (show output)

1. The full regrade report (all three forecasters, per-sport, calibration tables, EV-subset
   CI, de-vig bake-off) pasted.
2. The decision-rule verdict stated in one sentence, the config diff (if any), and the
   `PREREGISTRATION_V2.md` amendment.
3. `pytest` green (the new daily-report line has a fixture test).
4. Proof of read-only: no archive mtime changed.

## Out of scope

- No changes to EV detection math; no calibration fitting (that's gated by
  `calibration.min_samples` and belongs to a later prompt once the verdict is known).
- No fee changes (prompt 05, independent).

**STOP for review after this prompt** — the verdict decides whether the EV lane lives.
