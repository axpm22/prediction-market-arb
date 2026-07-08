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

## LIVE lane (amendment 2026-07-09, frozen BEFORE the first persisted live poll)

**Hypothesis:** in-play cross-venue dislocations at ~20s cadence confirm at a
non-trivial rate and carry positive locked P&L after fees. Populations: live
kalshi:polymarket and live polymarket:sx, separate, never pooled with each
other or with their pregame counterparts. All live claims are
cadence-conditional at ≥20s observation latency.

**Kill gate:** at the earlier of 30 days (`live_lane.kill.max_days`) or 300
booked live flags per pair (`live_lane.kill.min_flags`) — if the live confirm
rate < 0.10 (`live_lane.kill.confirm_rate_floor`) **or** the confirmed-P&L
cluster-bootstrap 95% CI includes 0, set `live_lane.enabled: false` by dated
amendment. Data is kept (it feeds the lead-lag study); the lane stops booking.
Re-opening requires a new amendment.

**Confirmation carve-out:** the v3.4 rule "an in-play second sighting never
confirms" was written for pregame-booked bets drifting in-play and STANDS for
them. For bets booked by `poll_kind='live'`, in-play is the domain: a second
consecutive live-cycle sighting writes a `confirmed` bet_event tagged `live`.
The live confirm rate is the lane's primary statistic — the in-play analogue
of the 88% pregame vanish rate.

**Headline protection:** nothing in the live lane creates a path to the
headline. Live bets are review-lane by the unchanged `classify()` predicate;
an assertion test (tests/test_live_tracker.py) proves a booked live bet never
enters the headline in either implementation (report.py and headline.sql).
Any future live-to-clean gate is a separate pre-registered amendment with its
own n. Markouts stay pregame-only; the lane's honesty instruments are confirm
rate, lifetime, and locked-vs-confirmed drift.

## Lead–lag study (amendment 2026-07-09)

**Hypothesis:** one venue's mids systematically lead another's on the same
proposition. Primary statistic: first-mover tally — `p_lead = wins_A /
(wins_A + wins_B)` over matched same-sign moves (|Δmid| ≥
`leadlag.min_move_points`, matched within `leadlag.match_window_secs`), CI by
cluster bootstrap over event_key; a venue leads iff the 95% CI excludes 0.5.
Median lead time among won matches is the tradability bound. Secondary:
cross-correlation of change series at lags up to `leadlag.max_lag_polls`.

**Gate:** after ≥100 matched moves (`leadlag.min_matched_moves`) across ≥50
events (`leadlag.min_events`) for a pair — if no pair's `p_lead` CI excludes
0.5, record the null, close the study by dated amendment; the honest successor
claim is "no lead visible at ≥20s observation latency." The study also emits
per-sport/venue observed live line velocities that replace the PLACEHOLDER
`live_lane.line_velocity_points_per_sec` constants via a dated config change.

## Amendment log

| Date | Change | Reason |
|---|---|---|
| 2026-07-08 | Initial freeze (v3.5 spec §18 + power decision: 600 effective events) | Pre-data |
| 2026-07-09 | LIVE lane: hypothesis, kill gate, confirmation carve-out (poll_kind='live' only), headline protection | Pre-data for the lane (before first persisted live poll) |
| 2026-07-09 | Lead–lag study: first-mover tally + cross-correlation, gate, velocity re-estimation deliverable | Pre-data for the study |
| 2026-07-09 | Lead–lag tie rule: matches with \|Δt\| ≤ `leadlag.tie_epsilon_secs` (5) are ties — within one poll cycle the fetch ORDER (kalshi ~0.7s vs PM ~15s) stamps the earlier time, not leadership. First run (pre-gate, 32 events, no claim made) showed p_lead 0.778 with a 1s median "lead" — a fetch-latency artifact this rule removes | Pre-gate refinement; no claim had been made |
