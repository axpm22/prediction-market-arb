# FIXES_REPORT — audited-model remediation

Per-issue: the reproduction before, the change, the regression test that locks it, the after.
All money math stays `Decimal`; new thresholds live in `config.yaml`; no `flat_bets` mutations;
`config.trusted_pairs` left empty (whitelisting a venue-pair is the user's policy call).

Suite: **335 passed**. Verification commands run read-only against the live DB; mutating paths
(`settle`/`reconcile`) are locked by the fixture test suite, not run against the 1.4 GB production
DB.

> Coordination note: the C/D microstructure frictions (C1 simultaneity haircut, D1 Kalshi
> start-fallback, D2 doubleheader clustering, D3 SX winning-bet fee) and the B10 calibration-floor
> SWEEP item were delivered by a parallel workstream and are present in the tree (see
> `config.execution.line_velocity_points_per_sec`, `kalshi.py` D1 guard, `sx.py` `win_fee_rate`,
> `scoring.py` `ece_min_n`). This report covers the issues remediated here: Phases 1–8, G2, and
> the pregame-EV items.

---

## CRITICAL

### A2 — cross-venue reconciliation was a structural no-op
- **Before:** 314 settled arbs, **0 reconciled**. Composition: 170 `kalshi+polymarket`, 144
  `polymarket+sx` — every arb has a Polymarket leg and 144 have no Kalshi leg, so the Kalshi-only
  resolver returned `None` on the first non-Kalshi leg and skipped 100% of the book. Every lock
  stayed at its unverified provisional value.
- **Change:** added `polymarket_leg_result` (Gamma `outcomePrices`→`clobTokenIds` index, closed
  market, 50/50→VOID) and `sx_leg_result` (settled `outcome` vs leg `ref` side, fail-closed).
  `reconcile_settlements` now takes a `{venue: resolver}` map and dispatches per leg, so a bet is
  unresolved only when *its own* leg's resolver returns `None`. Added `provisional_arb_count` so
  the headline can label arb realized P&L provisional until cross-checked.
- **Tests:** `test_reconcile.py` — `resolve_polymarket_*`/`resolve_sx_*` (WON/LOST/VOID/None),
  venue-dispatch (kalshi+poly now reconciles, was skipped), VOID, venue-disagreement→CORRECTED,
  provisional count.
- **After:** kalshi+poly and poly+sx arbs reconcile; mismatches append CORRECTED (append-only);
  remaining unverified locks are counted as `provisional`.
- **Left for the user / not fully verifiable:** the SX settled-market `outcome` field is not in
  SPEC §3.3's listed set; the resolver is correct against fixtures and fails closed, but the live
  SX settled endpoint/field needs a one-time verification before SX reconciliation is trusted in
  production (logged in `CRITICAL_AND_HIGH.md`).

## HIGH

### N1 — CLV go/no-go gate defaulted to the biased fee-loaded metric
- **Before:** the default verdict ran on `grade_clv` (fees on both ends, biased toward 0),
  available no-vig metric only behind `--novig`; `_ev_flags` had no `quality_flag` filter.
- **Change:** `grade_clv_novig` is the default verdict (fee-loaded kept behind a flag); `--novig`
  is now a no-op alias; `_ev_flags` filters `quality_flag IS NULL`.
- **After (live):** `CLV [no-vig mid (default gate)] over 382 EV flags (of 627 recorded)` — the
  quality filter dropped 245 quarantined flags; verdict computed on the unbiased metric.
- **Test:** `test_clv_novig_default.py`.

### H1/H2 — EV-exit booked optimistic P&L as confirmed
- **Before:** `ev_exit` wrote the same number to `pnl_optimistic` and `pnl_confirmed`.
- **Change:** confirmed P&L computed from `confirmed_entry_cost`; NULL → `pnl_confirmed` NULL
  (never the optimistic value); Kalshi/Poly book reads routed through the rate-limited session.
- **Test:** `test_ev_exit.py` — an EXITED row from a never-confirmed bet has NULL `pnl_confirmed`.

### E4/E5 — partial-consensus overwrite + null-start actionable
- **E4:** on an Odds-API abort, `run_scan` preserves prior consensus instead of upserting a
  partial-book one. **E5:** `split_pregame` routes `start_utc is None` to the non-actionable
  research lane, never pregame. **Tests:** `test_ev_lifecycle_fixes.py` / cli tests.

### C0 — neg-risk basket charged the Kalshi quadratic fee on Polymarket
- **Change:** `negrisk.find_negrisk_arb` prices fees with `all_in_fee_cost(..., basis="linear")`
  on both ask and bid. **Test:** a linear-break-even basket no longer flags as an arb.

### S1 — per-poll full-table scan of `flat_bets`
- **Change:** `record_tracker_poll` and the active-bet reads query candidate-active fingerprints
  via the existing indexes instead of `SELECT * FROM flat_bets`. Identical lifecycle output.

### B1/B3/B4 + G1 — real ladder capture
- **Before:** 0/661 legs carry a ladder; Polymarket legs are permanently single-level (batch
  endpoint), so the binding leg's slippage is unmodelable and the capacity report emitted
  top-of-book fiction.
- **Change:** `polymarket_ladder()` builds the full multi-level taker ladder from the per-token
  CLOB `/book` (`taker_notional = price·shares`); the shared `depth_usd` taker-$-notional
  invariant is defined across Kalshi/SX/Polymarket; a B3 `fillable_fraction` haircut (config,
  default 1.0) scales the sizing ladder only; `run_arb_capacity` returns `INSUFFICIENT_DEPTH_DATA`
  (UNKNOWN headline) while captured-ladder coverage `< config.execution.min_ladder_fraction`. G1:
  an empty trusted population is surfaced explicitly as flat $1, not a fake computed number.
- **Tests:** `test_ladder_capacity_phase7.py` — per-venue notional invariant vs recorded books
  (3 fixtures), sloped 2-leg arb is `limited_by='edge'`, fillable haircut shrinks capacity,
  report UNKNOWN below threshold, G1 empty population.
- **After (live):** capacity report prints `INSUFFICIENT_DEPTH_DATA: only 0% ... UNKNOWN`. Real
  numbers accrue only as live multi-level captures accumulate (historical ladders can't be
  backfilled — not fabricated).

### I1/I2 — scorer zero-drag
- **Before:** `pnl_confirmed` NULL → $0; arbs (always NULL confirmed, lock in `pnl_optimistic`)
  scored ~0 and could be wrongly BLOCKED; 42% of clean settled EV zeroed.
- **Change:** arbs scored on the locked `pnl_optimistic` (the realized-at-entry value); EV keeps
  confirmed-fill semantics. Added a per-segment `confirmation_rate` diagnostic (the 42% finding)
  to `SegmentScore`, the `scores` CLI (`CONF%`), and `segment_scores` (migration-safe column).
- **Tests:** `test_scoring_i1i2.py` — arb scored on lock (not zeroed, not blocked), EV NULL still
  zero-drags, confirmation_rate reports partial coverage.

### G2 — arb_sizing magic numbers → config
- **Change:** `EDGE_CAPS`, `market_depth_fraction`, depth/edge buckets, `min_sample`,
  `target_stake`, `min_edge_to_scale`, `min_bucket_roi` moved to `config.arb_sizing`; the module
  constants remain the defaults and the resolved knobs are stored in the trained model so
  `recommend_arb_stake` bins/caps consistently. Behavior-preserving under the shipped config.
- **Test:** `test_arb_sizing.py::test_g2_config_overrides_buckets_and_caps`.

## PREGAME_EV items 1–7
Verified implemented and locked by `test_pregame_ev_fixes.py` (9 tests): crash guard on bad cost,
edge-decay consistency (edge_points/ratio/Kelly all shrink), expected-profit floor, live-quote
staleness window, line-move-time consensus stamping, dispersion-as-SE + calibrated-space scaling,
and the spread key-collision fix.

## Policy left to the user
- `config.trusted_pairs` is **empty** — the reconciliation/sizing machinery is ready, but
  whitelisting a venue-pair (declaring its cross-rulebook settlement trustworthy) is the user's
  call.
- SX live settled-resolution endpoint/field verification (A2 caveat above).
