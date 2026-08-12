# PREREGISTRATION_V2 — ArbModel V2 (frozen before V2 collects data)

> **Status: FROZEN before any V2 data is collected.** Purpose: make null results citable and
> stop post-hoc story-fitting. Every hypothesis, gate, and n-threshold below is committed in
> advance. Changing any of these after data collection starts is a **versioned amendment**
> (new dated section), never a silent edit. Owner: Opus 4.8 (absorbs Fable 5's F7).
> Companion: `SPEC_V2.md` (methods), `ArbModelV2_Plans/00` (V1 evidence this reacts to).

---

## 1. Framing

V1's defensible finding stands as the prior: **riskless cross-venue arb is a mirage** (~88%
vanish on the fast pair; ≈\$0 settled confirmed) and **de-vigged EV barely beats the vig**
(−\$20 honest full-sample; ~10% Brier skill; edge *inverted*). V2 does not assume these are
wrong — it re-measures them honestly with the leaks fixed, and commits in advance to what would
change the verdict.

**Cardinal rule (R3):** the two arb venue-pairs are **different populations** and are
pre-registered separately. They may never share a win rate, a gate, or a headline.

---

## 2. Hypotheses (per class, split by venue pair)

Each hypothesis names its population, the metric, the direction that would *support* it, and the
null. All P&L on the **full-sample optimistic / clean / flat-\$1** basis (SPEC_V2 §10, §12).

### ARB — H-ARB (one hypothesis per venue pair)
- **H-ARB-KP (kalshi+polymarket).** *Claim:* a confirmed, eligibility-clean arb captures
  positive edge net of all frictions. *Prior:* ~88% vanish on ~10 s re-quote → expected null.
  *Metric:* mean confirmed `edge_points` over clean, reconciled arbs. *Null:* confirmed
  `edge_points ≤ 0` (indistinguishable from zero). *Support:* confirmed `edge_points > 0` with
  the CI excluding 0 at n ≥ N_ARB.
- **H-ARB-PS (polymarket+sx).** *Claim:* the ~5% vanish rate reflects real durable edge.
  *Prior:* durability is an artifact of SX being slow/unrepriced + unmodeled frictions (zero
  fee, thin depth, different void rules). *Metric:* confirmed `edge_points` **after** SX honest
  pricing (win-fee + real/thin depth, SPEC_V2 §8.6). *Null:* edge collapses to ≤ 0 once SX is
  priced honestly. *Support:* survives honest pricing with CI excluding 0.
- **H-ARB-SETTLE.** *Claim:* cross-venue settlement agrees often enough for whitelisting.
  *Metric:* fraction of reconciled cross-venue arbs where both legs settled consistently.
  *Gate to whitelist a pair into `trusted_pairs`:* ≥ 99% consistent settlement over ≥ 100
  reconciled positions **and** both rulebooks manually reviewed.

### NEGRISK — H-NR
- *Claim:* intra-Polymarket exhaustive baskets sum < 1 after **linear** fees (C0). *Prior:* V1
  saw all 4 baskets sum 1.005–1.011 (no edge). *Metric:* `1 − Σ eff_cost` with linear fee both
  sides. *Null:* basket edge ≤ `negrisk.min_edge` (no capturable edge). Single-venue → trusted.

### DIVERGENCE — H-DIV
- Measurement only; no bet. *Claim:* residual >10% spreads are mineable structure, not
  mismatch. *Metric:* distribution of spreads after the 15-pt suspect gate. No P&L claim.

### EV — H-EV (measurement only; betting stays OFF)
- *Claim:* calibrated, SE-gated, MLB-scoped EV has positive CLV against the **no-vig closing
  mid**. *Prior:* full-sample −\$20; Polymarket the only positive venue (+\$34.57); edge
  inverted. *Metric:* CLV (§3). *Null:* CLV ≤ 0. **EV betting is disabled until the §3 go/no-go
  bar clears** — no dollar is bet on EV in V2 by default.

---

## 3. Gates (the go/no-go bars, frozen)

### 3.1 CLV — measured against the no-vig closing mid (EV#9)
- CLV per bet = `p̂_closing_novig(outcome) − c_entry`, where `p̂_closing_novig` is the de-vigged
  (SPEC_V2 §8.1 method) consensus **mid** at close, **fees excluded from the CLV metric** (fees
  live in P&L only — including them biases the proof-of-edge gate toward zero).
- **EV go bar:** mean CLV > 0 with a one-sided 95% CI excluding 0, over **n ≥ 190** settled
  MLB EV bets (matches the frozen `validation.ev_filter.target_n`), **and** the calibration gate
  (§3.2) passes for the segment. Both required; ROI alone never promotes EV.
- **Kill rule:** 10 consecutive losing forward-test bets (`kill_consecutive_losses: 10`) halts
  the EV forward test pending review.

### 3.2 Calibration — per segment (EV#10)
- Fit isotonic (Platt if `n < 200`) on settled **raw** quotes (SPEC_V2 §8.4).
- **Gate:** segment ECE ≤ `scoring.ece_max` (0.10) **and** positive Brier skill vs base rate.
  A segment failing ECE is **blocked even if ROI passes**. Computed per segment
  (`ev.segments`, i.e. `baseball_mlb`), never pooled across sports.

### 3.3 Arb whitelisting (trusted_pairs promotion)
- A venue-pair+market-type enters `trusted_pairs` only after H-ARB-SETTLE's gate (§2) clears
  **and** a human has read both venues' rulebooks for that market type. Until then cross-venue
  is REVIEW-only (SPEC_V2 rule 9), auto-fill off.

### 3.4 Capacity / weighted-ARB enablement (F6)
- `capacity.arb_size_by_ladder` flips to true only after: (1) ≥ `min_ladder_fraction` of
  feasible arbs carry real multi-level ladders, **and** (2) the capacity report shows genuine
  *edge*-limited (not depth-fantasy) arbs. Until both, capacity reports `UNKNOWN` and no
  weighted headline is printed.

---

## 4. n-thresholds (minimum samples before any claim)

| Claim | Min n | Source |
|---|---|---|
| ARB per venue-pair confirmed-edge CI | N_ARB = 100 reconciled positions/pair | R3 populations separate |
| ARB settlement-consistency (whitelist) | 100 reconciled positions/pair @ ≥99% | §3.3 |
| EV CLV go bar | 190 settled MLB EV bets | `validation.ev_filter.target_n` |
| Calibration ECE | 200 settled raw quotes/segment | `calibration.min_samples` |
| NEGRISK edge | 30 settled baskets | `scoring.score_min_n` analogue |
| Segment score / allocation | 30 (`score_min_n`); block < 30 | `scoring` |

All counts are of **`position_key`-deduped, `lane=="clean"`, reconciled** rows (SPEC_V2 §4, §11).
Halfway checkpoints (`halfway_n`) are for monitoring only and never promote a strategy.

---

## 5. What would change the verdict (falsifiers, committed in advance)

- **ARB mirage overturned** iff a venue-pair shows confirmed `edge_points > 0` (CI excludes 0)
  at N_ARB **and** ≥99% settlement consistency — for that pair only.
- **EV thesis promoted** iff §3.1 CLV bar **and** §3.2 calibration gate both pass for a segment;
  then and only then does EV betting turn on for that segment, at flat-\$1 first.
- **Weighted-ARB reported** iff §3.4 both conditions hold.
- Anything short of these is reported as an **honest null**, split by venue pair, with the
  optimistic/confirmed and raw/clean labels intact. Null is a valid, publishable result — the
  harness's job is to disprove edges cheaply and honestly.

---

## 6. Amendment log

| Date | Change | Reason |
|---|---|---|
| 2026-07-01 | Initial freeze. `devig_method: power` chosen empirically on V1 MLB settled data (Brier 0.2231 / logloss 0.6323, n=191; margin within noise → re-run on V2 sample per SPEC_V2 §15 Phase 2). | Pre-registration before V2 data collection. |
| 2026-07-02 | **Phase A regrade verdict → EV bet creation FROZEN** (`ev_enabled: false`); `devig_method` restored `proportional`→`power` (shipped config had drifted off the freeze). | Prompt 04 ran `arb regrade --db data/archive-pre-v2/arb.sqlite` (read-only). See §7. |
| 2026-07-02 | **Fee cost vectors re-frozen.** Polymarket linear→**quadratic** per category (`fee = feeRate·p·(1−p)`); SX singles 0 / winning-parlay 5%; Kalshi 0.07 re-verified. | Prompt 05. Published schedules (Polymarket 2026-03-30, SX, Kalshi Jun-2026). See §8. |
| 2026-07-03 | **Baseline cutover (Option A) + supervised restart.** Live `data/arb.sqlite` archived to `data/arb-incident-baseline-20260703.sqlite` (507 flat_bets / 616,889 quotes, read-only); fresh empty `data/arb.sqlite` initialized; system restarted under the Phase D supervisor and `data/stop-all` removed. | Prompt 03, after 01–08 landed on `main` (515 tests green). **Rule: pre-cutover data (this baseline + `data/archive-pre-v2/`) is analysis-only and is NEVER blended into post-cutover P&L.** The measurement clock restarts here. |
| 2026-07-06 | **Stage α decision rules frozen** (prompt 13 — improvement-scan addendum). Three pre-committed read-only-measurement rules recorded in §9 BEFORE their data was collected: α1 totals cross-venue scope (2% overlap cutoff), α2 AI-matcher stage-1 recall (<50% / ≥80% bands), α3 dead-schema DELETE-or-REVIVE. | Prompt 13, Stage α. Rules transcribed verbatim from the prompt's pre-committed clauses; outcomes recorded in §9 after the queries ran. |

---

## 7. Phase A regrade — EV-lane verdict (2026-07-02, prompt 04)

Read-only run over `data/archive-pre-v2/arb.sqlite` (Jun 10–Jul 2), scores via the MLB Stats API
(free/keyless historical oracle — not an Odds API credit call), annotated rows excluded.

**Forecaster Brier (lower = sharper), MLB WINNER settled:**

| Source | n | Brier | log-loss | ECE |
|---|---|---|---|---|
| polymarket_mid | 38 | 0.2209 | 0.6249 | 0.0679 |
| kalshi_mid | 40 | 0.2230 | 0.6293 | 0.1095 |
| **consensus (p̂)** | 68 | **0.2379** | 0.6684 | **0.2997** |

**EV-subset test:** venue side won **420/877 (0.479, 95% CI 0.446–0.512)** — leans anti-edge.

**De-vig bake-off** (MLB settled consensus, n=30): power Brier **0.22041** < shin 0.22099 <
proportional 0.22217 → **power wins** (confirms the 2026-07-01 freeze).

**Fired rule (foundations Phase A, bullet 2, verbatim):** *"Venue mids beat consensus → the EV
lane's sign is wrong — freeze EV bet creation, keep recording EV quotes for data, repurpose the
signal as a venue-leads-books indicator."* Both venue mids beat consensus on Brier and consensus
calibration is far worse (ECE 0.30). **Action taken:** `ev_enabled: false` (freezes bet creation
for kind EV = pregame + micro; consensus still computed/recorded for study; `LIVE_BASEBALL_EV`
unaffected); `devig_method: power`; daily report gains a rolling consensus-vs-venue-mid Brier
premise-monitor line. **Trigger to re-open:** a re-run where consensus beats venue mids on Brier
**and** EV-subset win rate > 0.5 (CI excludes 0.5). Pre-cutover archive data is analysis-only and
is never blended into post-cutover P&L.

---

## 8. Fee-model corrections (2026-07-02, prompt 05)

**Versioned change — stored quotes keep their as-observed `fee_cost`; only newly fetched quotes
use the new model.** All fees route through the single `FEE_BASIS`/`leg_fee_cost` implementation
(K1/K2) — including negrisk, which previously carried a second inline linear fee.

- **Polymarket: linear → quadratic, per category.** Published schedule
  (docs.polymarket.com/trading/fees, read 2026-07-02, effective 2026-03-30):
  `fee = C·feeRate·p·(1−p)`, feeRate = sports 0.03, crypto 0.07, finance/politics/tech/mentions
  0.04, econ/culture/weather/other 0.05, geopolitics 0; makers 0. The old linear model
  double-charged mid-price legs (1.5¢ vs the true 0.75¢ at p=0.5). *Note:* the nonsports lane
  currently prices Polymarket legs at the econ/weather default (0.05) pending per-entry category
  tagging; the per-category keys exist for when that lands.
- **SX:** singles 0% (taker+maker) confirmed (help.sx.bet/…/sx-bet-fees, learn.sx.bet/trading/fees,
  read 2026-07-02); the 5% winning-**parlay** fee is modeled in the parlay lane
  (`parlay.sx_win_fee_rate: 0.05`, `parlay_sx_win_fee`), dormant until that lane activates.
- **Kalshi:** general taker `ceil(0.07·C·P·(1−P))` re-verified against the June-2026 schedule
  (effective 2026-06-29) via the help center; index-market 0.035 exception is out of our scanned
  scope; `confirmed_on` bumped 2026-07-02. (Authoritative PDF was rate-limited this pass — general
  rate corroborated via help center, not read line-by-line.)

**Decision rule outcome (offline archive re-read):** the 4 archived NEGRISK baskets stored only
`basket_sum` (linear), so an exact per-leg re-price wasn't possible; the analytic bound (quadratic
≈ half the mid-price fee) shifts each near-unity basket (Σ 1.005–1.011) down by at most
~0.005–0.010, so **none crosses below 1 by more than `negrisk.min_edge` (0.005) → honest null:
fees were not the binding constraint on the archived baskets.** No class flipped null→positive, so
no fresh forward test is triggered here (the general negrisk exhaustive sweep remains prompt 12).
Forward negrisk scans now price on the corrected quadratic curve and will flag more baskets than
the linear model did.

---

## 9. Stage α decision rules (2026-07-06, prompt 13) — FROZEN before Stage α data

Three read-only measurements from the improvement-scan addendum. The decision rules below are
transcribed from the prompt's pre-committed clauses and frozen here **before** the queries were
run; outcomes (the "Result" lines) were filled in afterward from `data/arb.sqlite` (post-cutover
live) and `data/archive-pre-v2/arb.sqlite`.

### α1 — Totals cross-venue scope
- **Metric.** Pairwise exact-line overlap = the fraction of *multi-venue* TOTAL events (an event
  with a `market_type='TOTAL'` line quoted by ≥2 distinct venues) for which at least one venue
  **pair** shares an identical `line` value. Measured over BOTH DBs. Kalshi multi-strike totals
  ladders are checked separately for intersection with Polymarket/SX main lines.
- **Rule (pre-committed).** Pairwise exact-line overlap **< 2%** of multi-venue totals events
  across BOTH DBs → totals leave cross-venue detection scope (config flag + dated SPEC amendment;
  single-venue totals persistence unchanged). If overlap is instead concentrated in Kalshi strike
  ladders intersecting Polymarket/SX main lines → do NOT drop totals; write a ladder-aware matching
  item as a new queue prompt.
- **Result (2026-07-06) — PREMISE REFUTED, second branch fires.** Pairwise exact-line overlap:
  LIVE MLB **21/21 = 100%**; ARCHIVE MLB **270/275 = 98.2%**, NBA 1/1. MLB is the ONLY
  multi-venue totals sport — MMA and soccer totals are Polymarket-single-venue (14 / 137 events)
  and so are correctly outside cross-venue scope by construction, not by a drop decision. The
  overlap is **entirely Kalshi-strike-ladder-driven**: Kalshi quotes an exhaustive half-point
  ladder (2.5→12.5, 11–25 distinct lines) that contains Polymarket's (8.5/9.5) and SX's (8.5)
  main lines — `kalshi+polymarket` share a line in 21/21 live events. Far above the 2% cutoff on
  BOTH DBs, so **totals do NOT leave cross-venue scope**; the pre-committed second branch fires →
  the deliverable is a **ladder-aware matching prompt** (detection must consider every Kalshi
  strike, not just a main line). Totals are ~50% of live quote inventory (27,721 of 55,618).
  Detection already keys outcomes by `(market_type, line, outcome)` with every Kalshi strike
  persisted, so the follow-on prompt's first task is to VERIFY strike-level pairing actually
  fires (0 ARB today is the fees/edge null, not necessarily a matching gap).

### α2 — AI-matcher stage-1 recall
- **Metric.** Recall = of the human-mapped / manually-identified equivalent non-sports pairs
  available as ground truth (`data/nonsports_map.yaml` enabled+filled entries; archive
  `logs/nonsports_candidates.log` manually-confirmed equivalents), the fraction that today's
  stage-1 candidate generation (fuzzy ≥ `nonsports.candidate_threshold` = 85 + the deterministic
  strike matcher) would emit as a candidate. Offline, **zero API calls**.
- **Rule (pre-committed).** Recall **< 50%** on ground-truth pairs → stage-1 thresholds get their
  own tuning prompt before ANY Haiku-budget expansion (MASTER_PLAN §3.5). Recall **≥ 80%** →
  expansion proceeds as MASTER_PLAN §3.5. (50–80%: inconclusive — default to tuning-first,
  re-measure as ground truth accrues.)
- **Zero-ground-truth contingency (pre-committed).** If no filled ground-truth pairs exist (map
  all-template, archive log absent/empty), recall is *undefined* — the pre-committed action is the
  spirit of the <50% branch: **never expand Haiku spend on an unmeasured recall**; assembling
  ground truth is a prerequisite deliverable before any stage-1 expansion.
- **Result (2026-07-06) — recall UNMEASURABLE; zero-GT contingency fires.** There are **zero
  ground-truth pairs anywhere**: `data/nonsports_map.yaml` = 3 disabled all-empty templates (no
  filled tickers/market_ids); `logs/nonsports_candidates.log` = **0 bytes**; `nonsports_verdicts`
  = **0 positive of 8 ever recorded** (pre-cutover baseline), 0 rows live, and the V1 archive has
  no verdict table at all. No known-equivalent non-sports pair has ever been confirmed. Recall is
  therefore undefined → the pre-committed contingency (= spirit of the <50% branch): **do NOT
  expand Haiku budget**; the prerequisite deliverable is a **stage-1 recall harness + a
  hand-labeled ground-truth set** (equivalent pairs mined from stored Kalshi/Polymarket listings)
  before any expansion. Separately: post-cutover the whole nonsports/AI-matcher lane **has not run
  at all** (0 verdicts; `nonsports` is not in `supervisor.processes`), so *precision* isn't being
  measured either — ties to α3's unlaunched-lanes finding.

### α3 — Dead-schema surfaces
- **Rule (pre-committed).** Every zero-row table (`wallets`, `wallet_trades`, `forecasts`,
  `btc_oracle_prints`; and `positions`/`ledger` if zero) either has, **by end of Stage 2**, a dated
  revive prompt carrying a pre-registered "real if" clause, or its schema + feeding code are
  **deleted in Stage 3** alongside queue 10. No third (indefinite-dormant) state. The BTC
  same-oracle lane (`btc_arb`) is evaluated for revive AFTER queue-05 corrected fees (done, §8), on
  its single-oracle no-settlement-mismatch merit. `ev-exit`: if it writes nothing durable,
  instrument its exit marks into an analysis table (CLV-at-exit) with a one-page spec, or stop the
  runner (γ2).
- **Result (2026-07-06) — every zero-row table has a WORKING writer; they are UNLAUNCHED lanes,
  not dead code.** `supervisor.processes = {watch: true}` only, so post-cutover just watch (140
  polls) + scan (2) ran; ev-exit/focus/negrisk/political/whales/btc/forecast were never launched
  (the prompt's "1,961 ev-exit polls" was the pre-cutover daemon). Per-surface memo:
  - **`positions` + `ledger`** ← `arb/paper.py:fill_opportunity`, the V1 auto-fill executor,
    reachable only when `auto_fill=true` (permanently false; V2 books to `flat_bets`). Empirically
    dead: 11 `flat_bets` rows, 0 `positions`/`ledger`. → **DELETE candidate** (schema +
    `arb/paper.py` + the `positions` fill path) in Stage 3 alongside queue 10, after confirming the
    scan path never calls `fill_opportunity`.
  - **`btc_oracle_prints`** ← `arb/experimental/btc_oracle.py` (`btc` cmd). → **REVIVE.** The
    prompt's designated candidate: the ONLY cross-venue class with the single-oracle
    (BRTI vs UMA) no-settlement-mismatch property, and queue-05 fees are now corrected.
    `data/stop-btc` (set 2026-06-22) blocks it. Revive plan (own dated prompt by end of Stage 2):
    clear stop-btc, add `btc` to `supervisor.processes`, collect prints; **real if** = the two
    oracles print the SAME binary result over ≥ N windows (N set in the revive prompt) before
    `btc_arb.trusted` is even discussed; whitelisting still gated by §3.3.
  - **`wallets` + `wallet_trades`** ← `arb/experimental/whales.py` (`whales` cmd), Phase 5
    "measurement/debunk only", manual, single-venue read-only. → **REVIVE-or-DELETE, lean DELETE
    (YAGNI):** disconnected from the arb thesis, never run, no committed study. Revive only with a
    "real if" (qualified wallets' forward copy-fill P&L beats base rate); else delete in Stage 3.
  - **`forecasts`** ← `arb/forecast/ledger.py` (`forecast-log/settle/report`), a MANUAL
    calibration ledger (not a loop, zero idle cost), 0 rows = never used. → **DELETE candidate
    (YAGNI)** unless the operator commits to logging forecasts.
  - **ev-exit (γ2 preview):** the "writes nothing durable" premise is **FALSE** — it appends
    EXITED/SOLD/STOPPED rows to `flat_bets` (`ev_exit.py:170-199`). It found 0 candidates because
    it only acts on `kind='EV' AND status='OPEN'` bets and EV creation is frozen; post-cutover it
    isn't even launched. → **KEEP code, leave dormant** (do not supervise while EV is frozen — no
    work exists); revisit if/when EV bet creation re-enables. No instrumentation needed.
  - No third (indefinite-dormant) state: revive prompts land by end of Stage 2; deletions execute
    in Stage 3.
