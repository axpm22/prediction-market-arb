# BACKLOG_ROADMAP.md

Prioritized sequencing of the open backlog. Ordering is by **leverage ÷ effort**, gated by
two hard constraints specific to this project:

- **Append-only comparability.** Anything that changes `effective_cost` or the consensus
  retroactively breaks the comparability of the ~945k stored quotes and 3,100+ bets. Such
  changes ship as a **new parallel column / versioned field**, never a silent in-place edit.
- **Frozen analysis rules.** Anything that changes a gate definition (CLV, segment
  enablement, validation) is a PREREGISTRATION.md change → **dated append-only amendment**,
  not an edit. Instrumentation (adapters, schema, refactors) may change freely with a
  changelog note.

So the cheapest, safest, most informative work goes first, and it often *reshapes* the
expensive work — e.g. the de-vig regrade may show proportional de-vig is fine, which
downgrades the urgency of sharpness-weighting.

## Priority table

| # | Item | Leverage | Effort | Blast radius | Depends on | Wave |
|---|------|----------|--------|--------------|------------|------|
| Lever A.3 | De-vig regrade analysis (Shin/power/additive on persisted p_i) | High (validates the de-vig bias caveat cheaply) | Low | **None — offline, read-only** | persisted per-book p (exists) | 0 |
| weakness#10 | DIVERGENCE mean-reversion scoring | Med | Low–Med | None — new offline measurement | divergence archive (exists) | 0 |
| EV#2 | Book-sharpness-weighted consensus | High (attacks "books-as-truth", Weakness #5) | Med | Low *if parallel column* | sharp-book list (open Q) | 0→1 |
| pass3#7 | DB UNIQUE + poll atomicity (re-key live DB) | Med (integrity/dedup) | Med | **HIGH — migrates live ledger** | backup + migration plan | 1 |
| pass3#8 | Lifecycle-helper consolidation (refactor) | Med (cuts bug surface) | Med | Low (behavior-preserving) | test coverage | 1 |
| EV#3 | SE→Kelly + edge/SE gate (wire dispersion into sizing/store) | High (uncertainty-aware sizing) | Med | Low (additive column) | re-keyed schema (pass3#7) | 1 |
| does-well#3 / weakness#2 | Full depth ladder capture | High (unlocks fill realism) | **High** (every adapter) | Med (additive schema, bigger rows) | — | 2 |
| does-well#2 | Depth-scaled slippage | High (shifts **every** cost vector) | Med | **HIGH — versioned field required** | depth ladder (above) | 2 |
| EV#9 | CLV vs no-vig closing mid | Med–High (cleaner EV gate) | Low–Med | Frozen-rule → amendment | closing recorder | 3 |
| EV#10 | Per-segment calibration gate | Med | Med | Frozen-rule → amendment | settled n per segment | 3 |
| pass3#9 | Walk-forward gates | High (kills in-sample overfit of scorer) | Med–High | Frozen-rule → amendment | settled history | 3 |
| Lever C | negRisk coverage expansion (S1) | Med (cleanest construct, no settlement risk) | Med | Low (single-venue, measurement) | — | 4 |

---

## Wave 0 — zero-risk offline wins (do first)

No live-DB writes, no frozen-rule changes. Each is read-only analysis over data you already
store, and each *informs* a later, more expensive item.

1. **Lever A.3 — de-vig regrade.** Re-grade existing flags under Shin / power / additive
   using the persisted per-book `p_i`. Output: how much the EV sign/magnitude moves vs
   proportional de-vig. This is the literal payoff of having stored per-book probabilities,
   costs nothing, and **gates whether EV#2 is even worth doing** — if proportional and Shin
   agree within noise, sharpness-weighting is lower priority.
2. **weakness#10 — divergence mean-reversion.** Turn the divergence archive into a signal:
   do large cross-venue divergences revert, and on what horizon? Pure offline; additive.
3. **EV#2 prerequisite — the sharp-book list.** Answer the open question first (see below),
   then compute sharpness-weighted consensus as a **parallel column beside the median** —
   not a replacement. Measurement-only, so blast radius stays near zero until you decide it
   beats the median on CLV.

**Open question to resolve here:** which Odds API books count as sharp? The defensible set
is the ones the literature/market treats as price-makers and that the feed actually returns
— **Pinnacle** above all, then **Circa** (often not in The Odds API US feed — verify),
**BetOnline**, and low-hold books. Action: dump the distinct `book` values your stored
oddsapi quotes actually contain, rank them by historical hold and by lead in your own
lead-lag data, and freeze the sharp set in `config.yaml` (`consensus.sharp_books`). Don't
hardcode Pinnacle/Circa blind — confirm they're in the feed first.

## Wave 1 — foundational schema + integrity (the one risky migration, done once)

These touch the live ledger, so do them **together, behind a backup and an idempotent
migration**, and get them out of the way before any further schema work lands on a messy key.

4. **pass3#7 — UNIQUE + poll atomicity.** Re-key the live DB first so subsequent additive
   columns land on a clean schema. Caution: the ledger is append-only and *legitimately*
   re-sights the same quote across polls — the UNIQUE key must dedup true duplicates without
   dropping real re-observations. Back up `data/*.db`, write the migration as a one-shot
   idempotent script, dry-run on a copy, diff row counts before/after.
5. **pass3#8 — lifecycle-helper consolidation.** Behavior-preserving refactor; do it
   alongside #4 to shrink the surface before wiring new sizing logic in #6. Lock it in with
   the existing tests (assert identical ledger output pre/post).
6. **EV#3 — SE→Kelly + edge/SE gate.** Dispersion is already computed; persist it
   (additive column, safe on the re-keyed schema), gate flags on `edge/SE`, and size Kelly
   off the **lower bound** of fair value — the same lower-CI principle in
   LIVE_BASEBALL_EV_SPEC §1.13. High leverage, now low-risk because the schema is clean.

## Wave 2 — cost-vector changes (high blast radius — version everything)

7. **does-well#3 / weakness#2 — full depth ladder.** Capture the whole top-of-book ladder
   per adapter, not just `depth_usd`. This is the expensive prerequisite: it feeds both the
   depth-scaled slippage below and the depth-capped P&L that fixes weakness#2. Additive
   schema, but broad adapter work — do it before #8, not after.
8. **does-well#2 — depth-scaled slippage.** Replace the flat `taker_buffer` with a
   depth-scaled buffer. This shifts **every** cost vector, so it must carry a
   `buffer_version` tag: old rows keep their original cost, new flags use v2, and the report
   compares like-for-like. Without versioning you destroy the comparability of the whole
   ledger. Roll the depth-capped P&L fix (weakness#2) in here using the new ladder.

## Wave 3 — frozen-rule amendments (each needs a dated PREREGISTRATION entry)

These change analysis gates, so they are pre-registration amendments, appended with date and
reason — never silent edits — and they only "count" forward from the amendment date.

9. **EV#9 — CLV vs no-vig closing mid.** De-vig the closing mid before the CLV comparison so
   the gate isn't contaminated by closing vig. Amend H-EV1's definition; prior CLV readings
   stay as-is under the old definition.
10. **EV#10 — per-segment calibration gate.** Enable/disable per segment on calibration, not
    one global gate. Pairs naturally with #11.
11. **pass3#9 — walk-forward gates.** Out-of-sample the scorer so segment scores aren't
    fitted and judged on the same bets — directly fixes the "no uncertainty / in-sample"
    weakness in segment scoring. Highest methodological payoff of this wave.

## Wave 4 — strategy expansion (independent, lower urgency)

12. **Lever C — negRisk coverage.** Expand the negative-risk basket sweep (S1 — "the single
    cleanest construct," no cross-venue settlement risk). Worth pushing once the cost and
    sizing infrastructure above is solid, since it inherits the depth ladder and SE sizing.

---

## One-line rationale for the ordering

Offline analyses (Wave 0) are free and can shrink the later waves; the live-DB re-key
(Wave 1) is the single highest-blast-radius change and everything additive should land after
it on a clean schema; cost-vector changes (Wave 2) must be versioned or they corrupt
comparability; gate changes (Wave 3) are frozen-rule amendments by definition; strategy
expansion (Wave 4) inherits all of the above and so comes last.
