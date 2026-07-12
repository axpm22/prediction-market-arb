# A Measurement-First Prediction-Market Instrument: Pre-Registered Edges, Honest Nulls, and a Non-Sports Pivot

*Draft v1 — 2026-07-12. Sections marked ⏳ fill from post-freeze data (PREREGISTRATION.md, non-sports program, frozen 2026-07-12). Numbers below are real and reproducible from the append-only ledger + reports/daily/HASHES.log.*

## 1. Thesis

Most retail "arbitrage bots" die of unmeasured optimism: fantasy fills, fake
matches, retro-edited ledgers. This project inverts the order: build the
measurement instrument first, pre-register every hypothesis and kill gate,
and only then ask whether an edge exists. Two hypotheses have already been
killed by their own gates; that is the point, not a failure.

## 2. Instrument design

- Paper-only, flat $1 stakes; append-only `flat_bets` + `bet_events` ledger
  (CI grep-tests forbid UPDATE/DELETE); every derived P&L cell recomputed at
  read time from raw events.
- One eligibility predicate (`classify()`, grep-enforced single definition)
  decides (eligible, reason, lane) for every bet-shaped object.
- Every observed quote persists (rule 7) — 700k+ rows/day across four lanes —
  with monthly archive rotation; daily reports hash-chain into
  `reports/daily/HASHES.log` for tamper-evidence.
- All thresholds in one strict-schema config; pre-registration with dated
  amendments; loops run only under a supervisor from committed HEAD after
  green pytest (220 tests).

## 3. Negative result #1: soft-book consensus EV (killed pre-launch)

The inherited Phase A verdict: de-vigged consensus from accessible "sharp"
books (lowvig, betonlineag) was a WORSE forecaster than the venues' own mids
— Brier 0.238 vs 0.221/0.223, consensus ECE 0.30, and the EV-subset venue
side won 420/877 = 47.9%. `ev_enabled` froze OFF with a pre-registered
re-open trigger (paired Brier via cluster bootstrap; Pinnacle anchor as the
live hypothesis). Lesson: on sports, the venues are the sharps; a signal that
loses to its own market is not a signal.

## 4. Negative result #2: lead–lag "speed edge" (artifact caught by design)

First-mover tallies across venues initially showed p_lead = 0.778 with a ~1s
median "lead" — entirely a fetch-ordering artifact (kalshi ~0.7s vs PM ~15s
within one poll cycle). The pre-registered tie rule (|Δt| ≤ 5s = tie) removed
it before any claim was made. Honest successor claim: no lead visible at
≥ 20s observation latency. Corollary adopted project-wide: at 60–300s polling
there is no speed edge to claim; the only durable retail edge is structural
patience.

## 5. The pivot: who actually loses money here?

Sports prices on Kalshi/PM are anchored by the sharpest betting markets on
earth (§3 proved it empirically). Non-sports is where retail flow is
undisciplined. Inventory survey (2026-07-11, free public endpoints): 829 open
PM events across politics/economy/weather, 62% carrying the structural
negRisk flag; weather 87% negRisk with 143/181 events resolving < 7 days;
~2,977 Kalshi non-sports series with numeric-strike templated econ/weather
cores. Two lanes measure the hypothesis: POLITICAL (intra-market YES+NO < 1,
PM only) and NEGRISK (exhaustiveness-proven basket sum violations), both
booked $1 paper through the same ledger as everything else.

## 6. Matching without lying to yourself

Cross-venue non-sports matching is where fake edges are born, so the pipeline
is deterministic-first: blocking on curated concept phrases + resolution-date
window + exact strike agreement, an entity guard (China GDP ≠ South Korea GDP
— caught live on day one), and NO fuzzy text matching anywhere. A Haiku
classifier runs only on blocked candidates, cached append-only with a per-day
budget, and may be enabled only after clearing a pre-registered bar
(precision ≥ 0.95, zero false positives, ≥ 80 human-verdicted pairs).
Human curation is the standard the machine is measured against
(data/nonsports_curation_protocol.md): ⏳ N pairs verdicted, M hours.
Field notes the pipeline surfaced unprompted: PM's "weather" tag is
climate/disaster events, not the daily city temperatures Kalshi lists; PM CPI
buckets vs Kalshi CPI thresholds are settlement-structurally different at the
same strike.

## 7. Measured results (post-freeze 2026-07-12) ⏳

Per the frozen program: weekly flag rate, fee-inclusive median edge at the
depth floor, 300s persistence fraction, mismatch count (must be 0); kill gate
at < 1 qualifying flag/week over 3 weeks; no edge claim below 50 settled bets
with a cluster-bootstrapped CI excluding 0.
- NEGRISK: ⏳
- POLITICAL: ⏳
- AI matcher eval vs ground truth: ⏳ (precision bar 0.95/0-FP)

## 8. What this is not, and the roadmap

No live orders were placed (hard rule 1); capacity projections are
quarantined behind a ladder-coverage gate and never touch the headline.
Roadmap, in order: live micro-executor for the same-venue locked lane behind
the existing gates ($500–1k, separate process, dated amendment); Pinnacle
single-anchor re-test of the EV lane per its re-open trigger; maker-side
quoting on Kalshi non-sports (the durable version of this edge). Decision
recorded 2026-07-12: no venue websockets yet — at a 300s structural-patience
cadence they solve a problem this program doesn't have; revisit only if
post-freeze persistence data shows flags dying inside the poll interval.

## Appendix: reproducibility

Repo with full git history; pre-registration with dated amendment log; test
suite (220) including the pinned math vectors; recorded API fixtures; daily
report hash chain. Every number in this document is recomputable from the
ledger by `arb report --daily <date>`.
