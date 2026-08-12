---
title: Code Audit Findings (New) — improvements not in prior sweeps
type: reference
project: ArbitrageModel
status: stable
tags: [arbmodel, v1, audit, improvements]
created: 2026-07-01
---

# Code Audit Findings (New) — K1–K9

> [!abstract] What this is
> A fresh pass over the V1 code and the live ledger (read-only), looking only for issues **not already catalogued** in [[02 - Weaknesses and Lessons]], `MODEL_ISSUES_SWEEP{,_2}.md`, `CRITICAL_AND_HIGH.md`, or [[00 - How It Works and What The Data Says]]. Numbered **K1–K9** (every other letter is taken). K1–K3 are quantified against the live DB. All should be folded into `SPEC_V2.md` by [[Model - Opus 4.8]].

---

## K1 — The flash verifier prices Kalshi with the WRONG fee curve ⚠️ highest payoff

`arb/experimental/verify.py:_kalshi_current_ask` calls `all_in_fee_cost(price, fee_rate, taker_buffer)` with no `basis` argument → defaults to **linear** (`rate·p`). Entry pricing uses **quadratic** (`rate·p·(1−p)`, `kalshi.py:161,180`). At `fee_rate: 0.07` the re-quote overstates every Kalshi leg's cost by `0.07·p²` — **0.6 to 3.4 points** across the price range, larger than most real arb edges (410 of 1,280 arbs sit in the 0–1% band).

**Measured impact (live DB):** of 161 VANISHED re-quotes with a parseable sum, **24 flip to VERIFIED** once the Kalshi leg is re-priced on the correct curve. Part of the headline "~88% kalshi+polymarket vanish rate" ([[00 - How It Works and What The Data Says]] §3) is the verifier's own fee error, not the market.

**Why nobody caught it:** `tests/test_verify.py` injects mock fetchers — the live `_kalshi_current_ask`/`_sx_current_ask` paths have zero test coverage.

**Consequence for V2:** R1 (verifier re-quote becomes `confirmed_cost`) would write this bias straight into the confirm column. **R1 must not ship until K1/K2 are fixed.**

*Fix:* pass `basis="quadratic"` for Kalshi; add a fixture test per venue asserting the verifier's effective cost equals the adapter's `effective_cost` at the same raw price.

## K2 — SX re-quote omits the win fee (and the slippage model) the entry price includes

Entry: `sx.py:149` `fee_cost = all_in_fee_cost(...) + win_rate·(1−cost)`, buffer via `effective_buffer(ladder, …)`. Re-quote: `requote_sx_leg` returns `cost + all_in_fee_cost(cost, rate, taker_buffer)` — **no win-fee term, flat buffer only**. Latent today (`sx.win_fee_rate: 0.0`), but the moment R4 turns the SX fee on, verification grades poly+sx arbs against a *cheaper* standard than entry — a bias toward VERIFIED, in exactly the population 00 §3 already flags as suspiciously persistent. Same class of asymmetry as K1, opposite direction.

*Fix (root cause for K1+K2):* one shared `leg_effective_cost(venue, raw_price, ladder) → Decimal` used by **both** the adapters and the verifier — the F2 "one predicate, every stage" principle applied to pricing. There should be no second implementation of "what does this leg cost."

## K3 — A venue outage mass-closes the book (close/reappear storm)

`cli.py:fetch_all_venues` (line ~520) catches any adapter exception, prints, and returns partial quotes. `record_tracker_poll` then closes every active fingerprint not in the poll as `absent-one-poll` — it cannot tell "gone from the book" from "venue didn't answer." Next poll they reopen as `reappeared`, corrupting lifetimes, bet counts, and the CLOSE bookkeeping.

**Measured impact:** 43 watch polls had at least one venue return **zero** quotes and still produced **240 CLOSE rows**; the ledger holds 989 `reappeared` rows overall.

*Fix:* record per-venue fetch health on the poll row; a fingerprint whose legs touch a failed/empty venue is exempt from absent-closing that poll (or require 2 consecutive misses). Cheap, and it protects every lifetime statistic.

## K4 — The skew-adjusted edge is computed, then thrown away

`arbitrage.py` C1 haircut computes `adj_edge` and the comment says it "is what sizing/ranking should trust" — but it lands only in a free-text note (`skew=…,adj_edge=…`). `edge_points` stores the **raw** margin, and `ladder_scaled_stake_quotes` runs **before** the haircut, so sizing consumes the unadjusted edge. Confirmed live: `edge_points` is NULL on **1,279 of 1,280** ARB rows anyway (R6), so the ledger can't bucket arb edges at all.

*Fix:* store `adj_edge` as a real column, compute sizing after the haircut, and populate `edge_points` on ARB rows at insert.

## K5 — The notes column is a shadow schema

Machine-readable facts live only inside comma-joined note strings: `floor:`, `capturable:`, `capacity:be=…,max=…,by=…`, `skew=…,adj_edge=…`, `flash-verify: re-quoted sum …`. The verifier's re-quote price — the value R1 wants as the confirm source — exists **only** as regex-parseable prose. Every downstream consumer (my K1 measurement included) has to scrape strings.

*Fix in V2:* first-class columns (`requote_sum`, `requote_lag_secs`, `adj_edge`, `capacity_*`, `guaranteed_floor`); `notes` becomes human-only. [[Model - Opus 4.8]] should freeze this in the Phase-1 schema.

## K6 — Per-venue exposure double-counts arb stakes

`exposure.py:59-60` charges each bet's **full** `stake_usd` to *every* venue in the bet. An arb split $0.52/$0.48 across kalshi/polymarket books $1.00 against each venue's cap — per-venue exposure reads ~2× reality for arbs, so venue caps bind early (conservative, but a wrong measurement in a measurement harness). Related: `_days_to_settlement` here still uses max leg **start** — the C4 settlement-lag fix reached detection's capital charge but not exposure's duration weighting.

*Fix:* charge each venue its own leg's stake (from `stakes`/`legs_json`); reuse the C4 `_hold_days` logic.

## K7 — One config knob, two incompatible meanings

`pricing.py:shrink_probability` interprets `prior_strength` as a **prior sd in probability units** when `dispersion > 0` but as a **pseudo-count of books** when `dispersion == 0`. Same yaml value, scales differing by ~100×, regime selected per-outcome at runtime by whether an SE happened to compute. A tuned-for-one-regime value silently misbehaves in the other.

*Fix:* two explicit keys (`shrink_prior_sd`, `shrink_prior_pseudocount`); error if both set.

## K8 — Divergence detection rescans all quotes per opportunity

`arbitrage.py:315` `event_quotes = [q for q in quotes if q.event_key == event_key]` inside the divergence loop — O(groups × total quotes) per poll, with ~3,400 divergences logged over the sample and quote batches in the thousands. *Fix:* build one `event_key → quotes` dict before the loop (three lines).

## K9 — `build_event` invents a start time

`arbitrage.py:87` `start = first.start_utc or datetime.now(timezone.utc)` — a start-unknown event gets stamped "starts now" and that fabricated time is persisted on the Event. Downstream guards happen to treat it as started (non-actionable, so no money leak), but D1's rule is *never invent a start*, and the stored data lies to any later analysis keyed on start. *Fix:* nullable start + explicit `start-unknown` flag, per the D1 pattern.

---

# Second pass — K10–K17 (precision errors ≥ 0.05% and further improvements)

> [!abstract] Scope of this pass
> A hunt for anything that moves recorded results by ≥ 5 bp: fee/rounding/P&L formulas in settlement, the adapters, the CLV gate, and the scorer. K10–K12 are quantified against the live DB.

## K10 — Cent-rounded P&L erases the arbs the harness exists to measure ⚠️

`tracker._optimistic_pnl` and `settlement._sized_pnl` quantize P&L with `money()` (2 dp). On a $1 flat stake, every arb with ROI < 0.5% books **exactly $0.00**, and every row carries up to ±$0.005 error — **±0.5% of stake, 10× this pass's threshold**.

**Measured:** 154 of 315 settled arbs booked $0.00. The 0–1% edge band — where theory says real arbs live, 410 of 1,280 flags — sums to **$0 recorded vs ≈ $0.85 true**. The "Settled arb P&L ≈ $0" headline in [[00 - How It Works and What The Data Says]] §1 is partly quantization, not just market truth. (EV is unaffected — magnitudes are ~$1.)

*Fix:* store P&L at `q6` (the ledger already stores costs at 6 dp); cents are a display concern. One-line change in each of the two functions + a backfill note.

## K11 — Kalshi quotes' price and ladder come from different moments, and `fetched_at` hides it

`kalshi.py` takes `cost` from the paginated `/markets` snapshot, then fetches each `/orderbook` sequentially at ≥ 0.15 s intervals (75+ s for 500 markets, plus 429 back-offs) — so a quote's price can be **minutes older than its own ladder**, and both get one `fetched_at` stamped at parse time. The C1 leg-skew haircut and the flash verifier both trust `fetched_at`, so cross-venue "simultaneity" for Kalshi legs is systematically better-looking than reality. In an MLB book moving ~0.1 pt/s this is easily ≥ 5 bp of phantom edge.

*Fix:* price the quote from the orderbook read itself (`1 − best opposing bid` — the same identity verify.py already uses) and stamp per-market fetch time; or at minimum stamp `fetched_at` per orderbook call. (WS feeds — R8 — solve this at the root.)

## K12 — The no-vig CLV gate silently excludes totals and isn't actually a mid

Two defects in the **default go/no-go instrument** (`clv.py:grade_clv_novig`):

1. It hardcodes `market_type="WINNER", line=None` — every TOTAL flag returns `None` and is dropped. **Measured: 245 of 627 clean EV flags (39%) are TOTAL-market** — invisible to the pre-registered gate that decides whether EV ever goes live.
2. `_two_sided_devig_mid` renormalizes **asks**, not (bid+ask)/2 — it's labeled a mid but carries the spread. Spreads narrow systematically into the close, so ask-renormalized "drift" conflates line movement with spread compression; on asymmetric books this biases mean CLV. A true mid is impossible today because bids are effectively never persisted (*corrected by K19: a `best_bid` column exists but is populated on 0 of 2.9M rows*).

*Fix:* key the mid lookup on the flag's own `(market_type, line)`; add `best_bid` (and spread) to the quotes schema — which also unlocks the maker-side analysis (L3) and real mid-based CLV.

## K13 — The cockpit still shows the biased CLV that N1 retired

`webserver.py:547` (`grade_clv(store, Decimal("50"))` — hardcoded depth floor, rule-#4 violation too) and `dashboard.py:247` both call the **fee-loaded** `grade_clv`. The N1 fix made no-vig the default in the CLI only; the two surfaces people actually look at still display the metric documented as "BIASED — comparison only."

*Fix:* both call sites → `clv_result(store, fee_loaded=False, …)`; move the floor to config.

## K14 — The push haircut leaks into the calibration measurement

`scoring.segment_calibration` reconstructs `p̂ = optimistic_cost + edge_points`, but stored `edge_points` is the **post-haircut** edge (push-adjusted via B11, decay-adjusted via EV#7). ECE is then computed on a distorted p̂ — on an integer MLB total the Poisson push haircut alone is ~13% of the edge. The calibration gate (EV#10) judges a number that was never the model's probability estimate.

Related improvement: `push_probability_poisson(line, λ=line)` overstates MLB push mass (pmf(9;9) ≈ 13.2% vs ~8–9% empirical — run totals are over-dispersed and the line is a median, not a mean). That's a 4–5 pt overshoot scaling every integer-total edge.

*Fix:* store raw `p̂` (or pre-haircut edge) as its own column; replace the Poisson default with an empirical push table fit from settled totals (the 2 GB quote archive already contains the sample).

## K15 — ARB segments structurally forfeit the win-rate weight

`score_segments` computes `win_rate = WON/n`, but settled arbs have `result='RESOLVED'` — win_rate is identically 0, so every ARB segment loses the full `w_wr` (15%) of its score and sits closer to `BLOCKED` for a reason that has nothing to do with performance. *Fix:* renormalize weights per kind (drop `w_wr` for kinds without WON/LOST semantics).

## K16 — Polymarket discovery caps at 100 events, unpaginated

`polymarket.py` `GET /events … limit=100` with no cursor loop. Fine for MLB; the soccer tag can exceed 100 concurrent events → silent coverage loss (an opportunity leak, not a pricing error). *Fix:* paginate like the Kalshi/SX adapters already do.

## K17 — B3's event-liquidity depth fallback survives on one path

`polymarket.py:112` `depth = … else liquidity` — when `clobAskDepths` is absent (fixture path) a quote's depth falls back to event-level liquidity, the exact pattern B3 banned. Live path is unaffected. *Fix:* `None` instead of `liquidity`; let the depth-unknown guard (D4) handle it.

---

# Third pass — K18–K20 (first run of [[05 - Cowork Audit and Research Prompt]])

> [!note] Data note (1 Jul 2026, ~23:08)
> Mid-audit, `data/arb.sqlite` was archived to `data/archive-pre-v2/arb.sqlite` and a fresh DB started (V2 cutover in progress). K18–K19 are quantified against the **archived V1 DB** (11,806 bets / 2.94 M quotes). Future audit runs must point Step 3 at the archive for V1 claims.

## K18 — The CLV "close" can be an in-play price

Every main-lane adapter hardcodes `live=False`, but Kalshi/Polymarket/SX keep trading in-play and the daemon keeps persisting those quotes — so `live=0` does not mean pregame. Both close lookups (`clv.venue_close` and `_two_sided_devig_mid`) take the **last** `live=0` quote with no start-time bound, so a 7th-inning price can grade as "the closing line."

**Measured (archived V1 DB):** 14 of 184 clean EV flags with a gradeable close (**8%**) had their "close" fetched **after** the leg's start — worst case **288 minutes** into the game. In-play prices drift toward 0/1, so 8% contamination is far beyond 5 bp on mean CLV and can flip the pre-registered `ci95_low > 0` verdict either way.

*Fix:* bound the close query at the leg's `start_utc` (`AND fetched_at <= ?`); persist `start_utc` on quotes rows so the bound doesn't depend on `legs_json`; set `live` from `start_utc <= fetched_at` instead of hardcoding.

## K19 — `best_bid` is a dead column (and the bid data is free)

The `quotes` schema, `models.Quote`, and `store.save` all carry `best_bid` — but it is populated on **0 of 2,946,326** archived rows. Reading the code: only the Kalshi **WINNER** path (`kalshi.py:182`) wires it; the Kalshi TOTAL path, Polymarket, and SX never set it — even though Polymarket's `/book` response already contains the bids (they're parsed for asks and discarded) and SX's opposing maker orders are the bids. This is the single blocker for K12's true no-vig mid and for any maker-side analysis (L3), and the marginal fetch cost is zero.

*Fix:* populate `best_bid` in all three adapters (Kalshi TOTAL included) from data already in hand.

## K20 — The BTC lane prices Polymarket off Gamma's stale `bestAsk`/`bestBid`

`btc_sweep.py:100` builds Polymarket legs from Gamma's `bestAsk`/`bestBid` — the exact fields `polymarket.py`'s own SPEC §3.2 comment bans for executable pricing ("Gamma outcomePrices are stale mids"; the sports path was moved to CLOB `/book` for this reason). Every BTC cross-venue "arb" has one leg priced on an indicative, stale number — the same phantom-edge mechanism as the >4% trap class. Currently latent (`data/stop-btc` pauses the lane; E3 already notes it has no settler), but it silently poisons the lane the day it's re-enabled.

*Fix:* per-token CLOB `/book` for BTC legs, same as the sports adapter; add a fixture test asserting the BTC leg price equals the book ask, not the Gamma field.

---

## Priority order and V2 wiring

| Rank | Finding | Payoff | V2 hook |
|---|---|---|---|
| 1 | K1 verifier fee basis | Corrects the headline vanish-rate; unblocks R1 | Phase 5 spec + adapter-parity fixture tests |
| 2 | K2 shared leg-cost function | Prevents the *next* K1 when R4 lands | F5 / SPEC_V2 §Math |
| 3 | K3 outage close-storms | Protects every lifetime/count statistic | Phase 6 tracker spec |
| 4 | K4+K5 columns not notes | Makes R1/R6 implementable at all | Phase 1 schema (Opus 4.8) |
| 5 | K10 q6 P&L storage | Un-erases the 0–1% arb band ($0 → ~$0.85); ±0.5%/row error gone | Phase 6 ledger spec |
| 6 | K11 Kalshi intra-quote staleness | Honest skew guard + verifier; interim step toward R8 (WS) | Phase 3 adapter spec |
| 7 | K12+K13+K18+K19 CLV gate (totals, true mid, pregame-bounded close, populated bids, surfaces) | The EV go/no-go instrument measures what it claims | Phase 1 schema + Phase 7 |
| 7b | K20 BTC lane pricing source | Must land before the lane is ever re-enabled | Phase 3/8 |
| 8 | K6 exposure measure | Honest per-venue caps | Phase 8 |
| 9 | K14+K15 scorer honesty | Calibration gate on real p̂; arbs not auto-penalized | Phase 7/8 |
| 10 | K7–K9, K16–K17 | Hygiene | Phases 2/3/5 |

> [!important] Sequencing note for [[03 - V2 Build Instructions]]
> R1 ("use the flash verifier as the confirm source") is the top-ranked fix in [[00 - How It Works and What The Data Says]] §8 — but it inherits the verifier's pricing. **K1/K2 are prerequisites of R1**, and the venue-pair split (R3) should be re-read after K1: the kalshi+poly vs poly+sx asymmetry is partly instrument error.

Related: [[00 - How It Works and What The Data Says]] · [[02 - Weaknesses and Lessons]] · [[Model - Opus 4.8]] · [[Model - Sonnet 5]]
