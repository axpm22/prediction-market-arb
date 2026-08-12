---
title: Weaknesses & Lessons — what V2 must fix
type: reference
project: ArbitrageModel
status: stable
tags: [arbmodel, v1, weaknesses, postmortem]
created: 2026-07-01
---

# Weaknesses & Lessons — what V2 must fix

> [!abstract] Why this note exists
> V2 has the **same goal** as V1 and must be **very similar in shape**, but must not repeat V1's structural mistakes. This is the consolidated fix-list, distilled from `ARB_WEAKNESSES.md` and `EV_WEAKNESSES.md`. Every V2 build phase in [[03 - V2 Build Instructions]] traces back to an item here.

Related: [[01 - Current Model - How It Works]] · [[03 - V2 Build Instructions]]

---

## A. The deepest lesson (carry this into V2's design, not just its code)

> [!danger] Detection was honest; the path that *books money* did not enforce detection's safeguards.
> V1's failure mode was not bad math — it was that `REVIEW`, depth, and skew tags were set at detection but **ignored at settle/sizing/capacity**. The settled arb book was 100% cross-venue REVIEW: exactly the population hard-rule #6 forbids. **V2 rule: one eligibility predicate, defined once, enforced at every stage** (detection, fill, settle, reconcile, capacity, exposure).

Second lesson: **a signal is worthless until the input is proven clean.** Depth floors, one-sided-book rejection, and edge-sanity caps come *before* any scorer or Kelly layer, and everything downstream is gated on the clean column.

---

## B. ARB-path weaknesses

### CRITICAL — the ledger booked arbs it was forbidden to book
- **A1** Settle queries had no trust/status filter → 314/314 settled arbs were REVIEW. *Fix:* shared `eligible = trusted=1 OR venue_pair ∈ trusted_pairs`; REVIEW settles into a separate lane only.
- **A2** Reconciliation never ran, and *couldn't* — only a Kalshi resolver existed, so any Polymarket/SX leg skipped the whole bet. *Fix:* write `polymarket_leg_result` + `sx_leg_result`, compose resolvers by venue, run reconcile automatically after settle; label P&L `provisional` until reconciled.
- **A3** No timing guard — 85/314 arbs were placed *after* the event. *Fix:* reject/invalidate any bet with `placed_at > event_start/final` at both detection and settle.

### HIGH — capacity / sizing numbers were fantasy
- **B1** Zero captured ladders → single flat rung → profit is linear, never edge-limited, no slippage modeled. *Fix:* capture real multi-level ladders; until then report capacity as **UNKNOWN**, not a number.
- **B2** "\$72,209 summed profit" stacked three overstatements: no slippage + rows counted 4–5× (661 rows = 148 fingerprints) + no trust filter. *Fix:* dedup by position key, filter to actionable, before any aggregate.
- **B3** `depth_usd` computed three different ways across venues and trusted as 100% fillable. *Fix:* normalize to "taker \$-notional executable at this level," unit-test per venue, apply a fillable-fraction haircut, never fall back to event-level liquidity.
- **B4** Polymarket legs permanently single-level → every arb's binding leg unmodelable. *Fix:* per-token CLOB `/book` fetch for arb legs.

### HIGH — fee/edge mispricing that manufactures fake arbs
- **C0** Neg-risk basket used Kalshi's **quadratic** fee on **Polymarket** (linear) markets → undercharged → fake convert arbs in the *safest* class. *Fix:* linear fee for neg-risk both sides.
- **C1** 60s leg-skew counted as "simultaneous." *Fix:* tighten actionable window (≤10s) and treat skew as a continuous **edge haircut**, not a binary tag.
- **C2** No **position key** → one economic position tracked as many bets. *Fix:* `position_key = (sport, event_key, outcome, market_type, line)`; aggregate everything by it.
- **C3** Portfolio/capital constraint never bound detection or capacity. *Fix:* run exposure inside the scan; refuse arbs that breach per-venue/total caps.
- **C4** Capital charged to event *start*, not *settlement* (capital is locked until both venues settle). *Fix:* charge to later venue settlement time.
- **C5** No started/final guard at detection → arbs open on in-play/finished events. *Fix:* drop legs with `start_utc <= now` or missing start.

### MEDIUM / LOWER
- **D1** Kalshi start-time fell back to `close_time` (days late) → corrupt event_key. *Fix:* mark `start-unknown`, non-actionable, never invent a start.
- **D2** Doubleheaders collapse to one event_key. *Fix:* cluster legs by start time (≤2h) before pairing.
- **D3** SX winning-bet fee modeled as 0 → overstates every polymarket+sx arb. *Fix:* model SX winning-leg fee.
- **D4** Depth-unknown arbs still counted feasible. *Fix:* require a real depth basis for `feasible`.
- **E1–E3** Rounding-broke-lock rows, no audit trail for rejected arbs, BTC arb has no settler. *Fix:* guard downstream, write suspect/diagnostic rows, add resolver before enabling a class.

---

## C. EV-path weaknesses (the estimation risk lives here)

The chain is `p̂ (consensus) → EV=(p̂−c)/c → edge/Kelly`. Almost every weakness is an error in `p̂` or in ignoring its uncertainty.

1. **Biased de-vig** — proportional normalization overprices favorites. *Fix:* Shin/power method, chosen by Brier/log-loss on settled outcomes.
2. **Equal-weight median ignores book sharpness** — soft books count as much as sharp. *Fix:* weight/anchor to sharp subset; lower trust when only soft books exist.
3. **`p̂` treated as point truth** — a 5-pt edge from 3 disagreeing books == 12 tight books. *Fix:* carry consensus dispersion (SE/IQR), gate on **edge/SE ratio**, shrink Kelly by SE. *(deepest problem)*
4. **`(p̂−c)/c` ratio explodes on cheap contracts** — the "four-digit edge." *Fix:* make `edge_points = p̂−c` the stored primary edge; ratio is display-only.
5. **Measured favorite-longshot bias never fed back into `p̂`.** *Fix:* fit isotonic/Platt on settled **raw** quotes, apply `p̂_calibrated`.
6. **Flat additive slippage buffer** independent of depth/stake/volatility. *Fix:* slippage as `f(stake/depth)` + volatility term.
7. **Stale-consensus edge not decayed** — full edge to the cliff, then binary quarantine. *Fix:* continuous exponential staleness decay.
8. **No correlation/portfolio view** — same game's ML+total double-counted. *Fix:* correlation-aware caps, per-event/per-sport-day aggregates.
9. **CLV measured ask-to-ask with fees** — biases the proof-of-edge gate toward zero. *Fix:* CLV against **no-vig closing mid**; keep fees in P&L only.
10. **EV headline across 5 sports, consensus for only 2, never validated per segment.** *Fix:* require a passing **calibration** check (ECE/Brier-skill) per segment, not just positive ROI; scope EV to where consensus is available and calibrated.

> [!tip] If V2 fixes only three EV things
> (1) calibrate/de-vig `p̂` properly and validate with Brier tooling; (2) propagate `p̂`'s estimation error into the edge gate and Kelly; (3) fix CLV to no-vig mids so the go/no-go gate actually measures edge.
