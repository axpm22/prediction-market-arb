# Pre-registration — frozen 2026-06-11T22:20Z (before any cumulative-results analysis)

Two layers. **Analysis rules** below are frozen; amendments are appended with date and
reason, never edited. **Instrumentation** (adapters, detection guards, alerting, bug
fixes) may change freely with dated changelog entries — fixing a parser is not p-hacking.

## Hypotheses

- **H1 (existence):** true cross-venue arbitrage flags with locked edge ≥ 0.2% occur at
  least once per active game day on MLB across Kalshi/Polymarket.
- **H2 (reality):** ≥ 30% of flash-graded arb flags are VERIFIED (legs re-quoted within
  seconds still sum < $1).
- **H3 (capturability proxy):** median *sequential* edge (worst leg ordering) across
  VERIFIED arbs is > 0.
- **H4 (settlement):** among resolved arbs on whitelisted pairs, realized P&L equals
  provisional P&L in ≥ 99% of bets (settlement-mismatch cost < 1% of bets).

## Minimum evidence before any thesis judgment

- **n ≥ 100 flash-graded arb flags** (VERIFIED + VANISHED, excluding UNVERIFIABLE).
- No conclusions from smaller samples; interim reports are plumbing checks only.
- The 2026-06-11 4.5-hour staking test (baseline row 414) is explicitly a **plumbing
  test**: it passes/fails on whether every flag was verified, settled, and reconciled —
  not on P&L sign. Its n will be far below 30 and carries no statistical weight.

## Kill criteria (thesis dead, stop or redesign)

- VANISHED rate > 70% across n ≥ 100 flash-graded flags, or
- median sequential edge (worst ordering) ≤ 0 across VERIFIED flags, or
- settlement mismatches on any whitelisted pair > 1% of resolved bets.

## Method

- Point estimates: medians. Uncertainty: bootstrap 95% CIs (10,000 resamples) computed
  in the daily report once n ≥ 30; reported alongside n always.
- Stake convention for headline numbers: flat $1 per bet.
- Grades are taken from the **earliest** verdict row per bet (deterministic under
  concurrent writers).
- Bets carrying annotations (bug:*, settler-error, match-suspect) are reported in
  headline AND excluded in a parallel "clean" column; both are always shown.

## Amendments (append-only)

- **2026-06-12 — EV signal added (per EV_SPEC.md).** New hypotheses, frozen now before EV
  data accumulates. EV betting stays OFF; these gate whether it ever turns on.
  - **H-EV1 (CLV):** EV flags with edge ≥ `ev.e_min` beat their own venue close —
    mean CLV > 0 with a bootstrap 95% CI excluding 0, over **n ≥ 40** depth-floored flags.
  - **H-EV2 (targeting):** the venue lags the sportsbook consensus by a measurable,
    null-corrected interval (lead-lag, §5.2).
  - **EV kill criterion:** if mean CLV ≤ 0 with CI excluding a meaningful positive over
    n ≥ 40 flags, the EV thesis is dead — do not enable betting. Reason: no edge exists
    if flagged prices don't beat the venue's own close.
  - **Enable-betting gate:** BOTH H-EV1 and H-EV2 positive AND the venue×market-type
    event-match whitelisted (EV_SPEC §4). Until all three, EV is measured, never bet.
  - Method note: proportional de-vig is used (EV_SPEC §2.2); it mildly biases favorites/
    longshots. Per-book p_i are persisted so flags can be re-graded under another de-vig
    method without re-running the experiment. This is instrumentation, not an analysis-rule
    change.

- **2026-06-12 — BUILD_PLAN strategy experiments, hypotheses frozen before their data.**
  - **H-S1 (negative-risk basket):** intra-Polymarket convert/overshoot flags with edge ≥
    `negrisk.min_edge` produce positive CONFIRMED P&L over n ≥ 50 flags. Single venue,
    single resolution → no settlement-mismatch escape hatch. Also report the flag
    *lifetime* distribution (how fast the basket closes) — itself a result. Kill: confirmed
    median ≤ 0.
  - **H-S2 (favorite-longshot):** on settled clean bets, realized win rate diverges from
    the implied band midpoint in the literature direction (longshots overpriced, favorites
    underpriced) by ≥ `flb.flb_min_edge`, with ≥ `flb.min_samples_per_band` per band.
    Forward FLB bets are flat-$1, bands fitted before forward data. Kill: no band shows a
    significant divergence after n is reached.
  - **H-Phase4 (BTC same-oracle):** same-oracle short-horizon BTC cross-venue arb produces
    positive CONFIRMED P&L over n ≥ 100 flags — AND ONLY IF Kalshi and Polymarket are first
    proven to settle off the identical index (probed 2026-06: they are NOT, so the pair
    stays REVIEW and this hypothesis is dormant until the oracle match is documented). Kill:
    confirmed median ≤ 0, or oracles never provably identical.
