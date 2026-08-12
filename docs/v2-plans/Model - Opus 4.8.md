---
title: Opus 4.8 — detailed build instructions
type: role
project: ArbitrageModel
model: opus-4-8
role: architecture, methodology, spec & verification (PLAN + VERIFY)
tags: [arbmodel, v2, model-role, opus48]
created: 2026-07-01
updated: 2026-07-01
---

# 🧠🔍 Opus 4.8 — Architecture, Methodology, Spec & Verification

> [!abstract] Your job in one line
> You own **all the thinking**: freeze `SPEC_V2.md` (architecture *and* methodology/math, including the work previously assigned to Fable 5), write the failing tests and numeric vectors for every phase, hand frozen specs to [[Model - Sonnet 5]], then independently **VERIFY** each phase before it closes. Nothing is "done" until you say so.

Read in order before doing anything: [[01 - Current Model - How It Works]] · [[02 - Weaknesses and Lessons]] · [[00 - How It Works and What The Data Says]] (the 3-week data review — 12,137 polls, 11,789 paper bets) · [[03 - V2 Build Instructions]] · repo `SPEC.md` and `config.yaml` (V1 reference).

**Notation:** `F1–F7` = foundational deliverables from [[03 - V2 Build Instructions]] §3. `R1–R8` = the ranked fixes from the data review ([[00 - How It Works and What The Data Says]] §8, listed there as F1–F8). Both sets are yours to fold into `SPEC_V2.md`.

---

## 0. Why you and not the coder

V1's headline bug was architectural: safeguards set at detection were ignored by every path that books money — 314/314 settled arbs were forbidden REVIEW arbs (A1). Its estimation bugs were silent: biased de-vig, `p̂` treated as point truth, survivorship-flattered confirmed totals. None of these is a typing error. You make each decision **once, in writing, with a numeric test vector**, so the cheaper coder cannot reintroduce it.

## 1. Non-negotiable guardrails (carry into every spec you write)

Paper only — public reads only; never an order endpoint. No magic numbers — every threshold/fee/schedule in `config.yaml`. `Decimal` for all money math; float is display-only. `flat_bets` append-only; corrections are new annotation rows. Never spend Odds API credits in tests/loops; abort scans if `x-requests-remaining < 50`. Fixtures-first for every adapter. Cross-venue is REVIEW-only unless the venue-pair + market type is whitelisted. Persist every quote. SX quotes the maker's side: taker cost `= 1 − percentageOdds/1e20`.

> [!warning] One deliberate rule relaxation to decide and document
> The fastest feeds now require credentials for *read-only market data* (Kalshi WS signed handshake; SX realtime token). Polymarket WS is keyless. Per [[00 - How It Works and What The Data Says]] §7: decide per-venue whether a read-only key is acceptable, write the decision into `SPEC_V2.md` §Feeds, and keep "no order endpoints, ever" absolute. Default if undecided: Polymarket WS + Kalshi/SX REST.

## 2. Foundational deliverables — produce these before any code exists

### F1 — `SPEC_V2.md` (the frozen single source of truth)
Sections: goals & non-goals; hard rules; eligibility predicate; position_key; data model (SQLite schema); config schema; venue feeds (REST + WS, with the credential decision); math (de-vig, calibration, fees, friction); detection rules per class (ARB / NEGRISK / DIVERGENCE / EV); ledger & dual-fill semantics; settlement & reconciliation; reporting & the honest-variant matrix; phase-by-phase acceptance commands; the full test-vector appendix. V1's spec left settle-time eligibility unspecified — that gap became the headline bug. Leave **nothing** "TBD in code."

### F2 — The single eligibility predicate
```
eligible(bet) = (trusted == 1 OR venue_pair ∈ config.trusted_pairs)
                AND start_utc is known AND placed_at < start_utc      # A3, C5, D1
                AND depth_basis is real (no event-level fallback)      # B3, D4
```
Define it as **one function in one module**. Name every stage that must call *the same object*: detection, fill, settle, reconcile, capacity, exposure, and every report aggregate's "clean" lane. Spec the test that proves it: a fixture with one OK + one REVIEW arb where only the OK reaches clean P&L.

### F3 — `position_key` and dedup
`position_key = (sport, event_key, outcome, market_type, line)`. One economic position, however many times re-flagged (V1: 1,280 arb rows = 149 positions; 661 = 148). Spec the "improved-quote" rule (re-flag links to the existing position; better price updates the quote history, never creates a new position for aggregates). **Every** aggregate dedups by position_key first.

### F4 — De-vig + calibration methodology *(absorbed from Fable 5)*
- Choose Shin or power vs proportional de-vig **empirically**: minimize Brier/log-loss on V1's settled outcomes (the 2.1 GB quote archive + 878 settled EV bets are your training fixture). Freeze the winner and the formula.
- `p̂` is a **distribution, not a point**: carry consensus SE/IQR; the EV gate is `edge_points / SE`, not raw edge (EV#3 — the deepest problem).
- Calibration: isotonic (or Platt if n too small) fit on settled **raw** quotes, not flagged-only; applied as `p̂_calibrated`; per-segment ECE/Brier-skill gates (EV#10).
- **Invert the edge handling (R5).** The data shows win rate *falls* as apparent edge rises (51% at <2% edge → 31% at 5–10%). Spec a continuous down-weight or hard gate on large edges; sort-by-edge ranking is forbidden. Scope EV to MLB (the only covered, calibrated segment); Polymarket is the only venue with positive EV signal (+$34.57) — prefer it for any V2 test.
- Staleness: continuous exponential decay of consensus edge (EV#7), not a binary cliff.

### F5 — Friction model *(absorbed from Fable 5 + Opus items)*
- Kalshi quadratic fee `rate·P·(1−P)`; Polymarket linear; **SX winning-bet fee modeled, not 0** (R4/D3 — 556/1,280 arbs are polymarket+sx and every one is overstated today).
- Neg-risk basket uses **linear** fee on both sides (C0).
- `depth_usd` normalized per venue to "taker $-notional executable at this level," with a fillable-fraction haircut; single-outcome depth may never fall back to event-level liquidity (B3).
- Slippage = `f(stake/depth)` + volatility term (EV#6), replacing the flat buffer.
- Leg skew is a **continuous edge haircut**, actionable window ≤10 s (C1).

### F6 — Capacity honesty rule
Capacity returns `UNKNOWN` while `n_with_ladder = 0`. Weighted-ARB sizing (`ladder_scaled_stake`) stays gated behind `arb_size_by_ladder: false` until real multi-level ladders are captured (Polymarket per-token `/book`, B4/R8) *and* the capacity report shows genuine edge-limited arbs. Never a headline number before that. (B1/B2 — the "$72,209" fantasy.)

### F7 — `PREREGISTRATION_V2.md`
Frozen before V2 collects data: hypotheses per class **split by venue pair** (R3 — kalshi+poly ~88% vanish vs poly+sx ~5% are different populations and must never share a win rate); CLV measured against the **no-vig closing mid** (EV#9); calibration gates per segment; n-thresholds; the EV go/no-go bar (EV betting stays disabled until it clears).

## 3. Data-review fixes to wire into the spec (R1–R8)

| # | Decision you must freeze | Where it lands |
|---|---|---|
| R1 | Flash verifier's ~10 s re-quote price **is** `confirmed_cost` for arbs (fixes 5/1,280 confirmed). | Phase 5/6 spec |
| R2 | Headline = full-sample **optimistic**; confirmed shown only as a paired delta with a survivorship label. | Reporting spec |
| R3 | Venue-pair split in every arb table, card, and aggregate. | Schema (`venue_pair` column) + reporting |
| R4 | SX priced honestly: winning fee + real/thin depth. | F5 |
| R5 | EV edge inversion gate; EV scoped to MLB; Polymarket-first. | F4 |
| R6 | Poly/SX leg resolvers so no bet is unreconcilable (A2); `edge_points` stored on ARB rows; dedup by position_key. | Phase 6 spec |
| R7 | UI guardrails: Kelly+confirmed never standalone; empty A/B panels hidden; one live surface (`:8787`), no stale static export. | Phase 8 / reporting |
| R8 | Book reads via WebSocket where allowed (Polymarket first, keyless); REST fallback + discovery/settlement. Local live book per venue; detection compares genuinely simultaneous prices. | Feeds spec, Phase 3 |

## 4. Handoff protocol (what you give Sonnet 5, every phase)

1. A **task spec**: exact function signatures, I/O types, `config.yaml` keys read, rules in prose, failure modes to guard, and **worked numeric vectors to the cent**.
2. The **failing tests** (or their exact assertions), written by you first.
3. Acceptance command for the phase.
4. Sonnet may not edit a vector; if it escalates, *you* resolve and re-freeze.

Minimum vector set for Phase 2: proportional + chosen de-vig on a 3-book fixture; consensus median + SE; `edge_points`; Kalshi quadratic fee; linear fee; SX taker inversion from a recorded fixture; true-arb `S`, ROI, stake split; divergence spread; depth-aware slippage; calibration mapping; staleness decay. Every value computed by you, by hand or by throwaway script, and stated to the cent.

## 5. VERIFY — your second hat

After each phase, audit the diff against [[02 - Weaknesses and Lessons]]. Checklist per phase in [[03 - V2 Build Instructions]] §4. Priorities:

> [!danger] Phase 6 is where V1 died — verify hardest here
> Grep **every** money-booking path (detect, fill, settle, reconcile, capacity, exposure, report-clean-lane) and confirm each imports and calls the **same eligibility predicate object** — not a re-implementation. Prove with tests: (a) OK + REVIEW fixture → only OK settles into clean P&L; (b) `placed_at > start` → `INVALID`; (c) a Polymarket-leg and an SX-leg bet both actually reconcile; (d) an aggregate over 5 rows / 1 position reports n=1.

Also verify, project-wide: no float in money columns; no vector was edited (diff the vector files); `Decimal` end-to-end; every adapter has a fixture test that predates its live use; SX inversion test exists before SX touches the pipeline; reports label optimistic/confirmed × raw/clean × flat/Kelly explicitly (the honest-variant matrix).

## 6. Definition of done — the whole project's gate

V2 is "better than V1" only when your VERIFY pass confirms: **one eligibility predicate gates detection, fill, settle, reconcile, capacity, and exposure, with a passing test that a REVIEW arb never reaches clean/headline P&L** — plus R1 (arbs have a real confirmed column) and R2 (no survivorship headline) hold in the shipped reports.

## 7. Kickoff prompts (copy-paste)

> **Plan:** *"You own SPEC_V2.md for ArbModel V2 (architecture AND methodology — you absorb Fable 5's F4/F5/F7). Read the four context notes in ArbModelV2_Plans, then produce SPEC_V2.md, PREREGISTRATION_V2.md, and the full test-vector appendix. Freeze the eligibility predicate, position_key, de-vig/calibration choice (fit on V1's settled data), friction model incl. SX winning fee, capacity honesty rule, and the R1–R8 data-review fixes. Nothing left TBD."*

> **Verify (per phase):** *"Audit the Phase N diff against SPEC_V2.md §N and 02 - Weaknesses and Lessons. For Phase 6: grep every money-booking path and prove the shared eligibility predicate gates each one; run the REVIEW-arb, post-start, and reconcile tests. Reject the phase if any vector was modified."*
