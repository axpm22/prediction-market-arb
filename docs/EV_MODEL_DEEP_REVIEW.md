# EV model — deep review (June 2026)

Scope: the EV (expected-value) lane — consensus/de-vig (`pricing.py`), detection
(`cli.py::detect_ev_from_consensus`), sizing (`sizing.py`), calibration/FLB (`bias.py`),
segment scoring (`scoring.py`), CLV (`experimental/ev/clv.py`), closing (`experimental/closing.py`),
live (`experimental/live_baseball_ev.py`).

The machinery here is mature; most of the obvious statistical fixes from `EV_WEAKNESSES.md`
are already coded. The remaining weaknesses fall into five buckets. The single biggest one:
**the good fixes are implemented but shipped OFF by default**, so production runs the naive model.

---

## A. Best logic exists but is disabled in `config.yaml`

The code carries the better behavior behind flags that default to the naive path. As shipped,
the live model is the crude one.

1. **Edge decay is off.** `ev.edge_decay_per_hour: 0.0` (config.yaml:154). With 0, a pregame
   line that moved hours ago counts at full edge right up to the staleness cutoff
   (`detect_ev_from_consensus`, cli.py:628-631). Pregame markets move fast; full edge on an
   hours-old consensus is "venue vs stale line." Set a real per-hour decay and validate vs CLV.

2. **Dispersion gate is off.** `ev.dispersion_gate: false` (config.yaml:157). So `p_hat` is
   treated as point-truth: no edge/SE gate and Kelly is sized off the point estimate, not the
   lower bound (cli.py:664-672). A 5-pt edge from three disagreeing books is sized identically
   to one from three tight books.

3. **FLB calibration is off.** `flb.apply_calibration` gates whether the measured
   favorite-longshot curve is fed back into `p_hat` (cli.py:310, 603-615). Off => detection
   uses raw de-vigged `p_hat` even after you've measured it's miscalibrated.

4. **Sharp-book weighting is off.** `weight_sharp_books` false (cli.py:392-394) => Pinnacle and
   a tiny retail book vote equally in the median.

5. **Absolute profit floor is off.** `ev.min_expected_profit_usd: 0.0` (config.yaml:163). The
   headline ratio `(p−c)/c` explodes on cheap contracts, so a 2¢ longshot can clear the
   per-dollar uneconomic-horizon gate on rate alone (cli.py:777-785).

Recommendation: these aren't "parallel/measurement" niceties — at least decay, the dispersion
haircut, and sharp weighting should be the default and the naive path the opt-out.

---

## B. Statistical / prediction weaknesses (genuine gaps)

6. **Cross-book SE assumes independent books — it isn't.** `consensus_dispersion` computes
   `stdev(probs)/sqrt(n)` (pricing.py:264). Books copy each other and many resell the same
   originating feed, so they are positively correlated. Dividing by `sqrt(n)` of correlated
   samples **understates** the true SE, making the (optional) edge/SE gate overconfident exactly
   when books cluster. At minimum apply an effective-sample-size correction or a floor on SE;
   better, estimate book correlation and use it.

7. **No shrinkage of `p_hat` toward the prior.** A 3-book median is taken as exactly true
   (pricing.py:212-217). With few books, `p_hat` should be shrunk (toward 0.5 or toward a
   larger-book consensus) as a function of `n` and dispersion. Today the only haircut on
   estimation error is fractional Kelly (0.25) — a constant, not a function of how thin the
   consensus is. Thin consensus and deep consensus get the same trust.

8. **No correlation/portfolio view in sizing.** Each EV flag is Kelly-sized independently
   (cli.py:746-786, sizing.py). Multiple flags on the same game (moneyline + total, or both
   correlated legs) over-bet shared risk; simultaneous Kelly bets on positively-correlated
   outcomes exceed true Kelly. Group same-event/same-team exposure and cap jointly. (Listed in
   EV_WEAKNESSES #8 but still not implemented in the sizing path.)

9. **Isotonic FLB calibrator is fit and applied in-sample.** `fit_calibrator` (bias.py:129) runs
   PAV on the settled set with no holdout or cross-validation, yet `score_segments` has a
   `walk_forward_holdout` for the ROI gate. Applying an in-sample isotonic map to detection is
   overfitting; PAV on binary outcomes with modest n is high-variance. Fit on a fit-window /
   out-of-fold and regularize (monotone + minimum bin mass), mirroring the walk-forward already
   used for scoring.

10. **`segment_calibration` ECE threshold is statistically meaningless at the gate's own
    minimum.** It computes ECE once a segment has `>= bins` rows — default 10 rows across 10 bins
    (scoring.py:71). ECE on ~1 sample/bin is noise, but `calibration_gate` would BLOCK a segment
    on it (scoring.py:134). Require a far larger n (e.g. ≥ 200) before the calibration gate can
    fire, or use a smoother calibration metric.

11. **Integer-line totals: push is flagged, never priced.** A TOTAL whose line equals an integer
    can PUSH (stake returned, no payout), lowering true EV, but the model only tags
    `push-risk-integer-total` and keeps `(p−c)/c` (cli.py:758-763). Even a crude push-probability
    (from a Poisson/empirical run-total distribution) would correct EV instead of overstating it.

12. **Capital lock uses game start, not settlement time.** `is_economic`/uneconomic-horizon use
    `event.start_utc` (cli.py:772-775) as the moment capital frees up. Markets settle after the
    game ends (and some venues lag hours/days). Capital is locked longer than modeled, so the
    horizon gate is too lenient. Use expected settlement time, not first pitch.

---

## C. Logic / consistency bugs and edge cases

13. **The closing-EV recorder is a second, inconsistent EV path.** `experimental/closing.py:134`
    computes `expected_value(p_hat, effective_cost)` with **none** of the main lane's adjustments —
    no decay, no calibration, no dispersion haircut, no capital-charge/horizon gate, no
    push-risk flag. Two recorders that both produce "EV" rows but price them differently will
    bias any pooled analysis. Route both through one detection function.

14. **`consensus_probabilities` silently drops books with a smaller-but-valid outcome set.**
    `complete = max(..., key=len)` then keeps only books matching it exactly (pricing.py:191-194).
    If one stray book carries an extra outcome (bad parse, an alt market), every correctly-2-way
    book is discarded and consensus is built from the one anomalous book. Pick the **modal**
    complete set, not the max-length one.

15. **Sharp weighting via list duplication distorts the median oddly.** Sharp books are injected
    as `[prob]*reps` before `median()` (pricing.py:202-212). With an even count the median
    averages two middle entries, so a duplicated sharp value can land "between itself," producing
    a value no book quoted. Use a proper weighted median (or weighted mean of de-vigged probs).

16. **Mixed time bases in consensus age.** `consensus_outcome_times` falls back from
    `source_updated_at` to `fetched_at` per book (pricing.py:281) and medians them together, so
    "time since the line moved" and "time since we fetched" are averaged in one number — then
    feeds the decay in (1). When a book lacks a move-time, exclude it from the age median rather
    than mixing clocks.

17. **`p_for_kelly` cap can mis-order with the haircut.** `p_for_kelly = min(p_hat, cost+eff_edge)`
    then `max(0, p_for_kelly - haircut*disp)` (cli.py:664, 671-672). Under decay, `cost+eff_edge`
    < `p_hat`, then the haircut subtracts again — decay and dispersion both shrink the stake, which
    can double-count staleness. Confirm that's intended; if not, apply one or the other to the
    Kelly `p`, not both.

18. **Plausibility filter boundary clips legit favorites.** Quotes with `raw_price < 1.002`
    (implied > 99.8%) are dropped before the median (pricing.py:176). Heavy-favorite moneylines
    in lopsided MLB games can legitimately sit there; dropping them thins exactly the favorite
    side and can bias the de-vig. Widen the favorite bound or handle it explicitly.

---

## D. API / compute efficiency

19. **The three consensus passes re-de-vig every book three times.** `compute_consensus`,
    `compute_dispersion`, and `compute_line_times` (cli.py:390-397) each independently iterate the
    full quote set and call `devig_book_method` per book — and `power`/`shin` each run a
    200-iteration bisection (pricing.py:67, 91). For a full sweep that's 3× the de-vig work plus
    redundant grouping. Compute the per-(book,market,line) de-vigged vector **once** and feed all
    three (probs, SE, age) from it.

20. **Bisection runs a fixed 200 iterations with no early exit.** `devig_book_power` /
    `devig_book_shin` (pricing.py:67-73, 91-97) always do 200 halvings; convergence to 1e-6 takes
    ~20. Add a tolerance break. Minor per call, but it's inside the hot 3× loop above.

21. **CLV bootstrap is 10,000 pure-Python resamples** (`_bootstrap_ci`, clv.py:116-125). Offline,
    so low priority, but vectorizing (numpy) or lowering resamples would speed the report.

---

## E. Things worth adding

22. **Validate EV per segment before trusting it.** EV is the headline across 5 sports but
    consensus exists for ~2, and CLV/calibration aren't required per segment before a segment's
    flags count (EV_WEAKNESSES #10). Gate "clean EV" on a passing CLV **or** calibration check in
    that exact segment.

23. **Compare like-for-like price types.** `p_hat` is a no-vig mid; the venue side is ask+fee
    (correct, conservative). But the consensus is built from book **asks** de-vigged as if
    symmetric. If a book's two sides have asymmetric vig, the de-vigged "mid" is biased. Worth
    confirming the de-vig input is balanced two-sided quotes per book.

24. **Decay is linear and hard-clamped at 0** (cli.py:631). Information half-life is closer to
    exponential; a linear ramp to exactly 0 is a crude proxy. Low priority once a non-zero rate
    is even turned on.

---

## Priority order

1. Turn on the fixes you already built — decay, dispersion haircut, sharp weighting, profit
   floor (bucket A). Biggest risk-adjusted gain, zero new code.
2. Fix the independent-book SE and add `p_hat` shrinkage by n/dispersion (B6, B7) — these make
   every downstream gate honest.
3. Unify the closing and main EV paths (C13) and the modal-outcome-set bug (C14) — silent
   correctness.
4. Portfolio/correlation sizing (B8) before any real sizing goes live.
5. Efficiency (D19) — single-pass de-vig — when sweeps get large.
