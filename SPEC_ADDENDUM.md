---
title: "ArbitrageModel — Production Readiness Addendum (v3.5)"
subtitle: "Companion to REBUILD_SPEC_V3.4.md — capital-preservation layer; where the two conflict, this addendum wins"
date: "July 7, 2026"
---

# Scope and provenance

This addendum revises the spec lineage (v3.1 → v3.4) into a production-ready document per the capital-preservation mandate. Three facts frame everything below:

1. **The system is paper-only** (v3.4 rule 1). "Auto-book" in this document means *automatic admission to the clean/headline lane*; the identical gates govern any future live deployment, so nothing here is hypothetical — it is the deployment contract, rehearsed on paper.
2. **Every finding in SPEC_V3.1_REVIEW_FINAL_SWEEP.md (M/SC/MA/OP/ME series) was already applied in v3.2**, and two further adversarial passes (v3.3: 17 findings; v3.4: 34 findings) went beyond it. Section 9 maps each checklist item to its fix location. This addendum does not re-fix fixed things; it formalizes what the checklist demanded that no prior version stated as a single implementable contract.
3. **Five new issues were discovered while writing this addendum** (Independent Discoveries, before Section 9) — none appears in any prior review pass.

Terminology directive (binding, RULE 1): **"arbitrage" is same-venue only.** The kind taxonomy changes: `ARB` is reserved for same-venue, mechanically locked constructs — NegRisk basket exhaustion (proof-passing per v3.4 §8.4), intra-market `YES+NO < 1` (Polymarket only, v3.3 §12), and same-venue ladder crosses. Every cross-venue position becomes kind **`XSPREAD`** ("cross-platform spread"): a position with settlement risk that must be explicitly priced, never called arbitrage in code, schema, reports, or prose. v3.4's "TRUE ARB" class is `XSPREAD` whenever legs span venues. This is a rename plus a pricing obligation, not a behavior change — cross-venue was already REVIEW-only forever.

# Section 1: Resolution Fungibility & Settlement Risk Matrix

| Venue pair | Resolution source | Dispute window | Void/push rules | Timing | Equivalence verdict |
|---|---|---|---|---|---|
| Kalshi ↔ Polymarket | Kalshi: exchange determination per market rulebook. Polymarket: UMA optimistic oracle | Kalshi: none (exchange final). Polymarket: ~2h challenge after proposal; DVM escalation 48–96h+ on dispute | Kalshi: per-rulebook, voids rare. Polymarket: can settle 50/50 (`PARTIAL`, v3.4 §11.2); postponement conventions differ per sport | Kalshi resolves at data-final; Polymarket at proposal + window | **NOT EQUIVALENT.** Certificate required per (sport × market_type) |
| Kalshi ↔ SX | Exchange rulebook vs SX league-rules oracle | SX: outcome reporting per league; settled endpoint unverified (v3.4 §6.3 — if absent, SX legs never leave REVIEW) | SX has draw-no-bet-style voids (`NO_CONTEST`, `NO_GAME_OR_EVEN`) absent on Kalshi | SX settlement lag unmeasured | **NOT EQUIVALENT** |
| Polymarket ↔ SX | UMA vs SX oracle | Both windows apply, sequentially unhedged | Both venues' void lists differ | Compounded lag | **NOT EQUIVALENT** |

**Auto-book eligibility: no pair passes. None is expected to.** The path to eligibility is the Resolution Equivalence Certificate:

**Resolution Equivalence Certificate** — issued per `(venue_pair, sport, market_type)`, all four conditions:
1. Both rulebooks cite the same primary data source for this market type (human-read, section citations recorded in `settlement_matrix.yaml`).
2. Identical handling across the full v3.4 §11.4 scenario list (extra innings, postponement, retirement, abandonment, no-contest) — every cell GREEN.
3. `n` reconciled, position-deduped, mismatch-free positions observed (v3.4 §18.2 gate).
4. The certificate carries `p_mismatch_upper = 3/n` (rule of three: the one-sided 95% upper bound on an event probability after n trials with zero occurrences) and **expires on any rulebook edit** (`rules_sha` change re-zeroes n).

**Mismatch pricing (binding — this is the arithmetic that kills the thesis):** every XSPREAD's net edge subtracts the certificate's priced settlement risk:

```
net_spread_edge = gross_edge − Σ fees − p_mismatch_upper × LGM,   LGM = 1.0
```

LGM (loss-given-mismatch) = 1.0 stake, conservative: one venue voids or flips while the other leg loses — the "locked" payout on one side disappears entirely. Consequences at the v3.4 gate (n = 100, zero mismatches): `p_upper = 3/100 = 3%` → priced settlement cost = **3 points**, versus observed cross-venue spreads with median < 1 point and a 4-point stale-warn ceiling. **A 100-position certificate cannot produce a positive net spread — ever.** Positive net requires `p_upper ≤ 0.5%` → **n ≥ 600 mismatch-free positions**, *and* gross > fees + 0.5%. The v3.4 whitelist gate is therefore necessary but not sufficient; this pricing formula is the binding constraint and is evaluated by `eligibility.py`, not by a human reading a report.

**Settlement-lag risk:** between the first leg's resolution and the last leg's finality (UMA window included), the surviving leg is an unhedged directional position. `reconcile.py` measures the realized lag distribution per pair; `exposure.py` charges the full stake as capital-at-risk for the lag window and reports p50/p95 lag per pair in the daily report. A pair whose p95 lag exceeds 72h is annotated `settlement-lag-extreme` in its certificate.

# Section 2: Fee Matrix & Net-of-Fee Math

**Fee Matrix** (dated facts; trading fees verified against published schedules 2026-07-07; every cell carries `source_url` + `confirmed_on` in config; stale > 30 days = report badge):

| Venue | Taker (per contract, $1 payout) | Maker | At p=0.50 | Withdrawal | On-chain gas | Effective |
|---|---|---|---|---|---|---|
| Kalshi | `ceil_to_cent(0.07 · C · P · (1−P))`; index markets 0.035 | `0.0175 · C · P · (1−P)` (25% of taker — reduced, not rebated) | 1.75¢ taker / 0.44¢ maker | [VERIFY: ACH historically $0; wire fee exists] | n/a | 2026-06-29 |
| Polymarket | `C · rate · p · (1−p)`: sports 0.03, crypto 0.07, politics/finance/tech 0.04, econ/culture/weather 0.05, geopolitics 0 | 0 (+ taker-funded rebate pool) | 0.75¢ sports | gas only [VERIFY] | Polygon ≈ $0.005–0.02/tx [VERIFY] | 2026-03-30 |
| SX | 0% singles; 5% on **winning parlays** only | 0% | 0 | gas [VERIFY] | Polygon-class | help.sx.bet |

No formula in the system is valid unless its fees route through the single `leg_fee_cost()` implementation reading this matrix (v3.4 §6.5). Unverified cells are `[VERIFY]` dated-fact placeholders — inventing a number here is exactly the fee-fiction failure this section exists to kill; they must be confirmed in Phase 3 before any threshold depending on them is trusted.

**Net edge formula** (units: probability points per $1 payout, per v3.4 §7.1):

```
net_edge = gross_edge − Σ_legs leg_fee_cost_i − gas_estimate − amortized_withdrawal
           − [XSPREAD only] p_mismatch_upper × LGM
```

**Threshold derivation.** Auto-book floor: `auto_book_min_net_edge = max(2 × total_fees, 0.005)`. XSPREAD REVIEW ceiling: anything above `total_fees + 0.005` routes to mandatory REVIEW. Worked example, Kalshi+Polymarket sports at p ≈ 0.50: `total_fees = 0.0175 + 0.0075 = 0.025` → floor = `max(0.05, 0.005)` = **5.0 points**; ceiling = **3.0 points**. Floor > ceiling → the auto-book window is **empty**. Generally: the window is empty whenever `2F > F + 0.005 ⟺ F > 0.5%`, which holds for every cross-venue pair at all prices p ∈ [~0.15, 0.85]. At tail prices (p ≈ 0.05, F ≈ 0.47%) a sliver [0.5%, ~0.97%] technically opens — and is then swamped by the Section 1 mismatch pricing (≥ 3 points at any achievable certificate) and policed by the tail-quote guards (zombie, empty-book, stale-warn). Codified rather than left to arithmetic luck: `deployment.xspread_autobook: false`, structurally, with this derivation as the comment. Auto-book can only ever exist for same-venue `ARB` where fees can drop below 0.5% (geopolitics 0-fee NegRisk is the only construct that clears cleanly today).

**Void/push P&L convention per venue** (v3.4 §11.1 conventions, restated as the table the checklist demanded):

| Venue | Void trigger | Stake | Fees | P&L booked | Timing |
|---|---|---|---|---|---|
| Kalshi | Rulebook void (rare) | Returned | [VERIFY: refund policy is a dated fact, v3.4 §11.1] | $0, excluded from win-rate denominators | At exchange determination |
| Polymarket | Market voided; or UMA 50/50 | Returned / fractional | Taker fee not refunded [VERIFY] | VOID: $0. PARTIAL(f): `stake × (f/cost − 1)`; excluded from win rates | Only after UMA finality (resolver returns None inside the window) |
| SX | `NO_CONTEST`, `NO_GAME_OR_EVEN` | Returned | 0 (no single fees) | $0, excluded from win rates | At SX settlement [endpoint unverified] |

# Section 3: Position Sizing — Fractional Fee-Adjusted Kelly

**Derivation from first principles.** Maximize expected log wealth for a binary bet at fraction `f`, net odds `b` (profit per $1 staked on a win), win probability `p`, `q = 1−p`:

```
g(f) = p·ln(1 + f·b) + q·ln(1 − f)
g'(f) = p·b/(1 + f·b) − q/(1 − f) = 0
  ⟹  p·b·(1 − f) = q·(1 + f·b)
  ⟹  f·b·(p + q) = p·b − q
  ⟹  f* = (p·b − q)/b
```

For a binary contract at fee-inclusive effective cost `c` (dollars per $1 payout): `b_net = (1−c)/c`, so

```
f* = (p·(1−c)/c − (1−p)) / ((1−c)/c) = (p − c)/(1 − c)
```

— the prompt's `f* = (p_net·b_net − (1−p_net))/b_net` and the spec's `(p−c)/(1−c)` are **the same formula**; the spec keeps the second form with `c` fee-inclusive (fees enter Kelly through `c`, exactly once).

**`p_net` admissibility constraint (RULE 3, already enforced):** `p_net` must come from a forecaster demonstrated to beat the venue's own mid at the same timestamp, out of sample, with clustered CIs (v3.4 §18.3). No such forecaster currently exists — the archive verdict runs the other way — so **the EV lane is frozen and Kelly has no admissible input today.** Kelly remains a projection column until the re-open trigger fires. A sizing model wired to an unvalidated `p_net` is not conservative at any fraction.

**Fractional Kelly = 1/4, justification:** `g(κ·f*) ≈ g_max·(2κ − κ²)` for small edges — quarter-Kelly keeps ≈ 43.75% of maximal growth while cutting drawdown roughly 4× and, with the backtest's own probability SEs (σ_p ≈ 0.03–0.05), makes the overbet-past-f* probability negligible (growth turns negative beyond 2f*; estimation error makes full Kelly an overbet roughly half the time by construction). Hard cap: 5% of bankroll per bet (v3.4).

**Correlation adjustment (implementable today, no estimated matrix required):** per-event aggregate ≤ 5% of bankroll; per-sport aggregate ≤ 10%; same-day same-league totals aggregate ≤ 15% (weather/park common drivers, v3.4 §18.5 known residual). Full portfolio Kelly (`f ∝ Σ⁻¹μ`) is deferred until a *measured* correlation matrix exists from the ledger — an assumed matrix is worse than caps.

**Capital lockup pricing:**

```
annualized_edge = (1 + net_edge)^(365 / lockup_days) / (1 + risk_free_rate) − 1
                  # geometric excess; arithmetic subtraction overstates it
```

Deployment gate: `annualized_edge > deployment.annualized_hurdle` (0.20). `risk_free_rate` is a **dated fact** like fees (13-week T-bill, `source_url` + `confirmed_on`) — see Independent Discovery #5. `lockup_days` runs to the **later** venue's settlement including dispute windows (v3.4 §13.4). Reconciliation with v3.4's 5%/yr carry tag: the 5% tag is the measurement-layer "uneconomic" label; the 20% hurdle is the capital-deployment gate. Both exist, layered, distinct purposes. NO-basket lockup may be ≈ 0 if NegRiskAdapter conversion is verified — Independent Discovery #3.

# Section 4: Execution Risk & Leg Failure

**State machine (two-legged; N-legged baskets generalize leg B to "all remaining legs"):**

```
DETECT ──► CERTIFY ──► SIZE ──► FILL_A ──► FILL_B ──► CONFIRM_LOCK ──► LOCKED
              │           │        │          │
              ▼           ▼        ▼          ▼
           REVIEW      no-trade  NO_POSITION LEG_RISK ──► (hold as EV | unwind)
```

- **CERTIFY:** equivalence certificate valid and unexpired (XSPREAD) or exhaustiveness proof passing (ARB baskets); quotes ≤ `max_actionable_skew_secs` (10) apart; depth ≥ floor; edge in points, net of Section 2.
- **SIZE:** `min(quarter-Kelly, VWAP cap §5)`.
- **FILL_A:** IOC/FOK limit order at `quote + taker_price_buffer`. Reject or partial → cancel remainder, **NO_POSITION**, write `left_on_table` reason `fill-fail`. Never chase.
- **FILL_B:** IOC within `fok_window_secs` (5). The book moving through the limit is a fail, not a worse fill — IOC guarantees that.
- **CONFIRM_LOCK:** both fills tape-checked (v3.4 §10.5 tape rules, per-leg).

**LEG_RISK (the classic failure — leg A filled, leg B didn't):** leg A is now an unhedged directional bet and is *re-sized as one*, never held at arb size:
1. Compute standalone `EV_A = p_ref − c_A` (points), `p_ref` = best same-timestamp reference (venue mid consensus per §18.3 status).
2. If `EV_A ≥ ev.e_min` → hold at standalone quarter-Kelly size; **unwind the excess at market immediately**, booking exit slippage as a realized cost.
3. Else → unwind fully, immediately, at market; the round-trip loss (spread + fees + adverse move) is the recorded cost of the failure.
4. Every LEG_RISK event appends a `bet_events` row `leg_failure` with the realized cost; per-pair leg-failure rate is a daily-report line, and a pair exceeding `deployment.leg_failure_suspend_rate` (0.20) is suspended from CERTIFY.

**Paper rehearsal (mandatory before capital):** the machine runs in simulation — the verify lane's 10s re-quote is the fill oracle. A flag whose re-quote moved through the limit counts as a simulated leg failure. This produces the leg-failure rate, the unwind-cost distribution, and the suspension list **before any dollar exists**; the ~88% vanish rate already measured says the simulated rate will be brutal, which is the point.

**Failure-mode table:** stale quote → IOC no-fill (cost 0); partial fill → treat unfilled remainder as leg failure pro-rata; venue 5xx/timeout mid-sequence → abort before FILL_A or enter LEG_RISK after; halted/suspended market → zombie/flicker guards bar CERTIFY; resolution dispute after LOCKED → Section 1 lag exposure, resolver None until finality.

# Section 5: Liquidity, Slippage & Order-Book Depth

**Volume-at-quote requirements:** venue-printed 24h volume > 0 AND `depth_usd ≥ min_executable_usd` (50; micro lane per its own floors) AND not zombie (zero volume + ≥ 0.90 spread → excluded from detection and markouts, v3.2/v3.4).

**Slippage model — VWAP, never top-of-book:** walking the stored ladder (full-book capture is already mandatory, v3.3 §6.2),

```
vwap_cost(q) = Σ_k c_k·q_k / q   over the cheapest levels covering size q
slippage(q)  = vwap_cost(q) − vwap_cost(top)
```

All projection P&L fills at `vwap_cost(q)`, not the top quote. Where no ladder exists, capacity already reports `INSUFFICIENT_DEPTH_DATA` (v3.4 §13.4) — a top-of-book capacity number is fiction by prior demonstration ($72,209).

**Position cap:**

```
max_position = min( quarter_kelly_size,  visible_depth / 5,  q* )
q* = max q such that slippage(q) ≤ slippage.max_edge_fraction (0.20) × net_edge
```

Derivation of the 20% term: at `slippage(q) = 0.2 × net_edge`, realized edge ≥ 0.8 × net — the cap keeps execution from consuming more than a fifth of what detection found. `depth/5` is the coarse bound when the ladder is thin; `q*` is the binding one when it isn't.

# Section 6: Backtest Hygiene & Edge Validation

- **Out-of-sample holdout:** 20% of *events* (split by `event_key` cluster, never by row — rows within an event leak), walk-forward monthly: tune on months 1..k, test on month k+1, roll. Reported numbers are test-only.
- **Same-timestamp protocol:** per v3.4 §18.3 — every forecaster comparison runs both as-traded (the trading question) and same-timestamp (the science question), paired bootstrap, clustered by event. This resolves M1 (the staleness confound) as the sweep demanded.
- **Sample size & SE:** every claimed edge reports n, n_eff (cluster-adjusted), SE, and a cluster-bootstrap CI. The v3.4 §18.5 MDE disclosure applies: at n_eff ≈ 90 the win-rate gate detects only ≳ 0.65 — the [NEEDS INPUT] decision (accept that bar vs collect ~600 effective events) must be resolved before the first live poll.
- **Look-ahead controls:** features and decisions may read only rows with `fetched_at ≤ decision_time` (as-of joins); no resolved-market fields anywhere upstream of a decision; `regrade` is read-only; the re-open trigger runs on post-cutover data only (v3.4).
- **M1 deviation, stated plainly:** the checklist says "only freeze the lane if p < 0.01." Rejected as backwards under capital preservation: the lane ships **frozen by default**, and statistical evidence at pre-registered thresholds is required to *unfreeze* (the §18.3 trigger). The burden of proof sits on the edge, never on caution. The same-timestamp analysis the checklist wanted is implemented — it gates re-opening, not freezing.
- **Monte Carlo bankroll simulation (RULE 5):** block-bootstrap ≥ 1,000-bet paths from the settled ledger, resampling **sport-day blocks** (preserves intra-day correlation that iid resampling destroys); at quarter-Kelly sizing report max drawdown p50/p95 and `P(bankroll < 0.5 × initial)` in every backtest report. Deployment aborts if that probability exceeds `mc_sim.max_ruin_prob` (0.01). Until the ledger has ≥ 300 settled clean bets, the report prints `INSUFFICIENT_HISTORY` rather than a simulation on noise.

# Section 7: Revised Config Schema

**Checklist items — status:** `consensus_outlier_points` ✓ v3.2 (M2); `push_pricing` ✓ v3.2 (M2); `annotations(target_kind, target_id)` ✓ v3.2 (SC1); `poll_venues` table ✓ v3.2 (SC2); `markouts.actual_offset_secs` ✓ v3.2 (SC3); `venue_pair = <venue>:self` ✓ v3.2 (SC4); idempotent reports reading stored `lane`/`trusted` ✓ v3.2 (SC5). Nothing to re-add.

**New keys introduced by this addendum** (merge into `config.yaml`; comments binding):

```yaml
risk_free_rate: { value: 0.043, source_url: "TODO-13wk-T-bill", confirmed_on: 2026-07-07 }
                                    # dated fact like fees [VERIFY value at build]
deployment:
  annualized_hurdle: 0.20           # capital deploys only above this (Section 3)
  xspread_autobook: false           # STRUCTURAL: floor max(2F,0.5%) > ceiling F+0.5%
                                    #   for all F > 0.5% (Section 2 derivation)
  auto_book_min_net_edge: "max(2*total_fees, 0.005)"   # same-venue ARB only
  leg_failure_suspend_rate: 0.20    # pair suspended from CERTIFY above this
  fok_window_secs: 5                # FILL_B IOC window (Section 4)
slippage:
  max_edge_fraction: 0.20           # slippage(q*) <= 20% of net edge (Section 5)
  depth_divisor: 5                  # coarse cap when ladder thin
mc_sim:
  n_bets: 1000
  block: sport_day                  # block bootstrap preserves intra-day correlation
  ruin_threshold: 0.5               # "ruin" = bankroll below 50% of initial
  max_ruin_prob: 0.01
  min_history_bets: 300             # below this: INSUFFICIENT_HISTORY, no simulation
mismatch_pricing:
  lgm: 1.0                          # loss-given-mismatch, conservative full stake
  rule_of_three: true               # p_upper = 3 / n mismatch-free positions
caps:
  per_event_frac: 0.05
  per_sport_frac: 0.10
  per_day_totals_frac: 0.15         # same-day same-league totals share drivers
kind_taxonomy:
  cross_venue: XSPREAD              # RULE 1: never called "arb" anywhere
  same_venue_locked: ARB            # NegRisk proof-passing, political, ladder cross
```

# Section 8: Operational Guards & Monitoring

**Checklist items — status:** log rotation ✓ v3.2 (OP1, 90 days); NTP sanity ✓ v3.2 (OP3, 5s alarm); SX settled-endpoint verification gate ✓ v3.2 (OP4 — and if absent, SX legs never leave REVIEW, stated); UMA resolver returns None until finality ✓ v3.2 (OP5) + `PARTIAL` v3.4; manual settle echo + `-y` ✓ v3.2 (OP6) — add `--dry-run` as a no-append preview alias; pinned exact dependency versions ✓ spec §16 (OP7); per-venue fetch success by hour ✓ v3.2 (OP11). Added since: 429-rate alarm + rate limits as dated facts (v3.4), daily DB backup with off-volume warning (v3.2/v3.4), canary bets (v3.2), certificate expiry on `rules_sha` change (Section 1, new).

# Independent Discoveries (new in this pass — none in any prior review)

1. **The equivalence gate is arithmetically insufficient, not just procedurally strict.** 100 mismatch-free positions bound `p_mismatch` only to ≤ 3% (rule of three), so priced settlement risk (≥ 3 points) exceeds every observed cross-venue spread (< 1 point median) — the v3.4 gate could pass and the trade still be −EV. Fix: `net_spread_edge` subtracts `(3/n) × LGM` (Section 1); positive net requires n ≥ 600.
2. **The auto-book window is the empty set for cross-platform trades.** `max(2F, 0.5%)` floor vs `F + 0.5%` REVIEW ceiling is empty whenever `F > 0.5%` — true for every venue pair at all non-tail prices. Fix: `deployment.xspread_autobook: false` structurally, with the derivation as the config comment (Section 2).
3. **Polymarket NegRiskAdapter conversion changes NO-basket lockup economics.** A complete NO basket may be convertible/redeemable via the negRisk adapter without waiting for resolution — `lockup_days ≈ 0`, which transforms the annualized-edge calculation for the one construct the project still believes in. Fix: verify the adapter mechanics per rule 3 in Phase 3; if confirmed, NO-basket `lockup_days` = conversion settlement time (Section 3).
4. **Tape confirmation has a leg-count bias across classes.** Requiring every leg tape-confirmed within 60s makes a 20-leg basket's tape-confirmed P&L structurally near-empty while 1-leg EV bets confirm easily — the per-class tape columns are not comparable. Fix: report per-leg confirmation rates and a leg-weighted tape basis; never compare classes on the all-legs-confirmed column.
5. **`risk_free_rate` would be the system's only undated magic number.** Every fee is a dated fact with source and staleness badge; the rate entering every annualized-edge decision must be too. Fix: `risk_free_rate` config block with `source_url` + `confirmed_on` (Section 7).

# Section 9: Changelog

| Issue ID | Severity | Location in v3.1 | Fix applied | Section |
|---|---|---|---|---|
| Cross-platform settlement risk | CRITICAL | §2, §8 (arb class) | Already: REVIEW-only + settlement matrix + covering-set (v3.2). New: XSPREAD taxonomy, Equivalence Certificate, rule-of-three mismatch pricing | §1 |
| Fee math fiction | CRITICAL | §6.5 | Already: single `leg_fee_cost`, dated facts, verified rates (v3.2–v3.3). New: full Fee Matrix incl. maker/withdrawal/gas, net-edge formula, empty-window derivation | §2 |
| Auto-booking fake arbs (M6) | CRITICAL | §8.4 | Already: `arb_stale_warn` auto-REVIEW (v3.2), extended to all locked kinds (v3.3). New: threshold floor/ceiling derivation, structural disable | §2 |
| Kelly missing (M8) | CRITICAL | §13.4 | Already: formula stated (v3.2). New: full derivation, equivalence proof, admissibility constraint, 1/4 justification | §3 |
| Capital lockup unpriced | MAJOR | §13.4 | Already: 5%/yr carry tag, later-venue horizon (v3.2). New: annualized formula, 20% deployment hurdle, dated risk-free rate | §3 |
| Slippage/depth ignored | MAJOR | §13.4 | Already: ladders mandatory, `INSUFFICIENT_DEPTH_DATA`, fillable_fraction (v3.2–v3.3). New: VWAP model, `depth/5`, `q*` at 20% of edge | §5 |
| UMA dispute exposure (OP5) | MAJOR | §11.2 | Already: None-until-finality (v3.2), PARTIAL (v3.4). New: lag charged as unhedged exposure, p95 lag reporting | §1 |
| Correlation/portfolio risk | MAJOR | §18.5 | Already: event-cluster CIs (v3.2), day-correlation acknowledged (v3.4). New: per-sport/day caps, sport-day-block Monte Carlo, ruin gate | §3, §6 |
| Both-legs risk | MAJOR | absent | New: execution state machine, LEG_RISK resize-or-unwind, paper leg-failure simulator, suspension rate | §4 |
| Backtest hygiene | MAJOR | §18 | Already: pre-registration, same-timestamp, cluster bootstrap, MDE (v3.2–v3.4). New: event-level 20% walk-forward holdout, MC report | §6 |
| Void/push P&L (M3) | MAJOR | §11.1 | Already: convention pinned (v3.2), PARTIAL (v3.4). New: per-venue table with fee-refund [VERIFY] cells | §2 |
| Mutable interpretation layer (SC5) | MAJOR | §9 | Fixed in v3.2 (stored lanes, never re-derived). Confirmed, nothing added | §7 |
| Config gaps (M2) | MINOR | §15 | Fixed in v3.2. Confirmed | §7 |
| Schema gaps (SC1–SC4) | MINOR | §9 | Fixed in v3.2. Confirmed | §7 |
| Staleness confound (M1) | MAJOR | §18.3 | Already: dual as-traded/same-timestamp regrade (v3.2), post-cutover only (v3.4). Deviation: freeze stays default; evidence gates UNfreezing | §6 |

# Direct Verdict

1. **Is the cross-platform arbitrage thesis salvageable? No.** It is dead three independent ways — empirically (+$0.13 across 44 settled positions, 88% vanish rate), arithmetically (the auto-book window is the empty set at all non-tail prices), and statistically (any achievable equivalence certificate prices settlement risk at ≥ 3 points against sub-1-point spreads); it survives only as a zero-capital XSPREAD research lane feeding the lead-lag study.
2. **Is the EV lane salvageable? Yes, conditionally** — it stays frozen until, on post-cutover data, a forecaster (the Pinnacle anchor is the live hypothesis, not the falsified soft median) beats venue mids on same-timestamp Brier with cluster-bootstrap CIs excluding zero, plus CLV > 0 and debiased calibration at pre-registered n; if the trigger never fires, the lane's residual value is the venue-leads-books indicator, which is real but not tradable here.
3. **Minimum viable product for Phase 1 (before any capital):** (1) the Fee Matrix wired through the single `leg_fee_cost()` with net-edge math and the structural XSPREAD auto-book disable; (2) the ARB/XSPREAD taxonomy with Resolution Equivalence Certificates and rule-of-three mismatch pricing in `eligibility.py`; (3) the exhaustiveness-proven same-venue NegRisk scan — the only live arb thesis this system has; (4) the corrected (v3.4) pregame-valid markout/confirmation columns feeding the paper leg-failure simulator; (5) power-disclosed pre-registration with cluster-bootstrap gates and the Monte Carlo ruin report. Capital deploys only after 30 settled proof-passing baskets and a passing ruin report — and the honest prior, given everything this project has already measured, is that the ruin report never gets the chance to matter because the baskets null out. Build it anyway: the instrument's product is the proof.
