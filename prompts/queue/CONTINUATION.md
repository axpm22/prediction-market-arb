# CONTINUATION — how to finish the master plan from here

Written 2026-07-05. This is the handoff for the remaining queue work. It does **not** replace the
original prompts — `prompts/queue/09..12` and `MASTER_PLAN.md` are still the source of truth. This
file records **what is already done, the current live state, the working method, and a precise
step-by-step for each remaining prompt**, including the concrete blockers found while scoping them.

Read order for whoever picks this up: `CLAUDE.md` → `MASTER_PLAN.md` §2/§4 → `prompts/queue/README.md`
→ this file → the specific prompt you're executing.

---

## 1. Current state (2026-07-05)

**Branch/HEAD:** `main` @ `c5eb4c2`. Tree clean. Full suite **519 passed**.

**Merged (PRs #1–#10):** the whole spine of Stages 0–2 plus the live restart:

| Prompt | What landed | Key decision recorded |
|---|---|---|
| 01 | verify + commit tree (foundations A–F) | — |
| 02 | instant-settle tripwire + `arb check-invariants` | — |
| 04 | Phase A regrade → **EV lane FROZEN** | `ev_enabled: false`, `devig_method: power` (PREREG_V2 §7) |
| 05 | fee corrections: Polymarket **quadratic**, SX parlay 5%, Kalshi re-verified | cost vectors re-frozen (PREREG_V2 §8) |
| 06 | Phase B settlement matrix: enforce, startup warning, reconcile retro-check | no sport green → nothing whitelistable (honest) |
| 07 | Phase C markouts: persist hook + `arb markout` + archive dry-run | archive markout ≈ 0 / slightly favorable → no adverse selection (sanity only) |
| 08 (slice) | Phase F cadence honesty in the daily report | watch p50 124s / p95 165s — measured is the spec |
| 08b | `consensus_age_secs` + `leg_skew_secs` as real columns | tracks the post-restart week |
| 03 | **baseline cutover (Option A) + supervised restart** + stop-all safety | PREREG_V2 §6; measurement clock restarted |

**Live system (RUNNING):**
- `arb supervisor` is up (watch loop, ~30s cadence, **keyless — no Odds API credits**).
- Fresh post-cutover `data/arb.sqlite`; pre-cutover data archived to
  `data/arb-incident-baseline-20260703.sqlite` (507 bets / 616,889 quotes, read-only).
- V1 oracle archive: `data/archive-pre-v2/arb.sqlite` (analysis only).
- Config: `ev_enabled: false`, `devig_method: power`, `consensus_enabled: true`,
  `trusted_pairs: []`, Polymarket quadratic fees.

**Operating the live system:**
- **STOP:** `touch data/stop-all` — the supervisor exits within one loop tick (~30s). NOT `pkill`.
- **START:** `nohup env PYTHONUNBUFFERED=1 .venv/bin/python -m arb supervisor >> logs/supervisor.log 2>&1 &`
  (remove `data/stop-all` first).
- **Restart after merging code** (loops run old code until restarted): stop, then start. The
  supervisor warns once when the working tree HEAD changes.
- Health: `python -m arb status`, `python -m arb check-invariants`, `python -m arb report`.
- **Known cosmetic artifact:** `arb status` shows watch `DEAD` between cycles because the supervisor
  runs `watch --once` and relaunches it (not a persistent beating loop). The loop is fine — quotes
  accumulate. A Phase-D polish item, not a blocker.

---

## 2. Working method (keep doing this)

1. **One branch + PR per prompt**, off `main`: `git checkout -b stageN-XX-slug main`.
2. **Verify against recorded data, read-only.** The archive (`data/archive-pre-v2/arb.sqlite`) and the
   baseline are opened `Store(path, read_only=True)` / `file:...?mode=ro`. Never mutate them
   (`stat -f %Sm` before/after to prove mtime unchanged).
3. **Test discipline:** fixtures only, no Odds API credits, `Decimal` money, `flat_bets` append-only.
   Add the prompt's frozen tests; run the full suite; paste acceptance output.
4. **Decision rules → `PREREGISTRATION_V2.md`** as a dated amendment, with the trigger numbers, the
   moment one fires.
5. **Merge:** `gh pr merge <n> --rebase --delete-branch`. Independent branches off `main` conflict
   only on shared files (cli.py subparsers, tracker.py report) — resolve by keeping both, then
   `git rebase origin/main` locally, force-push, merge (this repo's PRs #5–#8 show the pattern).
6. **After a code merge that the live loop must use, restart the supervisor** (see §1).

Standing hard rules (from `CLAUDE.md`, restated in every prompt): paper only; no auth/order
endpoints; no invented endpoints; persist every quote; cross-venue REVIEW-only unless whitelisted
**and** (after 06) the matrix section is green; every threshold in `config.yaml`.

---

## 3. Remaining work, in order

### Gate first: Stage 2 → Stage 3
`prompts/queue/README.md` gates Stage 3 on **"one week of clean supervised data days, then review."**
The clock started at the 2026-07-03 restart. **09 does not strictly need that week** (it's a
structural refactor, testable on fixtures) — but the *review* of 09/10/11's effect on real numbers
does. Practically: **09 can proceed now**; hold the Stage-3 STOP review until ~2026-07-10 so the
before/after is judged on a week of clean data.

---

### 09 — eligibility predicate + `position_key`  (`prompts/queue/09-eligibility-predicate-position-key.md`)

**This is the highest-risk prompt: serial, money-path everywhere, behavior must stay identical.**
Do it as its own focused pass. **Read `SPEC_V2.md` §3 and §4 in full first** — that is the design
source for the predicate and the position model; do not reconstruct it from memory.

**Blockers found while scoping (resolve these deliberately, don't wing them):**

1. **`depth_basis` does not exist in V1.** The predicate clause `depth_basis == "real"` references a
   SPEC_V2 field this tree never had. Decide the mapping and record it in the commit: default is
   `depth_usd is not None and depth_usd > 0` (a real observed book). Do **not** add a `depth_basis`
   column (out of scope — only `position_key`/`venue_pair` are allowed new columns).
2. **The predicate must unify two different notions of "clean."** Today: arb clean = `trusted=1`;
   EV/other clean = `quality_flag IS NULL`. SPEC_V2 §3.1 defines the single predicate; map V1 fields
   onto it so that, **for the shipped config, the lane a bet lands in is byte-identical to today.**
3. **It is all-or-nothing.** The prompt's grep test **T-ELIG-5** (`grep -rn "def eligib\|trusted == 1\|trusted==1" arb/` → exactly one module) only passes once **all 15 sites** are deleted and routed
   through the predicate. The current sites (verified 2026-07-05):
   `arb/{cli,bias,arb_sizing,micro_validation,settlement,scoring}.py` and
   `arb/experimental/{negrisk_sweep,webserver,ev_exit}.py`.

**Execution steps:**
1. `arb/eligibility.py`: one pure function
   `eligibility(bet, cfg, now) -> Eligibility(eligible: bool, reason: str, lane: "clean"|"review"|"invalid")`
   per SPEC_V2 §3.1. Clauses (in order): `placed_at >= start_utc` → `invalid`; else the AND of
   [trusted-or-whitelisted-per-market-type, start known, `placed_at < start_utc`, depth real] →
   `clean`; else `review` with the first failing clause as `reason`
   (`review-untrusted` / `start-unknown` / `placed-after-start` / `depth-unknown`).
2. Add `position_key` (`sport|event_key|outcome|market_type|line`) and `venue_pair` as real columns
   via `MIGRATION_COLUMNS` (see how 08b added `consensus_age_secs`); populate in
   `arb/tracker.py::_insert_tracker_row`. Reuse `arb/arbitrage.py::venue_pair_key`. Confirm the
   doubleheader `game_index` disambiguator is already in `event_key` (it was delivered in June — verify,
   don't re-add).
3. Route **every** money-touching stage through `eligibility()` and **delete** the local checks
   (the 15 sites): detection, tracker fill (the in-play refusal becomes a predicate call), settle,
   reconcile, capacity/`arb_sizing`, exposure, and the report clean-aggregate. For SQL-embedded
   `quality_flag IS NULL` / `trusted=1` filters, either fetch-then-filter through the predicate or
   derive one shared SQL clause from it — whichever keeps numbers identical.
4. Every economic aggregate **dedups by `position_key`**; a re-flag of an active position updates
   quote history, never a new position (reappearance rule unchanged, but reappearances share the
   `position_key`).
5. Historical rows lack the new fields → a **read-time** lane derivation labeled `legacy-derived`.

**Acceptance (all required):**
- `pytest` green **including** T-ELIG-1..5 and T-POS-1/2 (ported verbatim from SPEC_V2 §3.3/§4.2);
  paste the T-ELIG-5 grep output showing one module.
- **Before/after report diff must be empty** for the shipped config, or every differing line
  explained. Capture `python -m arb report` (and a fixture daily render) before starting on a branch,
  and after; `diff` them. Any behavioral delta goes in the commit message explicitly.
- `python -m arb report` against the live DB renders with historical rows in `legacy-derived`.
- **STOP for review** (definition of done: SPEC_V2 §17 — one predicate object gates detection, fill,
  settle, reconcile, capacity, exposure; T-ELIG-1 proves a REVIEW arb never reaches clean/headline P&L).

---

### 10 — remove DIVERGENCE  (`prompts/queue/10-remove-divergence.md`)  — needs 09
Wraps the existing `REMOVE_DIVERGENCE_PROMPT.md`. Divergence generates ~1,600 no-op opportunities and
~177 zero-stake rows per active day of ledger noise. Stop generating it (quotes remain the recompute
source — no data is lost). Do it **after 09** so it's removing a lane the eligibility predicate already
understands. Acceptance: divergence detection off, reports recompute from quotes without it, suite green.

### 11 — UX/UI consistency  (`prompts/queue/11-ux-ui-consistency.md`)  — needs 09 + 10
Wraps `UX_UI_CONSISTENCY_PROMPT.md`. Three surfaces (CLI report, dashboard, webserver) currently
compute numbers independently. After 09 this is cheaper: one `arb/metrics.py` reading the **one**
eligibility predicate, consumed by all three. Acceptance: a single metrics module; the three surfaces
show identical numbers; **STOP — end of Stage 3.**

### 12 — negrisk exhaustive + maker shadow lane  (`prompts/queue/12-negrisk-exhaustive-maker-shadow.md`)
Measurement only; gated by its own pre-registered "real if" (PREREGISTRATION_V2 §4) and by Stages 0–2
+ 05. Re-scan the archive negrisk under the **corrected quadratic fees** (05 already flipped negrisk to
quadratic via `FEE_BASIS`) — 05's offline read found no basket flips null→positive, so expect a null
unless live scanning at scale surfaces one. Add the maker-side shadow lane (log the counterfactual
maker edge; never booked). Only promote on a pre-registered forward test, never on the flip fraction.

---

## 4. Deferred sub-items (noted in their PRs, pick up when convenient)

- **08 start-time hierarchy (the remaining Phase F piece):** grade every start `verified|nominal|absent`
  with the **MLB Stats schedule outranking venue times** (already a settlement dependency — reuse it),
  stored on the quote/event path so analysis can filter by grade. `arb/matching.py::start_grade` and
  `consensus_age_secs`/`leg_skew_secs` columns are done; this is the last Phase-F item. Also the
  cadence **decision** (parallelize venue fetches to actually hit 60s, or leave measured-is-the-spec).
- **07 polish:** daily-report markout line; per-class/venue-pair/edge-bucket markout breakdown; wire
  the **flicker guard** (`markout.zombie_fillable`, already tested) into the live tracker booking path
  so single-appearance quotes are recorded, never booked.
- **Phase-D polish:** make `arb status` liveness match the `watch --once` relaunch model (no false DEAD).

---

## 5. Decision rules — executed vs pending

**Executed and recorded in `PREREGISTRATION_V2.md`:**
- §7 Phase A: EV bet creation frozen (venue mids beat consensus on Brier; EV-subset 0.479).
- §8 Fees: cost vectors re-frozen (Polymarket quadratic; archive negrisk = honest null).
- §6 Cutover: pre-cutover data is analysis-only, never blended into post-cutover P&L.

**Pending (fire on real post-restart data):**
- Phase C markout: if +2min markout eats > half a class's average flagged edge, switch that class's
  headline to `pnl_markout`. Archive sanity ≈ 0; the real verdict needs live rows.
- EV re-open: only if a regrade re-run shows consensus beating venue mids on Brier **and** EV-subset
  win rate > 0.5 (CI excludes 0.5).
- Trusted-pair whitelisting (the only path to headline cross-venue P&L): needs 06 **green section** +
  **100 reconciled positions/pair** (PREREGISTRATION_V2 §4). Weeks away by construction — nothing
  shortcuts it.

---

## 6. One-paragraph summary for a cold start

Stages 0–2 and the live restart are done and merged (`main` @ `c5eb4c2`, 519 tests green); the
supervised system is running keyless with EV betting frozen and is accumulating clean data since the
2026-07-03 cutover. The remaining plan is Stage 3 (09 eligibility predicate → 10 divergence removal →
11 UX consolidation) then Stage 4 (12 negrisk/maker measurement). Start with **09**, but do it as a
careful serial pass: read `SPEC_V2.md` §3/§4, build one predicate that reproduces today's lane
assignment exactly, migrate all 15 "clean" sites in one go, and prove the before/after report diff is
empty. Hold the Stage-3 review until ~a week of clean data is in. Stop the live system with
`touch data/stop-all`.
