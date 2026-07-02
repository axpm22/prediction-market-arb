---
title: Cowork Audit & Research Prompt — reusable
type: prompt
project: ArbitrageModel
status: stable
tags: [arbmodel, prompt, audit, research, cowork]
created: 2026-07-01
---

# Instructional Prompt — Claude Cowork: find errors / research improvements

> [!abstract] How to use
> Paste the block below into a fresh Claude Cowork session with the `ArbitrageModel` folder selected. It encodes the methodology that produced [[04 - Code Audit Findings (New)]] (K1–K17). Run it periodically or after big changes; each run should extend the K-series, never repeat it.

---

## The prompt (copy everything in this block)

> You are auditing **ArbitrageModel**, a paper-only cross-venue prediction-market measurement harness (Python 3.11, SQLite, append-only ledger). Your job: **find errors that distort recorded results, and research concrete ways to improve the model.** New findings only — most of the obvious problems are already documented.
>
> **Hard guardrails (never violate):**
> 1. Read-only everywhere except the findings note. Open `data/arb.sqlite` with `sqlite3.connect("file:data/arb.sqlite?mode=ro", uri=True)` only. Never run `scan`, `settle`, `reconcile`, `watch`, or anything that mutates the ledger.
> 2. Never make a live API call to any venue or The Odds API — no credits, no exceptions. Web research uses public docs pages only.
> 3. Don't edit code, tests, or config; findings go in the note (fixes are a separate, explicitly-requested task).
>
> **Step 1 — Load the known-issues baseline (so you don't duplicate).** Read, at minimum: `ArbModelV2_Plans/00–04` (especially `04 - Code Audit Findings (New).md` — the K-series is yours to extend), `ARB_WEAKNESSES.md`, `EV_WEAKNESSES.md`, `MODEL_ISSUES_SWEEP.md`, `MODEL_ISSUES_SWEEP_2.md`, `CRITICAL_AND_HIGH.md`, `FIXES_REPORT.md`, `EV_MODEL_DEEP_REVIEW.md`. Anything already tagged (A/B/C/D/E/G/H/I/J/L/N/S/X, EV#1–10, R1–R8, K1–K17) is out of scope unless you can show the fix regressed or is incomplete.
>
> **Step 2 — Audit the code with a bias toward paths that book or grade money.** Highest-yield places, in order:
> - **Entry/verify/settle consistency**: any quantity computed twice (a leg's effective cost, a fee, a buffer, an edge) is a bug farm — diff the implementations line by line (this is how K1/K2 were found). Check every `all_in_fee_cost` call site for the right `basis`, every re-quote path against its adapter's entry pricing.
> - **Rounding and units**: `money()` vs `q6()` on small magnitudes (K10 was cent-rounding erasing sub-0.5% arb P&L); dollars vs contracts vs maker-vs-taker notional; ratio vs probability-points edges.
> - **Timing honesty**: everything stamped `fetched_at`, `placed_at`, or compared across clocks — is the stamp from when the data was actually read (K11)? Do guards use the same clock on both sides?
> - **Gates and filters**: does the population a metric is computed on match the population it claims (K12's CLV gate silently dropping 39% of flags)? Do all surfaces (CLI, dashboard, webserver) show the same default metric (K13)?
> - **Silent failure handling**: `except:`-and-continue around fetches, missing-data fallbacks that invent values (K3's close storms, K9's invented starts, K17's liquidity fallback).
> - **Untested live paths**: anywhere tests inject mocks, read the real implementation those mocks replace — that is where K1 hid.
>
> **Step 3 — Verify every candidate against the live ledger before it becomes a finding.** A claim without a number is a hypothesis: query the read-only DB, quantify the impact (rows affected, dollars, points, percentage of a headline metric), and state the query's logic in the finding. Discard anything that can't move a recorded result by at least ~0.05% or distort a decision gate.
>
> **Step 4 — Research improvements (separate from errors).** Use web search on: venue API capabilities (WebSocket feeds, fields we're not capturing — e.g. bids/spreads, per-token books), de-vig and calibration literature (Shin/power, isotonic, proper scoring), execution-microstructure practice (depth-aware slippage, maker vs taker), and anything the docs list as an open question. Every recommendation must name the config key/schema column/module it would touch and what evidence would validate it. Respect guardrail 2: docs and papers only, no live endpoints.
>
> **Step 5 — Write up.** Append findings to `ArbModelV2_Plans/04 - Code Audit Findings (New).md`, continuing the K-numbering (check the current max first). Per finding: file:line, what's wrong, **measured impact from Step 3**, the fix, and which V2 phase/spec section it lands in ([[03 - V2 Build Instructions]]). Update the priority table and the `_MOC` link line. Keep the existing note's format (Obsidian callouts, frontmatter untouched except nothing). If a prior K-item turns out wrong, correct it in place and say why.
>
> **Quality bar:** prefer 3 quantified findings over 10 speculative ones. If a pass finds nothing new above the threshold, say so plainly — a clean audit is a valid result. Never pad.

---

## Variants

- **Errors-only sweep** (faster): drop Step 4 and add "focus exclusively on Steps 2–3."
- **Research-only**: drop Steps 2–3 and add "produce an improvement memo, not K-findings — save as `ArbModelV2_Plans/0X - Research - <topic>.md`."
- **Regression check after fixes**: replace Step 2 with "for each K-item marked fixed in the latest commit, prove the fix held: re-run the Step-3 query that originally quantified it and compare."

Related: [[04 - Code Audit Findings (New)]] · [[03 - V2 Build Instructions]] · [[_MOC - ArbModel V2]]
