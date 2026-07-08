# Pre-registration (frozen 2026-07-08, before the first live poll)

Copied from SPEC.md §18 with the one `[NEEDS INPUT]` decision resolved (see
"Power decision" below). Changing any rule after data collection starts is a
**dated, versioned amendment** appended to the log at the bottom — never an edit.
Every null this instrument produces is **cadence-conditional**: claims read
"no edge visible at ≥60s observation latency," never "no edge" — sub-minute
dislocations are invisible to this instrument by construction.

## Populations

The two XSPREAD venue-pairs (kalshi+polymarket, polymarket+sx) are different
populations, pre-registered separately, never sharing a win rate, gate, or
headline.

## XSPREAD whitelisting gate (the only path to headline cross-venue P&L)

A venue-pair + market type enters `trusted_pairs` only after: ≥ 99% consistent
settlement over ≥ 100 reconciled, position-deduped, clean-lane positions for
that pair, **and** a human has read both venues' rulebooks for that market type,
**and** the settlement matrix section is fully GREEN. A reconciled mismatch
reverts the pair immediately. **Clearing this gate does not make XSPREADs
bookable** — 100 mismatch-free positions bound `p_mismatch` only to ≤ 3% (rule
of three), and eligibility prices that against the edge
(`net = gross − fees − (3/n)·LGM`); positive net requires **n ≥ 600**. The gate
is necessary; the pricing is binding; `deployment.xspread_autobook: false` is
structural (the auto-book window is the empty set at all non-tail prices).

## EV lane (frozen OFF, with a re-open trigger)

Inherited verdict: the de-vigged consensus was a worse forecaster than venue
mids (Brier 0.238 vs 0.221/0.223; consensus ECE 0.30) and the EV-subset venue
side won 420/877 = 0.479. Therefore `ev_enabled: false`.

**Re-open trigger** (all conditions, post-cutover data only, every CI
cluster-bootstrapped by event_key):

1. `arb regrade` shows consensus beats venue mids on Brier as a **paired**
   comparison (per-event differences, bootstrap 95% CI excluding 0), computed
   **both as-traded and at same timestamps** (the archived verdict may be a
   staleness artifact — trap 13);
2. EV-subset win rate > 0.5 with a 95% cluster-bootstrap CI excluding 0.5;
3. Two consensus definitions evaluated separately: the soft-book median
   (falsified once already) and the single sharp anchor (Pinnacle,
   `consensus_anchor`, h2h-only for budget) — the anchor is the live hypothesis.

If re-opened, go-live for a segment additionally requires mean CLV > 0
(one-sided 95% CI excluding 0; primary benchmark: the venue's own closing mid;
secondary: the no-vig consensus close within `clv_window_mins` of start; fees
excluded) over the pre-registered sample (see Power decision), AND segment
**debiased** ECE ≤ 0.05 with positive Brier skill. Kill rule: 10 consecutive
losing forward-test bets halts the test (pre-registered false-kill note: under
a null 50% loss rate a 10-loss run appears somewhere in ~1,200 bets with high
probability — a kill is halt-and-review, not itself evidence of negative edge).
ROI alone never promotes anything.

## Capacity / weighted-ARB enablement

`arb_size_by_ladder` flips true only after (1) ≥ 50% of feasible arbs carry
real multi-level ladders and (2) the capacity report shows genuine edge-limited
(not depth-fantasy) arbs. Until both: capacity says UNKNOWN, no weighted
headline exists.

## Class-specific "real if" clauses

- **NEGRISK:** real if corrected-fee basket edge > `negrisk.min_edge` appears
  at all in the exhaustive scan; the claim requires 30 settled baskets — only
  §8.4 proof-passing baskets count; `unproven-exhaustive` REVIEW baskets are
  tracked but can never promote the class. Structural note (pinned in
  tests/test_negrisk.py): at the shipped A1 defaults a YES basket with a
  bookable edge cannot pass the mids window — the NO side is the only
  construct that can clear cleanly.
- **BTC windows:** real only if the two oracles print the same binary result
  over ≥ 30 pre-set windows before `trusted` is even discussed; whitelisting
  still needs the XSPREAD gate. The BTC lane cannot be enabled without a
  registered settlement path (startup error).
- **AI matcher:** no Haiku budget expansion until a hand-labeled ground-truth
  set exists and stage-1 recall ≥ 80%; 50–80% = tune first. Bootstrap the
  recall harness on sports pairs (hundreds of confirmed-equivalent matches
  already exist in the manual map).
- **Maker shadow lane:** output is only "share of flags that flip +EV as
  maker" — an upper bound, never promoted on the flip fraction alone.
- **Ladder-aware totals:** first deliverable is verifying strike-level pairing
  fires at all (0 ARB may be the fees/edge null, not a matching gap).

## Minimum samples (amended by the Power decision below)

- 100 reconciled positions per XSPREAD pair for the *whitelist* gate
  (bookability additionally needs n ≥ 600 per the mismatch pricing);
- **~600 effective events (~1,200 bets at n_eff ≈ n/2) per EV segment** for
  any win-rate-gated promotion (`validation.ev_filter.target_n: 1200`);
- 200 settled quotes per calibration segment (debiased ECE);
- 30 settled proof-passing negRisk baskets;
- segment scoring blocked below n = 30; halfway checkpoints monitor, never
  promote.

All CIs cluster standard errors by `event_key` (a winner bet and a totals bet
on one game are not independent; ladder strikes are highly correlated). Known
residual: same-day same-league totals share weather/park drivers *across*
events — day-level correlation is an acknowledged limitation of these SEs.
Family-wise control: any positive result must **replicate in a second
out-of-sample month** before promotion.

## Power decision (resolves the spec's [NEEDS INPUT], 2026-07-08)

The spec disclosed that at n_eff ≈ 90 effective events the win-rate gate's
minimum detectable effect is ≈ 0.148 (only true win rates ≳ 0.65 reliably
detected). **Decision (made by the operator before the first live poll):
extend collection horizons to ~600 effective events per segment**, making a
modest true win rate of 0.55 detectable at 80% power / 95% confidence
(MDE ≈ (1.96+0.84)·√(0.25/600) ≈ 0.057). Consequences:

- `validation.ev_filter.target_n` = 1,200 bets (halfway checkpoint 600);
- collection horizons are ~6× longer than the draft gates; a non-firing
  trigger before target_n is **not evidence of no edge** and must not be cited
  as such;
- the kill rule (10 consecutive losses) and all other gates are unchanged.

## Amendment log

| Date | Change | Reason |
|---|---|---|
| 2026-07-08 | Initial freeze (v3.5 spec §18 + power decision: 600 effective events) | Pre-data |
