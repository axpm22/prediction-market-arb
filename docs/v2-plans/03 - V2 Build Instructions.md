---
title: V2 Build Instructions — model-assigned
type: plan
project: ArbitrageModel
status: active
tags: [arbmodel, v2, build-plan, model-assignment]
created: 2026-07-01
---

# V2 Build Instructions (Model-Assigned)

> [!abstract] Mission
> Build **ArbModel V2**: same goal as V1 (an honest, paper-only, append-only measurement harness for cross-venue prediction-market edge), very similar in shape, but **structurally sound** where V1 leaked. The organizing principle of V2 is one V1 never had: **a single eligibility predicate, defined once and enforced at every stage.**

Read first: [[01 - Current Model - How It Works]] · [[02 - Weaknesses and Lessons]]

---

## 0. How to read this note

Every task carries a **model tag**. The rule is simple:

> [!important] Division of labor
> **Hard thinking → the strong models. Mechanical typing → the cheap model.**
> - **Fable 5 / Opus 4.8** own everything where a wrong decision is expensive and silent: methodology, math derivation, spec freezing, architecture, code review, and final verification. These are the tasks V1 got wrong not because the code was buggy but because the *thinking* was incomplete.
> - **A weaker/cheaper coding model** (e.g. Sonnet-class) writes code **only from a frozen spec** — where the answer is already decided and the job is faithful transcription plus passing pre-written tests.

Tags used below:
- 🧠 **PLAN (Fable 5 / Opus 4.8)** — design, derive, decide, freeze.
- ⌨️ **CODE (weaker model)** — implement a frozen spec, make given tests pass.
- 🔍 **VERIFY (Opus 4.8 / Fable 5)** — independent review before the phase is called done.

---

## 1. Why the split is drawn here (rationale)

V1's post-mortem ([[02 - Weaknesses and Lessons]]) shows the failures were **decision failures, not syntax failures**:

- *Where should the eligibility filter live?* (A1) — an architecture call.
- *Which de-vig method is least biased?* (EV#1) — an empirical/statistical call.
- *Is `p̂` a point or a distribution?* (EV#3) — a modeling call.
- *What counts as "simultaneous"?* (C1) — a methodology call.

None of these is a typing problem; all of them were gotten wrong once. So the strong models must **finish the thinking and write it down as a frozen spec + test vectors** before any code is written. The weak model then cannot re-introduce the error, because the decision is no longer open.

> [!note] Guardrails carried verbatim from V1 (non-negotiable, all phases)
> Paper only, public GETs only. No magic numbers (all thresholds in `config.yaml`). **`Decimal` for all money math.** Cross-venue is **REVIEW-only** unless whitelisted. `flat_bets` is **append-only**. Never spend Odds API credits in tests/loops — tests run on recorded fixtures. Fixtures-first for every adapter.

---

## 2. Handoff protocol (the contract between models)

This is the mechanism that makes the cheap coder safe. For **every** code task:

1. 🧠 **PLAN** produces a **task spec** containing: exact function signatures, input/output types, the `config.yaml` keys it reads, the eligibility/edge rules in prose, **worked numeric test vectors to the cent**, and the list of failure modes it must guard.
2. 🧠 **PLAN** writes the **failing tests first** (or their exact assertions) so acceptance is objective.
3. ⌨️ **CODE** implements until `pytest` is green — and is **forbidden from changing a test vector**. If a vector seems wrong, it stops and escalates to PLAN.
4. 🔍 **VERIFY** (a *different* strong model than the one who planned, when possible) reviews the diff against the spec and the [[02 - Weaknesses and Lessons]] checklist before the phase closes.

> [!warning] The one rule that would have prevented V1's headline bug
> A phase is **not done** until VERIFY confirms the phase's eligibility/guard rule is enforced *at every stage that touches it*, not just at detection. VERIFY greps for the money-booking paths and checks each one applies the shared predicate.

---

## 3. Foundational design work (do this before Phase 1) — 🧠 PLAN

These are pure-thinking deliverables. **Fable 5 / Opus 4.8 only.** No code.

| # | Deliverable | Owner | Why strong-model |
|---|---|---|---|
| F1 | **`SPEC_V2.md`** — the single source of truth, freezing every decision below. | Fable 5 (draft) → Opus 4.8 (review) | V1's spec left settle-time eligibility unspecified; that gap became the headline bug. |
| F2 | **Eligibility predicate**, defined once: `eligible = trusted=1 OR venue_pair ∈ trusted_pairs`, plus started/final and depth-known guards. Specify the exact stages it must gate. | Opus 4.8 | Fixes A1/A3/C5/D4 by construction. |
| F3 | **`position_key` definition** `(sport, event_key, outcome, market_type, line)` and the dedup/"improved-quote" rule. | Opus 4.8 | Fixes B2/C2 — the counting inflation. |
| F4 | **De-vig + calibration methodology**: Shin/power vs proportional, chosen by Brier/log-loss; `p̂` as a distribution (carry SE); calibration via isotonic on settled **raw** quotes. Freeze the formulas + test vectors. | Fable 5 (derivation) → Opus 4.8 (statistical review) | EV#1/#3/#5 — the estimation core. Getting this wrong biases every EV flag silently. |
| F5 | **Friction model**: depth-aware slippage `f(stake/depth)`, per-venue normalized `depth_usd`, SX winning-bet fee, linear neg-risk fee. Freeze formulas + vectors. | Opus 4.8 | B1/B3/C0/D3/EV#6 — all fee/depth mispricing. |
| F6 | **Capacity honesty rule**: capacity returns `UNKNOWN` until real ladders exist; never a headline number while `n_with_ladder = 0`. | Opus 4.8 | B1/B2 — refuse fantasy over invention. |
| F7 | **Pre-registration V2** (`PREREGISTRATION_V2.md`): hypotheses, gates (CLV against **no-vig mid**, calibration ECE/Brier per segment), and n-thresholds — frozen *before* data. | Fable 5 → Opus 4.8 | Makes null results citable; EV#9/#10. |

> [!tip] Output of section 3
> A frozen `SPEC_V2.md` + `PREREGISTRATION_V2.md` + a folder of test vectors. Only after these exist does any coding begin.

---

## 4. Build phases

Each phase: PLAN writes the spec+tests → CODE implements → `pytest` green → VERIFY reviews → acceptance command passes. Order is strict; do not start a phase until the prior one's acceptance passes.

### Phase 1 — Skeleton & config
- 🧠 **PLAN**: lock `config.yaml` schema (every V1 knob + new ones from F2–F6), `models.py` dataclasses incl. `position_key` and eligibility fields, SQLite schema (append-only `flat_bets`, `quotes`, `polls`, `opportunities`, `consensus`, `annotations`). *This is a design task — signatures and schema are decisions.*
- ⌨️ **CODE**: create the package, config loader, store with WAL, empty-frame `report`.
- 🔍 **VERIFY**: schema supports append-only correction rows; no float in money columns.
- **Acceptance:** `arb report` prints empty frame; DB created; `pytest` green.

### Phase 2 — Math core (the frozen vectors)
- 🧠 **PLAN**: freeze **all** test vectors — de-vig (proportional + Shin/power), consensus median, EV as `edge_points`, Kalshi quadratic fee, linear fee, SX taker `1 − pct/1e20`, true-arb `S`/ROI/stakes, divergence spread. Include the F4/F5 additions (SE, calibration, depth-aware slippage).
- ⌨️ **CODE**: implement `pricing.py` until every vector passes. **May not edit a vector.**
- 🔍 **VERIFY**: recompute two vectors by hand; confirm `Decimal` throughout; confirm `edge_points` (not the ratio) is the stored edge (EV#4).
- **Acceptance:** all §Math vectors green.

### Phase 3 — Venue adapters (fixtures-first)
- 🧠 **PLAN**: for each venue, specify field mapping, the **normalized `depth_usd`** definition (F5), the ladder-capture requirement (Polymarket per-token `/book`, B4), Kalshi `start-unknown` handling (D1), SX maker-side inversion + winning-fee (D3), and a recorded fixture per venue per market type.
- ⌨️ **CODE**: write adapters to parse fixtures; unit-test each `depth_usd` against its recorded book.
- 🔍 **VERIFY**: SX inversion test present *before* SX touches the pipeline (hard rule #10); no event-level liquidity fallback for single-outcome depth (B3).
- **Acceptance:** adapters parse fixtures; one live `watch --once --dry-run` shows quote counts; credits logged.

### Phase 4 — Matching
- 🧠 **PLAN**: freeze alias rules, 6h bucket, exact-line rule, **start-time clustering ≤2h for doubleheaders (D2)**, sanity gates (30-min drift, 15-pt suspect), non-sports manual-only flow.
- ⌨️ **CODE**: implement `matching.py`; fixture: same MLB game on 3 venues → one `event_key`; doubleheader → two.
- 🔍 **VERIFY**: doubleheader fixture does not cross-match; suspect rows written, not dropped silently (E2).
- **Acceptance:** matching fixtures pass.

### Phase 5 — Detection + display (eligibility enforced here AND downstream)
- 🧠 **PLAN**: specify `detect_true_arbs` with the F2 predicate applied **at detection**, the started/final guard (C5), continuous skew haircut (C1), and the diagnostic-row rule for rejects (E2). Specify the neg-risk detector with the **linear** fee (C0).
- ⌨️ **CODE**: implement detection; persist every quote even when no opp found (hard rule #7).
- 🔍 **VERIFY**: confirms the eligibility predicate is the *same object* used later in settle/capacity (no divergent copy).
- **Acceptance:** live `watch --once` end-to-end; quotes persisted; REVIEW arbs never enter the clean lane.

### Phase 6 — Ledger, settlement, reconciliation (the deliverable)
- 🧠 **PLAN**: freeze the tracker spec (dual-fill, lifetime/dedup by `position_key`, reappearance linking), the **shared settle-eligibility filter (A1)**, the **timing guard (A3)**, and the **multi-venue reconcile design (A2)** — `polymarket_leg_result` + `sx_leg_result` + venue-composed resolver, provisional labeling until reconciled. Freeze the daily-report contents.
- ⌨️ **CODE**: implement tracker, settlement, reconcile, daily CSV/MD writer.
- 🔍 **VERIFY (highest priority phase)**: fixture with one OK + one REVIEW arb → only OK produces an actionable SETTLED row; post-start bet → `INVALID`; a Polymarket-leg bet actually reconciles (not skipped). This is where V1 failed — verify hardest here.
- **Acceptance:** §9-style tracker vector passes to the cent; 3-poll fixture produces correct CSV; `report --daily` recomputes idempotently.

### Phase 7 — EV path (only after 1–6, and only measuring)
- 🧠 **PLAN**: specify calibrated `p̂` (F4), edge/SE gate (EV#3), staleness decay (EV#7), correlation-aware exposure (EV#8), CLV vs no-vig mid (EV#9), per-segment calibration gate (EV#10). EV betting **stays disabled** until the pre-registered CLV/calibration bar clears.
- ⌨️ **CODE**: implement against the frozen formulas + vectors.
- 🔍 **VERIFY**: CLV uses no-vig mid; ratio edge never consumed by sizing; EV disabled by default.
- **Acceptance:** EV flags recorded with clean `edge_points`, SE, and calibration status; nothing auto-bet.

### Phase 8 — Capacity, exposure, lower-risk theses
- 🧠 **PLAN**: capacity honesty rule (F6), portfolio/capital binding (C3), correct holding horizon (C4), neg-risk basket (C0 fixed), favorite-longshot calibration, lead-lag with null-correction.
- ⌨️ **CODE**: implement; capacity returns `UNKNOWN` while ladders absent.
- 🔍 **VERIFY**: no headline profit printed while `n_with_ladder=0`; aggregates dedup by `position_key` and filter to actionable.
- **Acceptance:** capacity report shows real `edge`-limited arbs or honest UNKNOWN.

---

## 5. Model-assignment summary

| Work type | Model | Examples |
|---|---|---|
| Methodology, statistics, math derivation | 🧠 **Fable 5** (draft) | de-vig/calibration choice, `p̂` distribution, pre-registration, friction formulas |
| Architecture, spec freezing, eligibility design | 🧠 **Opus 4.8** | eligibility predicate, `position_key`, schema, capacity honesty rule |
| Frozen-spec implementation | ⌨️ **Weaker/cheaper coder** | adapters, tracker, report writer, CLI — from a spec + failing tests |
| Independent review & final verification | 🔍 **Opus 4.8 / Fable 5** | diff review vs [[02 - Weaknesses and Lessons]], Phase-6 money-path audit |

> [!danger] The single acceptance gate for the whole project
> V2 is only "better than V1" when a strong-model VERIFY pass confirms: **the same eligibility predicate gates detection, fill, settle, reconcile, capacity, and exposure — with a test proving a REVIEW arb never reaches clean/headline P&L.** That one guarantee is the entire point of the rebuild.

---

## 6. Suggested prompts to kick off (copy-paste)

- To **Fable 5** (foundational): *"You are the methodology owner for ArbModel V2. Read [[01 - Current Model - How It Works]] and [[02 - Weaknesses and Lessons]]. Produce F4 and F7: freeze the de-vig + calibration methodology and the pre-registration, with worked numeric test vectors. Do not write implementation code."*
- To **Opus 4.8** (architecture): *"You own SPEC_V2.md. Define the single eligibility predicate (F2), the position_key (F3), the config schema and SQLite schema (Phase 1), and the handoff test vectors. Freeze decisions so a cheaper coder cannot reintroduce V1's A1/B2/C2 bugs."*
- To the **coder model** (per phase): *"Implement Phase N of ArbModel V2 strictly from SPEC_V2.md §N and the provided failing tests. Do not modify any test vector. Make `pytest` green. If a vector looks wrong, stop and report."*
- To the **verifier** (Phase 6): *"Audit the Phase 6 diff against [[02 - Weaknesses and Lessons]] A1/A2/A3. Grep every money-booking path and confirm the shared eligibility predicate gates each one. Prove with a test that a REVIEW arb cannot reach clean P&L."*
