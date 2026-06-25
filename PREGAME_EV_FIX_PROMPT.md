# Claude Code prompt — implement the pregame EV fixes

Read `CLAUDE.md` and `SPEC.md` first and obey every hard rule (Decimal-only money math, no
magic numbers — every new threshold goes in `config.yaml`, paper-only, etc.). Scope is the
**pregame +EV path only**: `detect_ev_from_consensus` in `arb/cli.py`, the math in
`arb/pricing.py`, consensus storage in `arb/store.py`, and the Odds API adapter
`arb/venues/oddsapi.py`. Do **not** touch the micro path (`detect_micro_ev_from_consensus`)
or live baseball EV.

Implement the following fixes. Every change must be **behavior-preserving under the shipped
`config.yaml`** (new knobs default to off / no-op), and `pytest -q` plus the SPEC §9
acceptance commands must stay green. Add or extend tests for each new behavior. Work in order;
run `pytest` after each fix before moving on.

## 1. Crash guard on bad cost
`expected_value` (`arb/pricing.py`) raises `ValueError` on `effective_cost <= 0`, and
`detect_ev_from_consensus` calls it unguarded inside the per-quote loop, so one malformed /
zero-cost quote aborts the whole scan/watch cycle. In `detect_ev_from_consensus`, skip
(`continue`) any quote with `effective_cost <= 0` right after the consensus lookup. Leave
`expected_value` raising. Add a test that a zero-cost quote yields no opportunity and does not
raise.

## 2. Edge-decay consistency
Edge decay (`ev.edge_decay_per_hour`) currently shrinks `gate_edge` only for the `e_min` gate
and the edge/SE ratio, but the stored `edge_points`, the EV ratio, the `capturable` note, and
Kelly sizing all use the undecayed value. Make the decayed edge the **effective** edge for all
of them: introduce `eff_edge = gate_edge` and `eff_ev = eff_edge / effective_cost`, store
those as `Opportunity.edge` / `edge_points`, use `eff_edge` in the `capturable` note, and size
Kelly off `min(p_hat, effective_cost + eff_edge)`. Under the default `edge_decay_per_hour: 0.0`
this must be identical to today. Add a test: with decay on, `edge_points`, ratio, and
`kelly_stake` all shrink with consensus age.

## 3. Absolute expected-profit floor
The headline EV is a per-dollar ROI, so the `is_economic` uneconomic-horizon gate can pass a
tiny-notional longshot on rate alone. Add a new config knob `ev.min_expected_profit_usd`
(default `0.0` = off). In `_annotate_quality_and_kelly`, when it's > 0 and
`kelly_stake * edge_points < min_expected_profit_usd`, set `quality_flag = "below-min-profit"`
and attach an `exp-profit-usd:<value>` note. Plumb the knob through `arb/config.py`,
`config.yaml`, and the `AppConfig` dataclass. Add a test (on when set, no-op at default).

## 4. Live-quote staleness window
Stored consensus is always pregame (the scan never lets in-play odds become consensus), yet a
`live` quote is compared against it with the looser `stale_cutoff` (8h) instead of the tighter
pregame window (3h) — backwards. Change the stale check to always use `pregame_cutoff`
(`is_stale = computed_at < pregame_cutoff`); keep `stale_cutoff` only as the fallback when no
pregame window is configured. Add a test: a live quote vs a 5h-old pregame consensus is flagged
`stale-consensus`.

## 5. Consensus age = line-move time, not scan time
Today one `datetime.now()` stamps every consensus row, so staleness/decay measure "time since
we scanned," not "time since the line moved." Plumb the book's real update time through:
- Add `source_updated_at: datetime | None = None` to `Quote` (`arb/models.py`).
- In `arb/venues/oddsapi.py`, capture each market's (else bookmaker's) `last_update` and set
  `source_updated_at` (parse ISO, handle `Z`); `None` when absent.
- Add `consensus_outcome_times(quotes, min_books, min_books_totals=None)` to `arb/pricing.py`,
  mirroring `consensus_dispersion`'s gating, returning the **median** `source_updated_at`
  (falling back to `fetched_at`) per `(market, outcome)`. Add a `compute_line_times` wrapper in
  `arb/cli.py`.
- In `run_scan`, compute the line times and pass them to `save_scan_results` →
  `save_consensus` (`arb/store.py`) as an optional `outcome_times` map; stamp each row's
  `computed_at` from it, falling back per-row to the scan clock. Keep the new param keyword and
  optional so existing callers are unaffected.
Add a store round-trip test: a row stamped with a 2h-old line time loads back with that time.

## 6. Dispersion = standard error, in calibrated space
`consensus_dispersion` returns the max−min **range**, which grows with book count, and the
edge/SE gate mixes a calibrated edge with raw-space dispersion. Fix both:
- Change `consensus_dispersion` to a true SE: `stdev(probs) / sqrt(n)` (require ≥2 books).
- In `detect_ev_from_consensus`, when a calibrator is supplied, scale the stored SE by the
  **local slope of the calibration curve at `p_hat`** (numeric derivative) so edge and SE share
  units. Slope is 1 (no-op) when no calibrator. Add a test: identity calibrator leaves
  `edge_se` unchanged; a `y=2x` calibrator doubles the SE.
Note in a comment that `ev.edge_se_min` will need re-tuning before the dispersion gate is
enabled, since the SE is numerically smaller than the old range.

## 7. Latent key collision (spreads)
`consensus_outcome_key` (`arb/pricing.py`) only appends the line for `TOTAL`. Make it key on
the line whenever one is present (`if line is not None: return f"{label}:{line}"`) so future
handicap/spread lines can't collide with the moneyline or each other. No-op for current markets
(WINNER has `line=None`).

## Acceptance
- `pytest -q` green; no existing test modified to pass (only added).
- `python -m arb watch --once --fixtures` and `python -m arb scan --dry-run --fixtures` run
  without error and the EV output is unchanged vs `main` under the default config.
- Show me the diff and the test output for each fix.
