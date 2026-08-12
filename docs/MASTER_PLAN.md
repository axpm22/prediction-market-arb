# MASTER_PLAN — audit findings and the road forward

Written 2026-07-02 by a full audit-and-planning pass (docs → git → code → DB → web).
Companion: `prompts/queue/` (the numbered implementation prompts this plan splits into).
Where this plan conflicts with `MODEL_FOUNDATIONS_FIX_SPEC.md`, this plan amends it; both
amend `SPEC.md`. Evidence discipline: every number below was read from `data/arb.sqlite`,
the archives, `git log`, or the working tree during this audit — numbers from the DB outrank
numbers in any document. Where something could not be verified in this session it is marked
**unverified**.

**Operative-path assumption (noted, not asked):** `SPEC_V2.md` (frozen 2026-07-01) describes
a from-scratch V2 rebuild by a PLAN/CODE/VERIFY model trio. Every commit since — the incident
closeout, the foundations work, live baseball — landed on the V1 tree, and
`MODEL_FOUNDATIONS_FIX_SPEC.md` (2026-07-02, the latest word) declares itself a companion to
`SPEC.md`. This plan therefore treats **"harden V1 in place"** as the operative path and
`SPEC_V2.md` as a design document to mine (its eligibility-predicate and position_key ideas
are ported into the roadmap as items F2/F3 below). If a from-scratch rebuild is still the
intent, say so — Stage 3 changes shape; nothing in Stages 0–2 is wasted either way.

No Obsidian vault was reachable from this session (only the repo folder was connected), so
no vault notes are cited.

---

## 1. State of the system (2026-07-02, evening)

### 1.1 Operational

- **Everything is stopped.** `data/stop-all` present (16:25 local). Last watch poll
  2026-07-02T20:34Z; the daemon exited cleanly on the stop flag. Leave the flag until the
  Stage 0 gate clears.
- **The working tree is dirty, and that is the single most important fact in this repo
  right now.** HEAD is `dd7854a` (live-baseball Phase 1), but uncommitted modifications sit
  in `arb/tracker.py`, `arb/settlement.py`, `arb/cli.py`, `arb/arbitrage.py`,
  `arb/matching.py`, `arb/nonsports_ai.py`, `arb/config.py`, plus untracked new modules
  (`arb/heartbeat.py`, `arb/settlement_matrix.py`,
  `arb/experimental/analysis/{regrade,markout,cadence}.py`) and their tests. This codebase
  has been burned twice by "the fix was in the tree while the old process kept betting"
  (2026-07-02 incident; `MODEL_FOUNDATIONS_FIX_SPEC.md` Phase D). The uncommitted work is
  exactly that pattern, one restart away from happening a third time.
- **Tests: unverified.** `pytest` could not run in this audit environment (no package
  installs available). The last committed claim is "443 tests green" (f131d84). Nothing
  since — including the interleaving fix and all six foundations phases — has a recorded
  green run.
- Odds API credits: **492 remaining** (last live log line). Healthy.

### 1.2 The data, honestly

**Live DB (`data/arb.sqlite`, born at the 2026-07-01 v2 cutover; data starts 03:07Z Jul 2):**

| Fact | Value |
|---|---|
| Quotes persisted | 616,889 (03:07Z–20:34Z, one day) |
| Polls | watch 448, ev-exit 1,961, focus 352, negrisk 88, political 17, scan 3 |
| Watch cadence, actual | **~140 s mean** gap vs `watch_interval_secs: 60` (Phase F confirmed in fresh data) |
| flat_bets | EV 186 rows, DIVERGENCE 177, CLOSE 144, **ARB 0**, NEGRISK 0 |
| Settled | 61 EV rows, optimistic +$19.36 / confirmed +$2.23 |
| **Settled net of annotations** | **0 rows, $0.00** — every settled row today is annotated as an artifact |
| Annotations | 315 `dedup-interleave-duplicate`, 48 `settled-rebook-loop`, 20 in-play tennis voids |
| AI matcher | 8 verdicts, **0 positive**; 0 `ai_matched` paper bets |
| Live-baseball lane | 178 probe samples, 91 candidate evaluations, 0 flat_bets (as designed) |
| Consensus | 261 rows, last computed 17:21Z |

The one-line reading: **after one day on the fresh DB, the system has produced zero clean
settled evidence.** The entire day's apparent P&L was manufactured by the two incident bugs
and correctly voided. The measurement clock restarts when the system restarts.

**V1 archive (`data/archive-pre-v2/arb.sqlite`, Jun 10 – Jul 2, read-only):** 2,946,326
quotes; 1,280 ARB rows (315 settled, +$5.37 optimistic, confirmed ≈ NULL-dominated), 3,371
EV rows (881 settled: **−$19.13 optimistic / +$40.64 confirmed** — the R2 survivorship
artifact, not real slippage-beating), 3,420 DIVERGENCE, 4 NEGRISK. Clean-flag settled MLB EV:
627 rows, **−$24.07**. This archive is the input for Phase A regrade and all calibration
fitting — it is oracle data, never P&L.

### 1.3 What is verified implemented vs. not (prompt-by-prompt ground truth)

Checked against code and `git log`, not against the prompts' existence:

| Work item | Status |
|---|---|
| `prompts/stop_inplay_betting_fix.md` (event-level start, tracker in-play refusal, annotations, stale-daemon guard) | **DONE, committed** (f131d84); annotation pass applied (20 rows) |
| `prompts/fix_poll_interleaving_dedup.md` (poll-kind ownership for absent-close) | **Implemented, UNCOMMITTED** (`arb/tracker.py` diff); annotation pass applied (315 rows) |
| `FIX_SETTLED_REBOOK_LOOP_PROMPT.md` #1/#2/#4 (A3 at placement, settled = terminal, future-start settle guard) | in-play refusal committed; settled-terminal in tree; **future-start settle guard UNCOMMITTED** (`arb/settlement.py` diff) |
| `FIX_SETTLED_REBOOK_LOOP_PROMPT.md` #3 (instant-settle **tripwire** in the settle path) | **NOT implemented** (no `settle_tripwire` / `instant-settle` in `arb/`; only the one-shot `loop_audit.py` detector) |
| `FIX_SETTLED_REBOOK_LOOP_PROMPT.md` insurance (`arb check-invariants`) | **NOT implemented** |
| `PURGE_LOOP_PNL_PROMPT.md` | detector (`loop_audit.py`) + annotations applied; report-surface exclusion **unverified** |
| `prompts/incident_recovery_master.md` Step 3 (incident baseline cutover / fresh DB) | **NOT done** — no `data/arb-incident-baseline-20260702.sqlite`; live DB retains (annotated) junk |
| `prompts/incident_recovery_master.md` restart gate | **Not reached**; stop flag correctly still up |
| MODEL_FOUNDATIONS **Phase A** (`arb regrade`) | **Built, UNCOMMITTED, never run in anger** (CLI wired; `tests/test_regrade.py`, `test_devig_regrade.py` exist) |
| **Phase B** (settlement matrix) | **Module + yaml + test built, UNCOMMITTED**; matrix content and trusted-gate enforcement unverified |
| **Phase C** (markouts) | **Built, UNCOMMITTED**; `markouts` table exists, **0 rows** — persist hook never ran |
| **Phase D** (heartbeats/supervisor/status) | **Built, UNCOMMITTED**; one heartbeat row exists (watch @ dd7854a) so it ran from the dirty tree |
| **Phase E** (rules-hash verdict expiry) | **Built, UNCOMMITTED** (`nonsports_ai.py` diff: `rules_sha`, `verdict_is_current`) |
| **Phase F** (cadence honesty) | **Partially built, UNCOMMITTED** (`cadence.py`); consensus-age-on-bet and start-time truth hierarchy not found |
| `SKIP_DEAD_SPORTS_PROMPT.md` (`arb/activity.py`) | **DONE, committed** |
| `FIX_POLYMARKET_UFC_TAG_PROMPT.md` (mma → `ufc` tag) | **DONE, committed**; runbook parity check concluded **REVIEW-only, no whitelist** |
| `STRIKE_AWARE_NONSPORTS_MATCHER_PROMPT.md` | **DONE, committed** (strike_match.py, paper lane) |
| `WORLDCUP_CONSENSUS_PROMPT.md` | **DONE** (Jun 24; `consensus_sports` includes World Cup, Wimbledon) |
| `LIVE_BASEBALL_BUILD_PROMPT.md` Phases 0–1 | **DONE, committed**, ran live (178 samples / 91 candidates) |
| `REMOVE_DIVERGENCE_PROMPT.md` | **NOT done** — `detect_divergences` live; 1,590 divergence opportunities and 177 bet rows in one day |
| `UX_UI_CONSISTENCY_PROMPT.md` | **NOT done** — no shared `arb/metrics.py`; webserver monolith intact |
| `FIX_TERMINAL_FINGERPRINT_PROMPT.md`, `PREGAME_EV_FIX_PROMPT.md`, `PROMPT_FOR_CLAUDE_CODE.md`, `DASHBOARD_PROMPT.md` | DONE (June commits; per `FIXES_REPORT.md`, spot-checked) |

### 1.4 Documented-vs-reality contradictions found (do not silently resolve)

1. **`devig_method`:** `PREREGISTRATION_V2.md` froze `power` as the empirical winner
   (Brier 0.2231 vs 0.2240, n=191); shipped `config.yaml` still says `proportional`. Either
   the freeze applies (flip it as a versioned change) or it doesn't (record why). Phase A's
   re-run decides.
2. **Polymarket fee curve:** the code prices Polymarket **linear** (`FEE_BASIS`
   `polymarket: linear`; SPEC_V2 §8.6 and rule C0 insist on linear). Polymarket's current
   published schedule is **quadratic**: `fee = C × feeRate × p(1−p)`, sports 0.03, crypto
   0.07, finance/politics/tech/mentions 0.04, econ/culture/weather/other 0.05, geopolitics 0
   (docs.polymarket.com/trading/fees, read 2026-07-02; fee structure updated Mar 30 2026).
   At p=0.50 the linear model charges 1.5¢/$1 where the real fee is 0.75¢ — **the model
   double-charges Polymarket sports legs at mid prices**, understating every Polymarket-leg
   edge by up to ~0.75 pts, which is larger than most real arb edges. SPEC_V2's C0
   ("linear fee both sides, never Kalshi quadratic on Poly") is factually stale.
3. **SX fees:** SPEC_V2 §6 mandates `sx.win_fee_rate: 0.05` ("never model SX at zero");
   shipped config has 0.0. SX's published schedule (help.sx.bet "SX Bet Fees",
   learn.sx.bet/trading/fees, read 2026-07-02) says **0% on single bets; 5% only on winning
   parlays**. The published page supports the config, not the spec — but the spec's concern
   (R4: poly+sx arbs suspiciously durable) still needs the parlay-lane fee and a one-time
   live re-verification note. Resolve as: singles 0 (cited), parlays 5% (must be modeled
   when the parlay lane activates).
4. **Kalshi fees:** 7¢ × C × P(1−P) taker confirmed; **a new fee schedule took effect
   2026-06-29** (kalshi.com/docs/kalshi-fee-schedule.pdf) with per-market exceptions and
   maker fees on some markets. Config's flat 0.07 with `confirmed_on: 2026-06-10` predates
   it. One-time re-verification needed (X2 fee-staleness gate will otherwise quarantine
   Kalshi quotes ~Jul 10).
5. **SPEC.md vs SPEC_V2.md** both claim to be "the single source of truth." Resolved by the
   operative-path assumption above; the winner for day-to-day work is SPEC.md + its
   amendments (foundations spec, this plan).
6. **`ev.segments`/scope:** SPEC_V2 scopes EV to MLB only; live config scans consensus for
   MLB + World Cup + Wimbledon. Not a bug (V1 lineage), but the calibration gate (EV#10)
   exists only as machinery — no segment has a passing calibration verdict on the fresh DB.

### 1.5 What's genuinely working

Quote persistence at scale (617k/day, WAL, poll atomicity committed Jun 23), venue adapters
with fixtures (incl. the UFC tag fix and SX inversion tests), the append-only
ledger + annotation discipline (it caught and voided 100% of today's fake P&L — the system
*worked* in that sense), activity gating (zero credits on dormant sports), the AI matcher's
negative-verdict caching, the live-baseball probe methodology, and a credit budget with
real headroom (492). The instrument is intact; what's missing is clean evidence, and the
operational discipline to keep it clean.

---

## 2. Open issues, ranked by evidentiary leverage

Format per issue: assumption at risk → evidence → fix logic → **pre-committed decision
rule**. Ranked by how much a result would change decisions, not by effort.

### #0 — The tree/test/restart gate (blocks everything; zero information until done)

- **Assumption at risk:** "the fixes are live." They are not — they are uncommitted, and
  the last daemon ran from a dirty tree (heartbeat row proves it).
- **Evidence:** §1.1; incident history (two prior stale-process burns).
- **Fix:** verify + commit the working tree in reviewable slices (interleaving fix; settle
  future-start guard; foundations A–F; Phase E), run the full suite, then execute the still
  missing incident items: settle tripwire, `check-invariants`, the baseline-cutover decision,
  and restart **under the Phase D supervisor** so this class of failure is structurally over.
- **Decision rule:** nothing restarts and no downstream item begins until `pytest` is green
  on a clean tree at a recorded HEAD. If any uncommitted foundation module fails its own
  tests, it is reverted (stashed to a branch), not patched live. If a loop double-runs or
  runs stale code after this ships, that is a Phase D bug (per foundations spec), not an
  operational whoopsie.

### #1 — Phase A: is the consensus actually right? (built; run it)

- **Assumption:** de-vigged soft-book median = truth; venue below it = edge. Every EV bet
  inherits this.
- **Evidence it's shaky:** archive clean MLB EV settled = **−$24.07 over 627 rows**; edge
  inversely related to win rate (R5); the venues carry informed money.
- **Fix:** `arb regrade` exists (uncommitted). Run it against the **archive** (881 settled
  EV, 627 clean) — the fresh DB has nothing to grade. Include the de-vig bake-off re-run,
  resolving contradiction §1.4.1.
- **Decision rule (from the foundations spec, kept verbatim in the prompt):** consensus
  beats venue mids on Brier AND EV-subset win rate > 50% → EV lane keeps running, tune
  `devig_method` to the winner. Venue mids beat consensus → **freeze EV bet creation**
  (`ev_enabled: false`), keep recording quotes, repurpose as a venue-leads-books indicator.
  Inconclusive → keep lane, halve display prominence, re-run monthly.

### #2 — Fee models vs. published reality (changes every cost vector; do before new evidence accumulates)

- **Assumption:** fees in config match the venues.
- **Evidence:** §1.4.2–4. Polymarket linear-vs-quadratic alone mis-prices every Polymarket
  leg by up to ~0.75 pts at mid prices — bigger than the median flagged arb edge.
- **Fix:** per-category quadratic Polymarket fees (sports 0.03, crypto 0.07, politics 0.04,
  econ/weather 0.05, geopolitics 0) through the single `leg_fee_cost` implementation;
  re-verify Kalshi's 2026-06-29 PDF (bump `confirmed_on`); keep SX singles at 0 with the
  citation, add the 5% winning-parlay fee to the parlay lane. **Versioned change**: it
  re-freezes cost vectors (SPEC_V2 discipline), so it lands as new config keys + dated
  amendment, never a silent retro-edit; stored quotes keep their as-observed `fee_cost`.
- **Decision rule:** after the change, re-run the archive negrisk sweep and arb detection on
  a recorded day offline: if the corrected fees flip any historical class from null to
  positive (plausible for Polymarket-heavy negrisk baskets), that class gets a fresh
  pre-registered forward test — it does NOT retroactively claim the old data proved edge.

### #3 — Settle tripwire + ledger invariants (the incident's missing insurance)

- **Assumption:** the next tracker bug will be caught before it prints a day of fake P&L.
  Today nothing enforces that at runtime.
- **Evidence:** §1.3 — tripwire and `check-invariants` are the two incident-recovery items
  simply not built; the loop-audit detector is one-shot, not a guard.
- **Fix:** exactly `FIX_SETTLED_REBOOK_LOOP_PROMPT.md` #3 + the five-invariant
  `check-invariants` command, run at the end of every settle cycle.
- **Decision rule:** a tripwire hit logs CRITICAL, annotates, excludes from every P&L cell,
  and blacklists the fingerprint for the event's lifetime. Zero tolerance: any future
  instant-settle row without an annotation is a sev-1.

### #4 — Phase B: settlement matrix filled and enforced (unblocks any trusted path)

- **Assumption:** venues listing "the same event" pay out the same way. The named #1 risk
  since SPEC v1; still handled by an empty `trusted_pairs` and vibes.
- **Evidence:** MMA parity check (SETTLEMENT_RUNBOOK §10) found a real divergence
  (Kalshi "fair price" vs Polymarket clean 50/50 on cancellation) — the mechanism works and
  finds mismatches; the matrix module exists but content/enforcement are unverified.
- **Fix:** fill `data/settlement_matrix.yaml` per sport/scenario with rulebook-section
  citations (tennis retirement, MLB extras/suspension/doubleheader, soccer ET/penalties/
  abandonment, MMA per the runbook); wire the GREEN-section requirement into the trusted
  gate; add the reconcile retro-check that flips cells from lived settlements.
- **Decision rule (foundations, verbatim):** no cross-venue sport gets trusted until its
  section is green; any reconciled mismatch on a trusted pair reverts it to REVIEW
  immediately, pending a cited matrix amendment.

### #5 — Phase C: markouts running (turns paper P&L into evidence)

- **Assumption:** optimistic-vs-confirmed captures the whole paper-to-real gap. It measures
  persistence, not adverse selection.
- **Evidence:** `markouts` table has **0 rows** — the analysis exists, the data hook never
  ran; archive confirmed-vs-optimistic (+$40.64 vs −$19.13) is survivorship, proving the
  current columns can't answer the question.
- **Fix:** persist markouts (+30 s/+2 m/+10 m/+30 m) for every booked leg from the quotes
  the hot/watch loops already sample; add `pnl_markout` (+30 s entry) and the
  flicker/zombie guard.
- **Decision rule (foundations, verbatim):** if markout at +2 min eats > half the average
  flagged edge for a class, that class's headline switches to `pnl_markout` and any go-live
  math prices the measured markout as a per-bet cost. Markout ≈ 0 → the single most
  valuable positive fact this project could produce.

### #6 — Phase F completion: time discipline (half-shipped)

- **Assumption:** timestamps are comparable and cadence claims are true.
- **Evidence:** fresh-DB watch cadence ~140 s mean vs configured 60 — a silent 2.3× gap
  (the foundations spec predicted ~163 s / 2.7× on V1 data), confirmed in one day of new
  data; every incident so far was a clock problem.
- **Fix:** finish the phase — start-time truth hierarchy (MLB Stats schedule outranks venue
  times; graded `verified|nominal|absent`), `consensus_age_secs` stored per EV bet, planned
  vs actual interval in the daily report (cadence.py exists), leg-skew persisted per
  multi-leg record.
- **Decision rule:** after it ships, a bet created from a `nominal` start or an unmeasured
  consensus age is a bug; the p95 cycle time in the daily report is the loop's real spec.

### #7 — One eligibility predicate + position_key on V1 (port SPEC_V2's F2/F3)

- **Assumption at risk:** "clean" means one thing. V1 still has three parallel notions
  (trusted flag, quality_flag, status) — the J1 dual-track foot-gun that produced 314/314
  forbidden settled arbs in V1 and re-emerged today as lane-by-lane guard gaps (the in-play
  guard existed in `split_pregame` but not at the tracker boundary until f131d84).
- **Fix:** `arb/eligibility.py` returning `(eligible, reason, lane ∈ clean|review|invalid)`,
  imported by detection, tracker, settle, reconcile, capacity, exposure, report — plus
  `position_key`/`venue_pair` columns and dedup-by-position in every aggregate. T-ELIG-1..5
  and T-POS-1..2 from SPEC_V2 §3.3/§4 are the frozen tests.
- **Decision rule:** the SPEC_V2 §17 definition of done applies to V1: a passing test that a
  REVIEW arb can never reach clean/headline P&L, with the grep-test proving the logic exists
  in exactly one module.

### #8 — Queued, still-valid cleanups

- **Divergence removal** (`REMOVE_DIVERGENCE_PROMPT.md`): still generating ~1,600
  no-op opportunities and 177 zero-stake rows per day of ledger noise. Do it; quotes remain
  the recompute source.
- **UX/UI consistency** (`UX_UI_CONSISTENCY_PROMPT.md`): three surfaces still compute
  numbers independently; after the eligibility work this becomes cheaper (one metrics
  module reading one predicate).
- **Phase E commit + live verification** (rules-hash verdict expiry — built; needs its
  tests run and a live pass showing a rules edit expiring a cached verdict).
- **`left_on_table` is empty** on the fresh DB — verify the filtered-flag persistence path
  still fires after the incident changes (it may be fine; one day with few flags).

---

## 3. Improvement opportunities beyond fixes

Skepticism rule carried from the project's history: every proposal states how we'd know
it's real. **Measurement first, betting later.** Nothing here starts before Stage 2.

1. **NegRisk exhaustive coverage (cheapest real candidate).** Single-venue, single-oracle —
   no settlement risk, the class the whole audit trail calls "safest." Today's scan is
   page-capped (3 pages × 200) and the archive holds 4 baskets, all Σ≈1.005–1.011 (no
   edge). With corrected quadratic Polymarket fees (issue #2) the fee bar *drops* at mid
   prices, so re-scan the archive offline first. *Real if:* corrected-fee basket edge >
   `negrisk.min_edge` appears at all; then H-NR forward test (30 settled baskets,
   PREREGISTRATION_V2 §4) before any claim.
2. **Maker-side shadow lane (measurement only).** Both Kalshi and Polymarket charge takers
   and rebate makers (Polymarket 25%, Kalshi maker = 25% of taker per current schedule).
   Re-price every flag at the maker side/fee and log the counterfactual edge; no fill model
   pretense — the output is "share of flags that flip +EV as maker," an upper bound. *Real
   if:* the flip fraction is material AND a later fill-probability study (from observed
   queue dynamics in stored ladders) survives; never promoted on the flip fraction alone.
3. **Polymarket WebSocket book feed (R8; keyless).** The one venue where a free, keyless WS
   gives full per-token ladders — feeds Phase C markouts, B4 ladder capture, and capacity
   honesty simultaneously. *Real if:* WS-derived ladders reduce `depth_basis != real` rows
   to a minority; measured, not assumed.
4. **Live-baseball Phase 2 (already gated).** Proceed only per the Phase 0/1 verdicts in
   the lane's own spec; 91 candidate evaluations exist, the rejection histogram is the
   product. *Real if:* the pre-registered freshness/depth gates from measured
   distributions hold across ≥ a week of game days.
5. **Non-sports strike expansion.** The machinery works (8 verdicts, all negative — an
   honest null so far). Widen only the deterministic stage (more series parsed), keep the
   Haiku budget; the paper lane's `settlement_mismatch` counter is the success metric.
   *Real if:* AI-matched paper bets settle consistently across venues at ≥ the H-ARB-SETTLE
   bar (99%/100) before any human-map promotion shortcut.
6. **CLV re-run after #1/#2.** The no-vig CLV gate (N1 fix) has never been re-read against
   corrected fees on the archive. Cheap, read-only, and it is the EV lane's actual go/no-go.
7. **Lead-lag null correction (archive analysis).** Kalshi-leads-Polymarket (42 s vs 209 s
   median) is still confounded by Polymarket's 9× move density; the shuffle-null fix is
   specified in NEXT_PHASES review-3 #9. Pure offline. *Real if:* the asymmetry survives
   the null model — only then consider a targeting experiment.
8. **What NOT to do now:** no new sports, no NFL, no real-money path, no Kelly/weighted
   headline (capacity still `INSUFFICIENT_DEPTH_DATA` by design), no second rebuild while
   Stage 0–1 evidence is missing.

---

## 4. Sequencing — dependency-ordered roadmap

Stage gates are hard: **STOP for review** at the end of each stage; a stage starts only
after the previous stage's acceptance is shown. Prompts live in `prompts/queue/`.

```
STAGE 0 — make the ground true (serial; nothing else until done)
  01 verify + commit the working tree, full pytest, acceptance runs      [01-…md]
  02 settle tripwire + check-invariants                                  [02-…md]
  03 baseline-cutover decision + supervised restart (stop flag removed
     only here)                                                          [03-…md]
  ── STOP: review status output, clean daily report header ──

STAGE 1 — decide what the data already says (parallelizable pair)
  04 Phase A regrade + de-vig bake-off on the archive → EV lane verdict  [04-…md]
  05 fee-model corrections (Polymarket quadratic, Kalshi re-verify,
     SX parlay fee) + offline re-scan of archive negrisk/arb             [05-…md]
  04 and 05 are independent → two worktrees if desired.
  ── STOP: decision rules #1 and #2 executed and recorded in
     PREREGISTRATION_V2.md ──

STAGE 2 — make future evidence trustworthy (parallelizable trio)
  06 Phase B settlement matrix: fill, enforce, retro-check               [06-…md]
  07 Phase C markouts: persist hook + pnl_markout + flicker guard        [07-…md]
  08 Phase F completion: start-time hierarchy, consensus age, cadence
     in report                                                           [08-…md]
  06/07/08 touch disjoint modules → separate worktrees OK; 07 benefits
  from (but does not require) the Polymarket WS feed (improvement 3).
  ── STOP: one week of clean supervised data days, then review ──

STAGE 3 — structural consolidation (serial, big blast radius)
  09 eligibility predicate + position_key everywhere (F2/F3 port)        [09-…md]
  10 divergence removal                                                  [10-…md]
  11 UX/UI consistency (one metrics module, three surfaces)              [11-…md]
  09 before 10/11 (both get cheaper once the predicate exists).
  ── STOP ──

STAGE 4 — improvements (each gated by its own "real if" from §3)
  12 negrisk exhaustive + maker shadow lane (measurement only)           [12-…md]
  then: Polymarket WS, live-baseball Phase 2, CLV re-read, lead-lag
  null — each as its own future prompt after its predecessor's data
  exists.
```

**What blocks what (summary):** everything blocks on Stage 0. Phase A (04) needs nothing
but the archive; fee fixes (05) need a green tree; the negrisk re-scan inside 05 needs 05's
fees. Markout persistence (07) needs the restart (03) to accumulate; its *analysis* can dry-
run on archive quotes immediately. The eligibility port (09) should land after 06–08 so it
consolidates their lanes rather than being re-opened by them. Trusted-pair whitelisting —
the only path to headline cross-venue P&L — needs 06 + 100 reconciled positions/pair
(PREREGISTRATION_V2 §4) and is therefore weeks away by construction; nothing in this plan
shortcuts it.

**Standing rules for every stage** (restated in every queue prompt): paper trading only;
no Odds API credits in tests or loops (abort < 50); no invented endpoints; `Decimal` money;
`flat_bets` append-only; every threshold in `config.yaml`; quotes always persisted;
headline = trusted + sports + pregame; decision rules recorded in `PREREGISTRATION_V2.md`
before their data arrives.
