---
title: MOC — ArbModel V2
type: moc
project: ArbitrageModel
tags: [arbmodel, moc, index]
created: 2026-07-01
---

# 🗺️ MOC — ArbModel V2

> [!abstract] What this is
> The map of content for planning **ArbModel V2** — an improved rebuild of the paper-only cross-venue arbitrage harness. Start at the top and read down.

## Read in order

1. [[01 - Current Model - How It Works]] — how V1 works today (goal, architecture, ledger, findings).
2. [[02 - Weaknesses and Lessons]] — everything V1 got wrong that V2 must fix.
3. [[03 - V2 Build Instructions]] — the model-assigned build plan (🧠 plan / ⌨️ code / 🔍 verify).
4. [[04 - Code Audit Findings (New)]] — K1–K20, new issues from the July code+ledger audit (K1/K2 are prerequisites of R1; K10 partly explains the "$0 arb P&L" headline).
5. [[05 - Cowork Audit and Research Prompt]] — reusable prompt to re-run this audit / research improvements (extends the K-series).
6. [[06 - Opus 4.8 Complete Build Guide]] — the complete from-scratch build guide (architecture, schema, config, engine rules, UX/UI spec, tests, phases) folding in R1–R8 and K1–K20. **Supersedes [[Model - Opus 4.8]] for a solo Opus build.**

Per-model instructions: [[Model - Fable 5]] · [[Model - Opus 4.8]] · [[Model - Sonnet 5]].

Visual: `ArbModel V2.canvas` (open in Obsidian).

## The one idea to remember

> [!danger] V2's whole reason to exist
> V1's math was honest but its **money-booking paths ignored detection's safeguards** — the settled arb book was 100% forbidden REVIEW arbs. V2 enforces **one eligibility predicate at every stage**. If V2 delivers only that, it is already better than V1.

## Model assignment at a glance

| | Role | Owns |
|---|---|---|
| 🧠 **Fable 5** | Methodology & math | de-vig/calibration, `p̂` uncertainty, pre-registration, friction formulas |
| 🧠 **Opus 4.8** | Architecture & spec | eligibility predicate, `position_key`, schemas, capacity honesty, review |
| ⌨️ **Sonnet 5** | Implementation | code from a frozen spec + failing tests; may not edit a vector |
| 🔍 **Opus 4.8 / Fable 5** | Verification | diff audit vs weaknesses list; Phase-6 money-path proof |

## Source docs in the repo (V1)
`SPEC.md` · `README.md` · `RESULTS.md` · `ARB_WEAKNESSES.md` · `EV_WEAKNESSES.md` · `PREREGISTRATION.md` · `config.yaml`
