# EV model fixes — implementation log (June 2026)

Every change is backward-compatible: new helpers and optional params/config keys that default to
**current behavior**, following the repo's existing gated-flag pattern. Existing pricing/devig/
consensus test vectors still pass; `consensus_all` was verified byte-identical to the two
functions it replaces across 50 randomized trials.

## Code changes by item

| Item | What was added | File | Default / how to enable |
|------|----------------|------|--------------------------|
| **B6** correlated-book SE | `consensus_dispersion(..., book_correlation)` — effective-sample-size correction `n_eff = n/(1+(n-1)ρ)` | `pricing.py`, `cli.py` | `ev.dispersion_book_correlation` (0 = legacy independent SE) |
| **B7** p_hat shrinkage | `shrink_probability()` — normal-normal (uses cross-book SE) or count shrinkage toward a prior; wired into detection | `pricing.py`, `cli.py` | `ev.shrink_prior` / `ev.shrink_prior_strength` (strength 0 = off) |
| **B8** portfolio sizing | `correlation_scaled_stakes()` + `group_correlated_stakes()`; `apply_correlation_sizing()` haircuts & caps same-event Kelly stakes | `sizing.py`, `cli.py` | `ev.correlation_rho` / `ev.correlation_group_cap_fraction` (0/0 = off) |
| **B9** calibrator overfit | `fit_calibrator(..., min_block_weight, holdout)` (minimum node mass + walk-forward window) + `crossval_calibration_gain()` (out-of-fold Brier gain) | `bias.py` | params default to legacy in-sample fit |
| **B10** calibration-gate n | `segment_calibration(..., min_n)`; gate consults `ece_min_n` (default 200) so ~1-sample/bin ECE can't block a segment | `scoring.py` | only active when `scoring.calibration_gate` is on |
| **B11** push pricing | `push_probability_poisson()` + `push_adjusted_edge()`; integer-total edge scaled by (1−push) in main and closing lanes | `pricing.py`, `cli.py`, `closing.py` | `ev.push_pricing` (false = legacy flag-only) |
| **B12** settlement lock | uneconomic-horizon uses event start **+ settlement lag**, not first pitch | `cli.py` | `ev.settlement_lag_hours` (0 = legacy) |
| **C13** unify closing EV | closing lane now applies the same push haircut; decay/dispersion documented as N/A at the close | `closing.py` | follows `ev.push_pricing` |
| **C14** modal outcome set | `_modal_complete_set()` replaces `max(...,key=len)` in all 3 consensus functions — a stray extra-outcome book no longer redefines the market | `pricing.py` | always on; ties break to longer set (uniform case unchanged) |
| **C15** weighted median | `_weighted_median()` replaces list-duplication for sharp weighting; reduces exactly to `statistics.median` at equal weights | `pricing.py` | always on; identical when sharp weighting off |
| **C16** age clock mixing | `consensus_outcome_times(..., require_move_time)` excludes books without a real line-move time | `pricing.py`, `cli.py` | `ev.require_move_time` (false = legacy fetched_at fallback) |
| **C17** decay+haircut | reviewed: the decay→Kelly-p coupling and dispersion haircut compound **by design** (both shrink stake under staleness); left unchanged, behavior documented | `cli.py` | n/a |
| **D19** single-pass de-vig | `_devig_cache()` + `consensus_all()`; scan calls `compute_consensus_and_dispersion()` → one de-vig pass instead of two | `pricing.py`, `cli.py` | always on; output equivalence-tested |
| **D20** bisection early-exit | power/shin bisection breaks at 1e-9 instead of always 200 halvings | `pricing.py` | always on; same result to 1e-6 |
| **D21** CLV bootstrap | `--resamples` flag threaded through `clv_result` / grade functions | `clv.py` | default 10000 (unchanged) |

## Bucket A (good logic already built, shipped off) — deliberately NOT flipped

`edge_decay_per_hour`, `dispersion_gate`, `sharp_books` weighting, `flb.apply_calibration`, and
`min_expected_profit_usd` are the existing EV#x machinery, kept off by config. The repo's
PREREGISTRATION/CLV philosophy says these stay parallel/measurement until they beat the median on
CLV, so flipping their canonical defaults would invalidate that pre-registered experiment. The
logic exists and is wired — enabling is a one-line config change per item once CLV validates them.
Recommended first to turn on once you have CLV sign-off: `edge_decay_per_hour`, the dispersion
haircut, and (now) `shrink_prior_strength` + `dispersion_book_correlation`.

## Validation done in this environment

The project's full `pytest` suite needs deps (`rich`, `requests`, …) not installable in the
sandbox, so validation was: (1) the repo's own pure-module test files (`test_pricing`,
`test_devig_methods`, `test_consensus_fees`) — 17/17 pass; (2) standalone property tests for every
new function; (3) `consensus_all` equivalence vs the separate functions over 50 random trials;
(4) `config.load_config()` loads with all new keys; (5) `compileall` over `arb/`. **Run the full
`pytest` once in your env to confirm the integration paths in `cli.py`/`closing.py` before relying
on the scan changes.**
