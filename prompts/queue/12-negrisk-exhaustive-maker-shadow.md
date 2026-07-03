# 12 — Improvements batch A: negRisk exhaustive coverage + maker-side shadow lane

Read `CLAUDE.md`, `SPEC.md`, `MASTER_PLAN.md` §3 (items 1–2), and
`MODEL_ISSUES_SWEEP.md` L3/L4 first. Executor: Opus 4.8. Requires Stage 0–2 complete and
prompt 05's corrected fees (both items are fee-sensitive). **Measurement only — nothing in
this prompt books a trusted bet or changes any headline.**

## Hard rules (restated, binding here)

- **Paper trading only.** The maker lane is a *counterfactual re-pricing*, not simulated
  order resting; no order endpoints exist anywhere.
- **No Odds API credits in tests or loops** — both items use only free venue GETs.
- **Every threshold in `config.yaml`; Decimal money; `flat_bets` append-only.**
- **Measurement first, betting later:** each item ships with its pre-registered hypothesis
  appended to `PREREGISTRATION_V2.md` *before* its forward data accumulates.

## Item 1 — negRisk exhaustive coverage (L4)

**Why:** intra-Polymarket exhaustive baskets are the safest construct in the project
(single venue, single oracle, no settlement risk) and the scan is page-capped
(`negrisk_scan_pages: 3` × 200) — baskets beyond the first pages are never seen. With
prompt 05's corrected quadratic fees, the fee bar at mid prices *dropped*, so coverage is
worth buying. V1 evidence: 4 baskets ever, all Σ ≈ 1.005–1.011 (honest null so far).

**Build:** paginate the negrisk sweep to exhaustion (it is cheap, public, keyless);
respect Gamma's pagination exactly as the adapter already does elsewhere (no invented
params); per-sweep coverage line in the report (baskets seen / new / flagged). Gate the
sweep cadence in config so exhaustive scans don't crowd the watch loop (e.g. negrisk full
sweep every N minutes, config key).

**Pre-registered hypothesis (append before data):** H-NR-2 — with corrected linear→quadratic
fees and exhaustive coverage, exhaustive-exclusive baskets with
`1 − Σ eff_cost ≥ negrisk.min_edge` occur and produce positive confirmed P&L over ≥30
settled baskets (PREREGISTRATION_V2 §4 analogue). Kill: confirmed median ≤ 0, or zero
qualifying baskets in 30 days (the null stands, stop looking).

## Item 2 — maker-side shadow lane (L3)

**Why:** every leg is priced as a taker. Both venues rebate makers (Polymarket: makers pay
0, 25% rebate program; Kalshi: maker fee = 25% of taker on fee-enabled markets — cited in
prompt 05). A fraction of "no-edge-after-fees" quotes flips +EV at maker pricing; the size
of that fraction is unknown and cheaply measurable.

**Build:** a measurement-only re-pricer: for every flag (and every near-miss within a
config band of the gate), also compute the maker-side effective cost (join the book at the
current bid / inside the spread per venue rules; fee at the maker rate) and log the
counterfactual edge to its own analysis table (operational data, UPDATE-allowed, never
P&L). Report: share of flags/near-misses that flip +EV as maker, by venue and class. NO
fill-probability claim — the output is explicitly labeled an upper bound ("if a maker order
filled at this price, which nothing here establishes").

**Pre-registered hypothesis (append before data):** H-MAKER-1 — ≥X% (pick and freeze a
threshold, suggest 20%) of near-miss quotes flip +EV at maker pricing over 14 days. This
gates only whether a *fill-probability study* is worth designing later; it never gates
betting.

## Tests (fixtures only)

1. Pagination: fixture with 3 pages of negrisk events → all baskets seen; coverage line
   correct.
2. Basket math under quadratic fees: the prompt-05 break-even vector reused.
3. Maker re-pricer: taker-cost vs maker-cost vectors per venue (Kalshi quadratic maker =
   25% of taker; Polymarket maker fee 0); a near-miss that flips is flagged, one that
   doesn't isn't.
4. Nothing writes to `flat_bets` from the maker lane (assert).

## Acceptance (show output)

1. `pytest` green.
2. One live exhaustive negrisk sweep: coverage line + any flagged baskets (expect few or
   none — say so plainly).
3. One day of maker-shadow data: the flip-fraction table.
4. `PREREGISTRATION_V2.md` shows both hypotheses dated **before** the live runs.

## Out of scope

- No maker fill simulation, no queue modeling, no betting from either lane, no Kelly.
- No SX maker modeling (0/0 fee structure makes it moot for fees; depth realism is the
  binding issue there and belongs to the ladder work).
