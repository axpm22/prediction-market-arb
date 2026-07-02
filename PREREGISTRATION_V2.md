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
