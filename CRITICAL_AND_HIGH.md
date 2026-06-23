# Critical & High issues — consolidated register (June 2026)

The actionable short list. Everything rated **CRITICAL** (books or loses real money / corrupts
the headline) or **HIGH** (materially distorts sizing, P&L, or the go/no-go decision) found
across the full investigation. Lower-severity items stay in `ARB_WEAKNESSES.md`,
`MODEL_ISSUES_SWEEP.md`, and `MODEL_ISSUES_SWEEP_2.md`. Each line: ID · where · status · one-line
fix. All evidence checked against the live `data/arb.sqlite`.

Status legend: **FIXED** (per the ARB_WEAKNESSES status block) · **OPEN** · **NEW** (found in the
final targeted sweep, not yet in another doc).

---

## CRITICAL

| ID | Issue | Where | Status |
|----|-------|-------|--------|
| A1 | Settlement ignored REVIEW → all 314 settled arbs were `trusted=0`, the population rule #6 forbids from P&L | `settlement.py` `_open_arb_bets` | **FIXED** (REVIEW lane + `trusted=1` filter) |
| A2 | Reconcile never ran *and* is a structural no-op (Kalshi-only resolver skips 100% of the kalshi+poly / poly+sx book) | `reconcile.py`, `kalshi.py:249` | **FIXED** — `polymarket_leg_result` + `sx_leg_result` added; `reconcile_settlements` dispatches per-leg by venue (`default_resolvers()`); realized P&L labeled provisional via `provisional_arb_count` until cross-checked. Tests: `test_polymarket_resolver_won_lost_void_none`, `test_sx_resolver_uses_ref_side`, `test_venue_dispatch_resolves_what_kalshi_only_skipped`, `test_poly_sx_arb_venue_disagreement_is_corrected`, `test_provisional_count_tracks_unreconciled`. **Caveat:** SX's settled-resolution `outcome` field + `/markets/find` read are not yet in SPEC §3.3 — resolver fails closed (None) until a one-time live verification (see below). |
| A3 | No settle-timing guard → 85/314 arbs settled with `placed_at` after the event (retroactive fake locks) | `settlement.py` `settle_sports` | **FIXED** (`INVALID_IN_PLAY` guard) |

No **new** criticals in the latest sweep — consistent with the diminishing-returns estimate.
A2 is now **FIXED**: `polymarket_leg_result` and `sx_leg_result` exist and `reconcile_settlements`
dispatches resolvers per leg by venue, so the 100% of the settled book that the Kalshi-only
resolver used to skip (170 kalshi+poly + 144 poly+sx = 314 arbs, every one with a Polymarket
leg) is now reconcilable. Proven offline on 30 real settled arbs: kalshi-only → 30 skipped/30
provisional; full venue map → 30 checked/0 provisional.

> **A2 open verification (discovered during the fix, not a new bug):** SX's settled-resolution
> read relies on `GET /markets/find?marketHashes=` and the `outcome` field (1/2/void), neither of
> which is in SPEC §3.3's verified field list (rule #3). `sx_leg_result` is implemented and unit
> tested against a recorded fixture, but **fails closed (returns None) on anything ambiguous**, so
> it can only skip, never fabricate a lock. Before trusting SX reconciliation in production, do one
> live read of a known-settled SX market and confirm the endpoint + field names, then promote the
> fixture to a recorded real response. Polymarket uses only SPEC §3.2 fields (`closed`,
> `clobTokenIds`, `outcomePrices`) and needs no such caveat.

---

## HIGH

| ID | Issue | Where | Status |
|----|-------|-------|--------|
| **N1** | **CLV — the EV go/no-go gate — defaults to the fee-loaded metric.** `grade_clv` measures `close − entry` with fees on *both* ends, biasing CLV toward zero. The unbiased no-vig version (`grade_clv_novig`) exists but only behind `--novig`, so the default verdict (`ci95[0] > 0 ? "edge demonstrated" : "NOT proven → EV stays disabled"`) runs on the biased number. Direction of bias is downward → it will wrongly say "not proven" and **keep EV switched off**, leaving real EV profit unrealized. | `experimental/ev/clv.py:99,202` | **FIXED** — `clv_result`/`main` default to `grade_clv_novig`; fee-loaded moved behind `--fee-loaded` (comparison only); `--novig` kept as a no-op alias. `_ev_flags` now filters `quality_flag IS NULL` (excludes 9/636 quarantined flags live). Tests: `test_fee_loaded_and_novig_verdicts_disagree`, `test_default_verdict_is_novig_and_flips`, `test_quarantined_flags_excluded_from_sample`. |
| B1/B3 | Capacity/sizing run on a single-level top-of-book fallback (0/661 legs have a real ladder); no slippage modeled, depth units not normalized across venues | `capacity.py`, adapters | **OPEN** (needs live ladder re-capture; logic spec written) |
| B4 | Polymarket legs are permanently single-level (batch endpoint), and every arb has a Polymarket leg → the binding leg's slippage is unmodelable book-wide | `polymarket.py:117-120` | **OPEN** |
| C0 | Neg-risk basket charges the Kalshi quadratic fee on Polymarket (linear) markets → under-charges → manufactures fake convert arbs in the *safest* class | `negrisk.py:68` | **FIXED** — both ask & bid fees now `all_in_fee_cost(..., basis="linear")`. Test: `test_c0_linear_fee_break_even_basket_is_not_an_arb` (a basket break-even under linear fees no longer flags; pre-C0 quadratic Σ<1 would have). Live footprint small (4 NEGRISK bets, 0 convert) → mostly preventive. Updated `test_sweep_persists_executable_legs_and_quality_flag` expected costs. |
| E4 | On an Odds-API abort, partial-book consensus is computed and upserted, overwriting good consensus → EV prices against a biased `p̂` | `cli.py:368-411` | **FIXED** — `run_scan` persists quotes but **skips the consensus write** on an aborted poll (prior complete consensus preserved). Tests: `test_aborted_scan_does_not_overwrite_prior_consensus` (+ `test_clean_scan_does_update_consensus` control). |
| E5 | `start_utc is None` is treated as pregame/actionable → a start-unknown quote can become a bet no time guard protects | `cli.py:468` | **FIXED** — `split_pregame` routes `start_utc is None` to the non-actionable lane (live=1, never detected/consensus). Test: `test_null_start_quote_is_non_actionable`. |
| S1 | `record_tracker_poll` full-scans all `flat_bets` every poll (O(bets×polls)) → silently throttles scan frequency → fewer captures | `tracker.py:102` | **FIXED** — new `lifecycle.active_rows_via_index` computes the active set via the fingerprint/status indexes; `_active_rows`, `_active_position_keys`, and `settlement_health._active_sports_bets` use it instead of `SELECT * FROM flat_bets`. Proven byte-identical to the Python derivation on the live DB; 52% fewer rows touched (8878→4226 candidates, then index lookups). Tests: `test_index_active_set_matches_python_derivation`, `test_index_kind_filter_matches_python_derivation`. (`exposure.py` already used an indexed status filter — left as-is to preserve its numbers.) |
| G1 | The depth/edge arb-sizing model trains on `trusted=1 AND settled` arbs — **0 rows** — so it can only ever return flat $1 (this is *why* "bet more on more depth/edge" produces nothing today) | `arb_sizing.py` | **OPEN (by data)** |
| H1 | EV-exit writes the same number to `pnl_optimistic` and `pnl_confirmed`, booking an optimistic entry as *confirmed* P&L → corrupts the confirmed headline | `ev_exit.py:178` | **FIXED** — `exit_decision` computes `pnl_optimistic` (from `optimistic_cost`) and `pnl_confirmed` (from `confirmed_entry_cost`, else **NULL**) separately; `_append_exit_row` stores them distinctly. H2: live reads routed through the rate-limited `KalshiAdapter._get` + a pooled Polymarket session. Tests: `test_ev_exit_never_confirmed_has_null_pnl_confirmed`, `test_ev_exit_confirmed_entry_populates_pnl_confirmed`. (Live DB has 0 EXITED rows — watcher stopped — so this is preventive; bug proven by code + test.) |
| I1/I2 | Scorer treats NULL `pnl_confirmed` as $0; 256/613 (42%) of clean settled EV are zeroed, and arbs (RESOLVED+NULL) score ~0 → profitable segments can be wrongly BLOCKED | `scoring.py:110` | **OPEN** |

---

## The one new finding, in full (N1)

**CLV is the instrument that decides whether EV ever goes live**, and its default reading is
biased. `grade_clv` computes `clv_points(close, entry_ask)` where both `close` and `entry_ask`
are **fee-loaded, same-side effective costs** (`clv.py:95,99`). `EV_WEAKNESSES.md #9` already
named this bias; the fix was *implemented* as `grade_clv_novig` (de-vigged two-sided mid, fees
excluded — `clv.py:163`) but it only runs with the `--novig` flag. The CLI verdict
(`clv.py:202`) and any default invocation use the biased metric.

Why it's HIGH, not cosmetic: loading fees on both ends pushes mean CLV toward (and below) zero,
so the pre-registered gate `ci95_low > 0` is more likely to read **"NOT proven → EV stays
disabled."** The failure mode is a *false negative on your own edge* — the model could be
genuinely beating the close and the gate would still keep EV off indefinitely. That's pure
opportunity cost on the strategy the whole consensus/EV stack exists to run.

Two smaller adjacent notes (MEDIUM, not promoted): `_ev_flags` applies no `quality_flag` filter,
so quarantined flags enter the CLV sample — but only **8 of 635** EV opens are quarantined, so
the contamination is small today; and the "close" proxy is the last venue quote, which is only a
true close if `watch` ran up to game start (a cadence dependency, not a code bug).

**Fix:** make `grade_clv_novig` the default (keep the fee-loaded one behind a flag for
comparison), and add the `quality_flag IS NULL` filter to `_ev_flags`. Re-run the gate before
trusting any "EV stays disabled" conclusion.

---

## Recommended order

1. **A2 follow-through** — build `polymarket_leg_result` + `sx_leg_result`; until then no arb's
   realized P&L is verified. (Highest leverage: it un-blocks A1's whole point.)
2. **N1** — flip CLV to the no-vig default; you may be sitting on a proven EV edge the biased
   gate is hiding.
3. **H1** — stop EV-exit double-booking optimistic as confirmed (cheap, corrupts the headline).
4. **E4 + E5** — partial-consensus overwrite and null-start-actionable; small silent wrong-number bugs.
5. **C0** — one-line fee-curve swap that's manufacturing fake neg-risk arbs.
6. **S1** — the per-poll full-table scan, before the ledger grows further.
7. **B1/B3/B4** — real ladder capture; until then sizing/capacity answers are top-of-book fiction (and **G1** stays inert regardless).
8. **I1/I2** — understand the 42% non-confirmation rate before trusting the segment gate.

Beyond this register, the remaining unread surface (daemon internals, `whales`/`mm_sim`/
`newslag`/`nonsports`/`webserver`, `bias.py`, `parlay`) is unlikely to hold new criticals/highs;
the higher-value next step is **verification** — run `settle`/`reconcile`/`scores`/`clv --novig`
against the live DB and the test suite to confirm the OPEN items empirically and catch the
dynamic bugs (lock contention, real settlement mismatches, calibration validity) that static
review can't.
