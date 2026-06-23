# Pregame EV — issues found

Scope: the pregame +EV path only — `detect_ev_from_consensus` (`arb/cli.py:522`), the
pricing/consensus math it calls (`arb/pricing.py`), and how `scan`/`watch` feed it
(`arb/cli.py`). Excludes the micro path (`detect_micro_ev_from_consensus`) and live baseball
EV. Ordered most to least serious.

Reviewed: 2026-06-23. Status updated after fixes (see each heading and the table at the end).

---

## 1. Every documented EV mitigation is disabled in the shipped `config.yaml`  — OPEN (config/validation decision)

The corrections from `EV_WEAKNESSES.md` are wired into the code but turned off by default:

- `devig_method: proportional` — favorite-biased de-vig (weakness #1)
- `weight_sharp_books: false` — equal-weight median across books (#2)
- `ev.dispersion_gate: false` — `p̂` treated as exact, no uncertainty haircut (#3)
- `ev.edge_decay_per_hour: 0.0` — no staleness decay (#7)
- `flb.apply_calibration: false` — measured favorite-longshot bias never applied (#5)

So the pregame EV that actually runs is the uncorrected version: biased devig, equal-weight
median of as few as 2–3 books (`min_books_for_consensus: 3`, totals `: 2`), no calibration,
no staleness decay. The "fixes" do nothing until the flags are flipped — and flipping them
should be gated on validation (Brier/CLV), not done blind.

## 2. Edge decay is only half-wired (`arb/cli.py:570–578`)  — FIXED

When decay is on, `gate_edge` (decayed) was used **only** for the `e_min` gate and the
edge/SE ratio. The stored `edge_points`, the EV ratio `edge`, the `capturable` note, Kelly
sizing (`p_for_kelly`) and the `is_economic` check all used the **undecayed** value, so a
flag stale enough to be down-weighted for gating was still ranked and sized at full edge.

**Fix:** introduced `eff_edge = gate_edge` and `eff_ev = eff_edge / effective_cost`, and
routed them into the stored `edge_points`, `edge` (ratio, which `is_economic` consumes), the
`capturable` note, and the Kelly base (`p_for_kelly = min(p_hat, effective_cost + eff_edge)`).
Under the default `edge_decay_per_hour: 0.0`, `eff_edge == edge`, so behavior is byte-for-byte
unchanged until decay is enabled.

## 3. The explosive EV ratio still drives a real decision (`arb/cli.py:610, 706`)  — FIXED (mechanism; off by default)

`Opportunity.edge` is the ratio `(p̂ − c)/c` (weakness #4, "should be display-only"), and
`is_economic(opp.edge, days, …)` consumes it. The ratio is dimensionally a per-dollar ROI,
so comparing it to the capital charge (also a rate) is defensible *for that one gate* — a
cheap contract genuinely earns a high return per dollar staked, even though the absolute
dollars are tiny. The real hazard is using the ratio for ranking/sizing; that path already
uses `edge_points`, and #2's fix now also feeds the **decayed** ratio into `is_economic`.

**Fix:** added an absolute expected-profit floor in `_annotate_quality_and_kelly` —
`kelly_stake × edge_points` must clear `ev.min_expected_profit_usd`, else the flag is
quarantined `below-min-profit` (and an `exp-profit-usd:` note is attached). Default is `0.0`
(off), so it's a no-op until you set a dollar floor; the ROI gate alone no longer lets a
tiny-notional longshot through once it's enabled. The headline `edge` stays the (now decayed)
ratio for display.

## 4. `expected_value` can crash the whole cycle (`arb/pricing.py:287–289` → `arb/cli.py:561`)  — FIXED

`expected_value` raises `ValueError` on `effective_cost <= 0`, and
`detect_ev_from_consensus` called it unguarded inside the per-quote loop. One malformed /
zero-cost quote aborted the entire `watch`/`scan` run.

**Fix:** guard at the call site (`if quote.effective_cost <= 0: continue`) right after the
consensus lookup, so a bad quote is skipped rather than killing the cycle. `expected_value`
keeps raising — a programmer error elsewhere should still surface loudly.

**Remaining:** `arb/experimental/closing.py:134` has the same unguarded call; apply the same
skip there if/when that path goes live.

## 5. Live quotes get the looser staleness window against pregame consensus (`arb/cli.py:587`)  — FIXED

`watch` evaluates both pregame and in-play quotes against stored consensus, which is
pregame-only. For a `live` quote, `not quote.live` was false, so it used `stale_cutoff` (8h)
instead of the tighter pregame window (3h). A 6h-old pregame line passed as "fresh" for a
game that's now in progress — backwards.

**Fix:** the stale gate now always uses `pregame_cutoff` (`is_stale = computed_at <
pregame_cutoff`). Since stored consensus is always pregame, a live quote compared against it
is flagged stale on the tighter window, not the looser one. The looser `stale_cutoff` is
retained only as the fallback when no pregame window is configured.

## 6. Consensus age is measured from scan time, not line-move time (`arb/cli.py:375`)  — FIXED

One `datetime.now()` stamped every consensus row per scan, so decay and the staleness gate
measured "time since we last scanned," not "time since the bookmaker moved the line."

**Fix (implemented):**

1. `Quote` gained `source_updated_at` (`arb/models.py`); the Odds API adapter now captures the
   market/bookmaker `last_update` (`arb/venues/oddsapi.py`), falling back to `None`.
2. New `consensus_outcome_times` (`arb/pricing.py`) + `compute_line_times` (`arb/cli.py`)
   produce the **median line-move time of the contributing books** per `(event, outcome)`,
   using the same completeness/min-books gating as the consensus itself, and falling back to a
   quote's `fetched_at` when a book can't report a move time.
3. `run_scan` passes those times to `save_scan_results` → `save_consensus`
   (`arb/store.py`), which now stamps each consensus row with its line-move time, falling back
   to the scan clock per-row when no time is available.

The detection side is unchanged — it already keys staleness and decay off `computed_at`; this
just makes `computed_at` mean the right thing. Verified end-to-end with a store round-trip.

## 7. Calibrated / uncalibrated mismatch in the dispersion gate (`arb/cli.py:558, 566, 595`)  — FIXED

`edge` used the calibrated `p̂`, but `disp` was the raw de-vigged **range** (max−min), so the
edge/SE ratio mixed spaces *and* used a statistic that grows mechanically with book count.

**Fix (implemented):**

- `consensus_dispersion` (`arb/pricing.py`) now returns a true standard error,
  `stdev(probs) / sqrt(n)`, instead of the max−min range (requires ≥2 surviving books).
- In `detect_ev_from_consensus`, when a calibrator is supplied, the stored SE is mapped into
  calibrated space by the **local slope of the calibration curve at `p̂`** (numeric derivative),
  so edge (calibrated) and SE now share units. Under no calibrator (default) the slope is 1 and
  nothing changes. Verified: an identity calibrator leaves `edge_se` unchanged; a `y=2x`
  calibrator doubles the SE and halves `edge_se`, as expected.

**Note:** because the SE is numerically much smaller than the old range, `ev.edge_se_min`
should be re-tuned against CLV before the dispersion gate is switched on (it remains off by
default).

## 8. Latent key collision (`arb/pricing.py:147–151`)  — FIXED

`consensus_outcome_key` only appended the line for `TOTAL`. Harmless today (only `h2h,totals`
are fetched, `arb/venues/oddsapi.py:18`), but the moment any handicap/spread market is added,
different spread lines — and spread vs moneyline — would collide on the same `(event, outcome)`
key in both `consensus` and `_cheapest_quote_per_outcome`.

**Fix:** key on the line whenever one is present (`if line is not None: return
f"{label}:{line}"`), not just for `TOTAL`. No-op for current markets (WINNER has `line=None`),
future-proof for spreads.

---

## Status summary

| # | Issue | Status |
|---|-------|--------|
| 1 | All EV mitigations disabled in shipped config | OPEN — config + validation decision |
| 2 | Edge decay only half-wired | **FIXED** (inert until decay enabled) |
| 3 | Explosive ratio gates uneconomic-horizon | **FIXED** — dollar floor mechanism (off by default) |
| 4 | `expected_value` can crash the cycle | **FIXED** (call-site guard) |
| 5 | Live quotes get looser staleness window | **FIXED** (always pregame window) |
| 6 | Consensus age = scan time, not line-move time | **FIXED** (line-move time plumbed through) |
| 7 | Calibrated/uncalibrated dispersion mismatch | **FIXED** (SE + calibration-slope scaling) |
| 8 | Latent spread/moneyline key collision | **FIXED** |

Only #1 remains, and it is intentionally a decision rather than a code change: turning on
`devig_method`/`weight_sharp_books`/`dispersion_gate`/`apply_calibration`/`edge_decay_per_hour`
should each be validated against CLV/Brier first. All the *machinery* they need is now in place
and correct.

All changes are behavior-preserving under the shipped `config.yaml` (decay off, dispersion gate
off, calibration off, no spreads, dollar floor 0). Two new opt-in knobs were added:
`ev.min_expected_profit_usd` (#3) defaulting to `0.0`.

> Verification: pytest could not run in this sandbox (the `.venv` symlinks point to macOS paths
> and there's no network to reinstall), so each fix was exercised directly with stubbed display
> deps — the locked EV tests (cheapest-per-outcome, stale/fresh quarantine, dispersion gate),
> plus new checks for the SE dispersion, line-move-time helper + store round-trip, calibrator
> slope scaling, the dollar floor, and the oddsapi `last_update` parse. Run `pytest -q` locally
> to confirm the full suite stays green.
