# Whale-follow backtest — results (2026-09-05), **Revision 2**

Pre-specified plan: `reports/backtest-whale-follow-plan-2026-09-05.md`, frozen before
any result below was computed. Nothing here changes the lane; anything it suggests goes
through PREREGISTRATION.md as a dated amendment.

Command: `python tools/backtest_whale_follow.py --slippage 0.01 --seed 20260905`
 (split 2026-07-16, 2000 bootstrap resamples, Bonferroni-12 quantile 0.016667).

## Changelog — Revision 2 (2026-09-05)

Revision 1 (commit `aebfa6c`) was reviewed; the verdict survived, the test-window
numbers did not. Each correction below and what it did to the numbers:

1. **K=2 is now the plan's rule** — one stake per (market, outcome) at the 2nd distinct
   qualified wallet, not one per qualified wallet. *Effect:* every K=2 cell changes; the
   headline `Q=all K=2 E=hold` test mean goes from +0.0276 (Revision 1) to -0.0087 (n 5530),
   and its n falls with it. The withdrawn rule is kept as a labelled diagnostic.
2. **The exit leg pays the venue taker fee** (SPEC.md's sell basis;
   `whale_book.exit_payload`). *Effect:* every `E=exit` cell falls by roughly the
   Polymarket fee on the exit price; the exit rule was already the worse of the two and
   is now worse by more. No hold cell moves.
3. **New "Sample construction" section** — per-era censoring, the pre-2026-02 drop and
   what it would do to train, split-window extra stakes, source mix, and the fact that
   the bootstrap clusters on condition_id, not event. *Effect:* no number changes; the
   bounds are relabelled honestly and the live era's right-censoring is now visible.
4. **Limitation 1 rewritten to the actual mechanism** — the wallet set is a leaderboard
   sample, and two of the four boards look back INTO the test window. *Effect:* no
   number changes; the reading of the test window does.
5. **New post-hoc cut** (labelled, not in the plan): the 12-config grid on wallets first
   seen before 2026-06-01, split by leaderboard membership, the train/test overlap
   cohort, and a weekly series of the baseline. *Effect:* adds evidence, promotes
   nothing.
6. **The fee schedule is stale-badged** and the ROI denominator is stated against the
   lane's (entry, not entry + fee: ≈1.8% of magnitude). *Effect:* no number changes.


## What the run says (numbers only; the verdict rule is the plan's)

- **No configuration is FAVORABLE** under the plan's rule (test Bonferroni-12 LB > 0 and the train sign agrees).
- Baseline `Q=all K=1 E=hold`: train -0.0549 (n 13225), test +0.0110 (n 39811), live era -0.0565 (n 13210), per $.
- Tightest cell `Q=top20 K=2 E=hold`: train -0.2546 (n 37), test +0.0402 (n 544), live era -0.0932 (n 101). The skill filter's SIGN is not stable across the three eras.
- The exit rule costs money nearly everywhere: mean of (exit − hold) over the 18 (split, Q, K) cells = -0.0056 per $ — and it now pays the taker fee on
  the sell, which is part of why.
- Positive cells, out of 12: train 0, test 6, live era 2. The test window (2026-07-16 →
  2026-09-04, entirely backfilled /activity prints) holds nearly all of them, and the
  Bonferroni-12 correction removes even those.
- The test window is also the window the wallet set was SELECTED in (limitation 1) and
  the one whose markets have all resolved (Sample construction). Both point the same
  way, and the post-hoc cut below is the attempt to separate them.


## Deviations from the plan

1. **The fee.** The plan asks for the venue fee "exactly as `arb/whale_score.realized_edge` charges it". `realized_edge` charges NO fee — it is a gross per-share
   edge. Implemented instead: `realized_edge`'s own accounting (proceeds − cost, per
   share, divided by the entry price to become per dollar) minus the Polymarket taker
   fee from the lane's single fee site, `venues.fees.leg_fee_cost`, priced with
   the position's root as the fee category — the same charge `arb/lanes/live.py` puts on
   the whale lane's own paper bets. **Revision 2:** the SELL leg pays its own taker fee,
   `bid − leg_fee_cost(bid)`, exactly as SPEC.md's exit-engine sell basis and
   `arb/lanes/whale_book.exit_payload` charge it. A hold to resolution still pays exactly
   one fee (resolution is not a trade); only `E=exit` pays two.
   The fee schedule's `confirmed_on` is 2026-07-02 — **65 days old against `fee_stale_days` 30: STALE-BADGED**, not halting, per §6.5.
   And the denominator: this report divides by the entry price, while the lane divides a
   $1 stake by entry + fee. Every ROI/$ here is therefore about 1.8% larger in magnitude
   than the lane's own accounting of the same trade (at 0.40 and the 3% sports rate,
   fee/entry = 0.018); it moves no sign and no ranking.
2. **Top-50%/top-20% is the lane's own bar**, `arb/whale_elite.cutoff` with `elite_pct`
   50 and 20: the reference population is every wallet with >= 1 resolved outcome in the
   root as of that month, each scored into the candidate's bucket by the transfer chain,
   and the cutoff is nearest-rank from the top. A wallet outside the population (no
   resolved history) is still compared to the cutoff, exactly as `whale_elite.point`
   does, so it can pass on the prior alone. Where a population is too small to have one
   elite member the cutoff is None and NOTHING qualifies in that bucket that month.
3. ~~**Consensus counts every later entry**~~ — **withdrawn in Revision 2.** K=2 is now
   the plan's own wording: ONE stake per (market, outcome), taken at the time and entry
   price of the 2nd distinct qualified wallet within 24 h. Revision 1 staked the 3rd,
   4th … entry as well, which made K=2 a measure of CROWDEDNESS, not of consensus, and
   weighted the busiest markets by up to 73 stakes each. The old behaviour survives
   below as the labelled "every qualified entry" diagnostic only.
4. **Win rate is ROI > 0**, so the hold and exit rules are measured the same way.
5. Only entry months 2026-02..2026-09 are in scope (the plan's `as_of` grid); 5,414 older
   positions have no walk-forward score and are excluded.

## The data as read

Snapshot at run time: 79,235 `positions_mat` rows, 1,075,256 prints,
76,159 resolutions, 88,433 markets.
Candidates (resolved market, entry month in scope, entry print found): **66,246**
— train 13,225, test 39,811, live era 13,210.

### Wallets, cutoffs and base rates per month (walk-forward)

| as_of | sports pop | scored wallets | sports base rate m0 | candidates | wallets | pass top50 | pass top20 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-02 | 51 | 53 | +0.0352 (n 633) | 731 | 36 | 25 | 7 |
| 2026-03 | 62 | 64 | -0.0200 (n 487) | 1436 | 48 | 13 | 3 |
| 2026-04 | 81 | 83 | -0.0072 (n 763) | 2008 | 65 | 19 | 6 |
| 2026-05 | 103 | 105 | -0.0263 (n 868) | 3348 | 100 | 25 | 11 |
| 2026-06 | 139 | 141 | -0.0363 (n 1481) | 3097 | 110 | 32 | 13 |
| 2026-07 | 167 | 169 | -0.0340 (n 1433) | 7064 | 185 | 54 | 21 |
| 2026-08 | 229 | 231 | -0.0062 (n 3265) | 26593 | 386 | 94 | 47 |
| 2026-09 | 399 | 399 | +0.0117 (n 13066) | 21969 | 1533 | 173 | 97 |


### Positions followed, per configuration

| config | train n | test n | live n |
| --- | --- | --- | --- |
| Q=all K=1 E=hold | 13225 | 39811 | 13210 |
| Q=all K=1 E=exit | 13225 | 39811 | 13210 |
| Q=all K=2 E=hold | 1627 | 5530 | 784 |
| Q=all K=2 E=exit | 1627 | 5530 | 784 |
| Q=top50 K=1 E=hold | 3758 | 10497 | 1416 |
| Q=top50 K=1 E=exit | 3758 | 10497 | 1416 |
| Q=top50 K=2 E=hold | 274 | 1560 | 180 |
| Q=top50 K=2 E=exit | 274 | 1560 | 180 |
| Q=top20 K=1 E=hold | 1014 | 4122 | 788 |
| Q=top20 K=1 E=exit | 1014 | 4122 | 788 |
| Q=top20 K=2 E=hold | 37 | 544 | 101 |
| Q=top20 K=2 E=exit | 37 | 544 | 101 |


### Per-sport base facts (all candidates, follow, hold, main slippage)

| sport | n | mean ROI/$ | outcome win rate |
| --- | --- | --- | --- |
| tennis | 26870 | -0.0252 | 0.585 |
| soccer | 17962 | -0.0062 | 0.646 |
| baseball_mlb | 13988 | -0.0031 | 0.549 |
| basketball_nba | 2417 | -0.0921 | 0.553 |
| basketball_wnba | 1550 | -0.0631 | 0.546 |
| mma | 1486 | +0.0537 | 0.601 |
| americanfootball_ncaaf | 479 | +0.1411 | 0.656 |
| icehockey_nhl | 447 | -0.0210 | 0.521 |
| americanfootball_nfl | 422 | -0.0139 | 0.550 |
| cricket | 394 | -0.0604 | 0.614 |
| baseball_kbo | 129 | -0.0181 | 0.605 |
| politics | 68 | +0.1353 | 0.897 |
| baseball_npb | 24 | +0.1400 | 0.750 |
| basketball_fiba_womens | 8 | +0.0320 | 0.875 |
| crypto | 2 | -1.0333 | 0.000 |


### Where the prints come from (a plain fact, not one of the plan's diagnostics)

`source` is the print that OPENED the position: `activity` = the cold-start /activity
backfill of discovered wallets, `tape` = the global real-time collector, which only
started 2026-09-04. Limitation 1 has a calendar shape, and this is it.

| source | split | n | mean ROI/$ (follow, hold) |
| --- | --- | --- | --- |
| activity | train | 13225 | -0.0549 |
| activity | test | 39811 | +0.0110 |
| activity | live | 3121 | -0.0280 |
| tape | train | 0 | — |
| tape | test | 0 | — |
| tape | live | 10089 | -0.0653 |


## Sample construction (new in Revision 2)

What fell out between `positions_mat` and the candidate set. Revision 1 printed
only the survivors, so the reader could not see the censoring.

| era | positions_mat rows | dropped: no resolved row | = share | dropped: no entry print | candidates | extra stakes: split BUY windows | = share |
| --- | --- | --- | --- | --- | --- | --- | --- |
| train | 18,951 | 452 | 2.4% | 0 | 13,225 | 2,602 | 19.7% |
| test | 40,485 | 674 | 1.7% | 0 | 39,811 | 8,662 | 21.8% |
| live | 19,799 | 6,589 | 33.3% | 0 | 13,210 | 4,633 | 35.1% |


- **Right-censoring is the live era's story.** A position whose market has not resolved yet has no `resolutions` row; 6,589 of the 19,799 live-era positions (33.3%) are in that state,
  against 2.4% on train and 1.7% on test. The live era's mean is
  therefore computed on the subset that resolved FASTEST, which is not a random
  subset of it.
- **Extra stakes.** One wallet + market + outcome can produce several positions when
  its buying splits across the position window, and each split window is its own $1
  stake. The share of candidate rows that are a 2nd-or-later window for the same
  (wallet, market, outcome) is 19.7% train / 21.8% test / 35.1% live — so the eras are not equally
  weighted per wallet-market either.

- **The 2026-02 grid start drops 5,274 positions**, all train-era, whose
  mean ROI/$ is +0.0511 — they are the profitable tail the walk-forward
  `as_of` grid cannot score. Folding them into train anyway (an illustration, not a
  configuration) moves the baseline train mean from -0.0549 to -0.0247:
  a large move, and the sign does not change.

### Source mix and wallet count per era

| era | source | n | share | distinct wallets |
| --- | --- | --- | --- | --- |
| train | activity | 13,225 | 100.0% | 204 |
| test | activity | 39,811 | 100.0% | 443 |
| live | activity | 3,121 | 23.6% | 255 |
| live | tape | 10,089 | 76.4% | 1,376 |


`activity` = the cold-start backfill of DISCOVERED wallets (see limitation 1);
`tape` = the global real-time collector, live only from 2026-09-04. Train and test
are one source; the live era is mostly the other, over a different and much wider
wallet population — the three eras are not three samples of one process.

### What the bootstrap actually clusters on

`markets.event_key` is empty for 87,746 of 88,433 rows, so the plan's
"event cluster" is a **condition_id cluster** almost everywhere: the two sides of
one game, and correlated markets on one event, are resampled as if independent.
Every lb80 / ub90 / Bonferroni bound in this report is therefore a
**condition_id-clustered** bound on train and test and a MIXED one in the live era,
and is anti-conservative to the extent that markets within an event move together.

| era | rows clustered by event_key | rows clustered by condition_id |
| --- | --- | --- |
| train | 0 | 13,225 |
| test | 23 | 39,788 |
| live | 8,249 | 4,961 |


## The 12 configurations

Main slippage 0.01. lb80 / ub90 / Bonferroni-12 LB are **condition_id-clustered**
bootstrap quantiles (the plan said "event cluster"; `markets.event_key` is empty
almost everywhere — see Sample construction) of (0.20 / 0.90 / 0.0167) over 2000 resamples, seed 20260905. `K=2` is one stake per (market, outcome) at the 2nd distinct
qualified wallet within 24 h — the plan's rule, corrected in Revision 2.

### train

| config | n | n_events | mean ROI/$ | win rate (ROI>0) | lb80 | ub90 | Bonf-12 LB |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Q=all K=1 E=hold | 13225 | 6566 | -0.0549 | 0.540 | -0.0657 | -0.0388 | -0.0822 |
| Q=all K=1 E=exit | 13225 | 6566 | -0.0574 | 0.524 | -0.0675 | -0.0417 | -0.0828 |
| Q=all K=2 E=hold | 1627 | 1385 | -0.0457 | 0.549 | -0.0656 | -0.0174 | -0.0933 |
| Q=all K=2 E=exit | 1627 | 1385 | -0.0491 | 0.527 | -0.0678 | -0.0189 | -0.0958 |
| Q=top50 K=1 E=hold | 3758 | 2353 | -0.0573 | 0.525 | -0.0763 | -0.0258 | -0.1081 |
| Q=top50 K=1 E=exit | 3758 | 2353 | -0.0602 | 0.508 | -0.0811 | -0.0311 | -0.1087 |
| Q=top50 K=2 E=hold | 274 | 246 | -0.0970 | 0.478 | -0.1498 | -0.0205 | -0.2237 |
| Q=top50 K=2 E=exit | 274 | 246 | -0.0850 | 0.471 | -0.1348 | -0.0074 | -0.2195 |
| Q=top20 K=1 E=hold | 1014 | 734 | -0.0818 | 0.523 | -0.1207 | -0.0210 | -0.1764 |
| Q=top20 K=1 E=exit | 1014 | 734 | -0.0809 | 0.513 | -0.1213 | -0.0258 | -0.1804 |
| Q=top20 K=2 E=hold | 37 | 35 | -0.2546 | 0.351 | -0.4221 | -0.0057 | -0.6286 |
| Q=top20 K=2 E=exit | 37 | 35 | -0.3064 | 0.297 | -0.4610 | -0.0592 | -0.6687 |


### test

| config | n | n_events | mean ROI/$ | win rate (ROI>0) | lb80 | ub90 | Bonf-12 LB |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Q=all K=1 E=hold | 39811 | 10933 | +0.0110 | 0.580 | +0.0043 | +0.0210 | -0.0052 |
| Q=all K=1 E=exit | 39811 | 10933 | +0.0013 | 0.568 | -0.0053 | +0.0112 | -0.0142 |
| Q=all K=2 E=hold | 5530 | 4056 | -0.0087 | 0.572 | -0.0166 | +0.0037 | -0.0292 |
| Q=all K=2 E=exit | 5530 | 4056 | -0.0175 | 0.559 | -0.0256 | -0.0054 | -0.0386 |
| Q=top50 K=1 E=hold | 10497 | 4285 | -0.0041 | 0.551 | -0.0175 | +0.0156 | -0.0368 |
| Q=top50 K=1 E=exit | 10497 | 4285 | -0.0184 | 0.544 | -0.0300 | +0.0009 | -0.0484 |
| Q=top50 K=2 E=hold | 1560 | 1266 | -0.0285 | 0.536 | -0.0462 | -0.0002 | -0.0719 |
| Q=top50 K=2 E=exit | 1560 | 1266 | -0.0355 | 0.532 | -0.0549 | -0.0086 | -0.0810 |
| Q=top20 K=1 E=hold | 4122 | 2079 | +0.0264 | 0.567 | +0.0050 | +0.0584 | -0.0226 |
| Q=top20 K=1 E=exit | 4122 | 2079 | +0.0081 | 0.554 | -0.0126 | +0.0385 | -0.0402 |
| Q=top20 K=2 E=hold | 544 | 463 | +0.0402 | 0.548 | +0.0036 | +0.0913 | -0.0507 |
| Q=top20 K=2 E=exit | 544 | 463 | +0.0155 | 0.539 | -0.0201 | +0.0693 | -0.0634 |


### live

| config | n | n_events | mean ROI/$ | win rate (ROI>0) | lb80 | ub90 | Bonf-12 LB |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Q=all K=1 E=hold | 13210 | 946 | -0.0565 | 0.555 | -0.0685 | -0.0370 | -0.0857 |
| Q=all K=1 E=exit | 13210 | 946 | -0.0580 | 0.541 | -0.0695 | -0.0391 | -0.0867 |
| Q=all K=2 E=hold | 784 | 467 | -0.0465 | 0.542 | -0.0648 | -0.0149 | -0.0948 |
| Q=all K=2 E=exit | 784 | 467 | -0.0444 | 0.533 | -0.0649 | -0.0146 | -0.0946 |
| Q=top50 K=1 E=hold | 1416 | 293 | -0.0896 | 0.532 | -0.1266 | -0.0308 | -0.1846 |
| Q=top50 K=1 E=exit | 1416 | 293 | -0.0891 | 0.523 | -0.1255 | -0.0318 | -0.1875 |
| Q=top50 K=2 E=hold | 180 | 112 | +0.0399 | 0.567 | -0.0168 | +0.1277 | -0.0934 |
| Q=top50 K=2 E=exit | 180 | 112 | +0.0381 | 0.556 | -0.0200 | +0.1281 | -0.0992 |
| Q=top20 K=1 E=hold | 788 | 233 | -0.1509 | 0.503 | -0.1975 | -0.0663 | -0.2794 |
| Q=top20 K=1 E=exit | 788 | 233 | -0.1462 | 0.501 | -0.1986 | -0.0640 | -0.2805 |
| Q=top20 K=2 E=hold | 101 | 74 | -0.0932 | 0.495 | -0.1638 | +0.0202 | -0.2697 |
| Q=top20 K=2 E=exit | 101 | 74 | -0.0677 | 0.495 | -0.1472 | +0.0515 | -0.2465 |


### Verdict (the plan's rule: TEST Bonferroni-12 LB > 0 AND the train sign agrees)

**No configuration is FAVORABLE.** Every one of the 12 fails the rule — the test
Bonferroni-12 lower bound is not above zero, or the train sign disagrees.

Near misses, reported and NOT promoted: test Bonferroni-12 LB > 0 but the train sign
disagrees — none. Test lb80 > 0 (the un-corrected
80% bound, which the plan does not accept as a verdict) — Q=all K=1 E=hold, Q=top20 K=1 E=hold, Q=top20 K=2 E=hold.

### Diagnostic: the withdrawn K=2 rule ("every qualified entry")

Revision 1's K=2 staked the 3rd, 4th … qualified wallet as well. Its worst single
(market, outcome) carried 27 stakes on train, 74 on test and 248 in the live era,
so its mean was a crowdedness-weighted average of a few busy markets. Reported here
for continuity with Revision 1; it is NOT one of the 12 configurations and it enters
no verdict.

| config | train old (n) | train plan (n) | test old (n) | test plan (n) | live old (n) | live plan (n) |
| --- | --- | --- | --- | --- | --- | --- |
| Q=all K=2 E=hold | -0.0608 (3,379) | -0.0457 (1,627) | +0.0276 (22,160) | -0.0087 (5,530) | -0.0566 (11,566) | -0.0465 (784) |
| Q=all K=2 E=exit | -0.0703 (3,379) | -0.0491 (1,627) | +0.0163 (22,160) | -0.0175 (5,530) | -0.0582 (11,566) | -0.0444 (784) |
| Q=top50 K=2 E=hold | -0.0715 (460) | -0.0970 (274) | +0.0384 (3,974) | -0.0285 (1,560) | -0.1146 (865) | +0.0399 (180) |
| Q=top50 K=2 E=exit | -0.0654 (460) | -0.0850 (274) | +0.0190 (3,974) | -0.0355 (1,560) | -0.1137 (865) | +0.0381 (180) |
| Q=top20 K=2 E=hold | -0.1919 (56) | -0.2546 (37) | +0.0948 (1,121) | +0.0402 (544) | -0.3370 (342) | -0.0932 (101) |
| Q=top20 K=2 E=exit | -0.2283 (56) | -0.3064 (37) | +0.0643 (1,121) | +0.0155 (544) | -0.3292 (342) | -0.0677 (101) |


## Sensitivity to slippage (mean ROI/$, never used to pick)

### slippage 0.00

| config | train | test | live |
| --- | --- | --- | --- |
| Q=all K=1 E=hold | -0.0350 | +0.0319 | -0.0371 |
| Q=all K=1 E=exit | -0.0332 | +0.0247 | -0.0364 |
| Q=all K=2 E=hold | -0.0251 | +0.0115 | -0.0270 |
| Q=all K=2 E=exit | -0.0229 | +0.0057 | -0.0231 |
| Q=top50 K=1 E=hold | -0.0368 | +0.0176 | -0.0699 |
| Q=top50 K=1 E=exit | -0.0348 | +0.0059 | -0.0681 |
| Q=top50 K=2 E=hold | -0.0766 | -0.0078 | +0.0647 |
| Q=top50 K=2 E=exit | -0.0581 | -0.0118 | +0.0645 |
| Q=top20 K=1 E=hold | -0.0634 | +0.0494 | -0.1330 |
| Q=top20 K=1 E=exit | -0.0565 | +0.0327 | -0.1273 |
| Q=top20 K=2 E=hold | -0.2350 | +0.0648 | -0.0720 |
| Q=top20 K=2 E=exit | -0.2831 | +0.0413 | -0.0440 |


### slippage 0.02

| config | train | test | live |
| --- | --- | --- | --- |
| Q=all K=1 E=hold | -0.0736 | -0.0088 | -0.0746 |
| Q=all K=1 E=exit | -0.0803 | -0.0208 | -0.0782 |
| Q=all K=2 E=hold | -0.0650 | -0.0279 | -0.0650 |
| Q=all K=2 E=exit | -0.0737 | -0.0395 | -0.0645 |
| Q=top50 K=1 E=hold | -0.0766 | -0.0246 | -0.1081 |
| Q=top50 K=1 E=exit | -0.0841 | -0.0413 | -0.1090 |
| Q=top50 K=2 E=hold | -0.1164 | -0.0481 | +0.0166 |
| Q=top50 K=2 E=exit | -0.1105 | -0.0581 | +0.0135 |
| Q=top20 K=1 E=hold | -0.0995 | +0.0047 | -0.1677 |
| Q=top20 K=1 E=exit | -0.1042 | -0.0151 | -0.1640 |
| Q=top20 K=2 E=hold | -0.2731 | +0.0172 | -0.1133 |
| Q=top20 K=2 E=exit | -0.3283 | -0.0087 | -0.0900 |


## Monthly walk-forward (mean ROI/$ by entry month, main slippage)

| config | 2026-02 | 2026-03 | 2026-04 | 2026-05 | 2026-06 | 2026-07 | 2026-08 | 2026-09 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Q=all K=1 E=hold | +0.085 (731) | -0.020 (1436) | -0.045 (2008) | -0.058 (3348) | -0.085 (3097) | -0.056 (7064) | +0.000 (26593) | -0.006 (21969) |
| Q=all K=1 E=exit | +0.081 (731) | -0.025 (1436) | -0.040 (2008) | -0.065 (3348) | -0.087 (3097) | -0.065 (7064) | -0.009 (26593) | -0.011 (21969) |
| Q=all K=2 E=hold | +0.058 (57) | -0.009 (143) | -0.014 (173) | -0.010 (411) | -0.067 (462) | -0.044 (1092) | -0.014 (3922) | -0.013 (1681) |
| Q=all K=2 E=exit | +0.138 (57) | -0.013 (143) | +0.001 (173) | -0.036 (411) | -0.060 (462) | -0.058 (1092) | -0.022 (3922) | -0.016 (1681) |
| Q=top50 K=1 E=hold | +0.271 (341) | -0.049 (400) | +0.010 (478) | -0.055 (645) | -0.146 (809) | -0.095 (2326) | -0.024 (6299) | +0.015 (4373) |
| Q=top50 K=1 E=exit | +0.261 (341) | -0.048 (400) | -0.008 (478) | -0.071 (645) | -0.142 (809) | -0.094 (2326) | -0.040 (6299) | +0.004 (4373) |
| Q=top50 K=2 E=hold | -0.012 (10) | -0.341 (12) | +0.209 (16) | -0.140 (39) | +0.046 (58) | -0.116 (271) | -0.047 (1036) | +0.034 (572) |
| Q=top50 K=2 E=exit | +0.057 (10) | -0.344 (12) | +0.205 (16) | -0.128 (39) | +0.047 (58) | -0.122 (271) | -0.051 (1036) | +0.029 (572) |
| Q=top20 K=1 E=hold | +0.041 (98) | -0.005 (82) | +0.086 (79) | -0.208 (63) | -0.155 (305) | -0.067 (721) | +0.004 (2186) | -0.001 (2390) |
| Q=top20 K=1 E=exit | +0.046 (98) | -0.008 (82) | +0.039 (79) | -0.213 (63) | -0.133 (305) | -0.078 (721) | -0.015 (2186) | -0.012 (2390) |
| Q=top20 K=2 E=hold | — | — | +0.073 (1) | -0.413 (5) | -0.354 (9) | -0.171 (43) | -0.007 (313) | +0.057 (311) |
| Q=top20 K=2 E=exit | — | — | +0.061 (1) | -0.413 (5) | -0.478 (9) | -0.193 (43) | -0.024 (313) | +0.039 (311) |


## Diagnostics (per configuration; NEVER used to choose)

Each cell is `mean ROI/$ (n)` at the main slippage. `fade` buys the opposite outcome
at `1 − price + slippage` and holds to resolution (the exit rule does not apply to
a side the whale never traded).

### Q=all K=1 E=hold

| diagnostic | split | level | mean ROI/$ (n) |
| --- | --- | --- | --- |
| follow/fade | train | follow | -0.0549 (13225) |
| follow/fade | train | fade | -0.0284 (13225) |
| follow/fade | test | follow | +0.0110 (39811) |
| follow/fade | test | fade | -0.0944 (39811) |
| price band | train | 0.20-0.50 | -0.0587 (4453) |
| price band | train | 0.50-0.80 | -0.0490 (6423) |
| price band | train | <0.20 | -0.1940 (408) |
| price band | train | >0.80 | -0.0362 (1941) |
| price band | test | 0.20-0.50 | +0.0444 (13102) |
| price band | test | 0.50-0.80 | +0.0041 (17790) |
| price band | test | <0.20 | -0.0996 (1106) |
| price band | test | >0.80 | -0.0138 (7813) |
| sport | train | americanfootball_nfl | +0.3239 (13) |
| sport | train | baseball_kbo | -0.5744 (4) |
| sport | train | baseball_mlb | -0.0513 (1972) |
| sport | train | baseball_npb | -1.0150 (1) |
| sport | train | basketball_nba | -0.0913 (2396) |
| sport | train | basketball_wnba | -0.1017 (525) |
| sport | train | cricket | -0.0178 (93) |
| sport | train | crypto | -1.0333 (2) |
| sport | train | icehockey_nhl | -0.0210 (447) |
| sport | train | mma | +0.1641 (631) |
| sport | train | politics | -0.0814 (21) |
| sport | train | soccer | +0.0146 (1826) |
| sport | train | tennis | -0.0885 (5294) |
| sport | test | americanfootball_ncaaf | +0.1793 (171) |
| sport | test | americanfootball_nfl | -0.0246 (409) |
| sport | test | baseball_kbo | +0.0884 (64) |
| sport | test | baseball_mlb | +0.0251 (9134) |
| sport | test | baseball_npb | +0.2322 (16) |
| sport | test | basketball_fiba_womens | +0.0128 (3) |
| sport | test | basketball_nba | -0.1796 (21) |
| sport | test | basketball_wnba | -0.0433 (1025) |
| sport | test | cricket | -0.0584 (233) |
| sport | test | mma | -0.0277 (855) |
| sport | test | politics | +0.2321 (47) |
| sport | test | soccer | +0.0082 (12649) |
| sport | test | tennis | +0.0097 (15184) |
| timing | train | in-play | -0.0547 (6931) |
| timing | train | pregame | -0.0551 (6294) |
| timing | test | in-play | +0.0098 (19524) |
| timing | test | pregame | +0.0121 (20287) |
| notional decile | train | D0 | -0.0071 (1196) |
| notional decile | train | D1 | -0.1058 (1251) |
| notional decile | train | D2 | -0.0983 (1244) |
| notional decile | train | D3 | -0.0782 (1487) |
| notional decile | train | D4 | -0.0545 (1211) |
| notional decile | train | D5 | -0.0687 (1377) |
| notional decile | train | D6 | -0.0431 (1068) |
| notional decile | train | D7 | -0.0365 (1427) |
| notional decile | train | D8 | -0.0395 (1533) |
| notional decile | train | D9 | -0.0190 (1431) |
| notional decile | test | D0 | -0.0017 (4009) |
| notional decile | test | D1 | +0.0176 (4004) |
| notional decile | test | D2 | +0.0072 (4092) |
| notional decile | test | D3 | +0.0066 (4071) |
| notional decile | test | D4 | +0.0085 (4140) |
| notional decile | test | D5 | +0.0091 (3991) |
| notional decile | test | D6 | -0.0056 (3872) |
| notional decile | test | D7 | +0.0104 (3665) |
| notional decile | test | D8 | +0.0165 (3758) |
| notional decile | test | D9 | +0.0396 (4209) |


### Q=all K=1 E=exit

| diagnostic | split | level | mean ROI/$ (n) |
| --- | --- | --- | --- |
| follow/fade | train | follow | -0.0574 (13225) |
| follow/fade | train | fade | -0.0284 (13225) |
| follow/fade | test | follow | +0.0013 (39811) |
| follow/fade | test | fade | -0.0944 (39811) |
| price band | train | 0.20-0.50 | -0.0602 (4453) |
| price band | train | 0.50-0.80 | -0.0543 (6423) |
| price band | train | <0.20 | -0.1539 (408) |
| price band | train | >0.80 | -0.0410 (1941) |
| price band | test | 0.20-0.50 | +0.0241 (13102) |
| price band | test | 0.50-0.80 | -0.0013 (17790) |
| price band | test | <0.20 | -0.1095 (1106) |
| price band | test | >0.80 | -0.0154 (7813) |
| sport | train | americanfootball_nfl | +0.3239 (13) |
| sport | train | baseball_kbo | -0.5744 (4) |
| sport | train | baseball_mlb | -0.0485 (1972) |
| sport | train | baseball_npb | -1.0150 (1) |
| sport | train | basketball_nba | -0.0883 (2396) |
| sport | train | basketball_wnba | -0.1224 (525) |
| sport | train | cricket | +0.0096 (93) |
| sport | train | crypto | -1.0333 (2) |
| sport | train | icehockey_nhl | -0.0293 (447) |
| sport | train | mma | +0.1459 (631) |
| sport | train | politics | -0.0377 (21) |
| sport | train | soccer | +0.0074 (1826) |
| sport | train | tennis | -0.0905 (5294) |
| sport | test | americanfootball_ncaaf | +0.1819 (171) |
| sport | test | americanfootball_nfl | -0.0044 (409) |
| sport | test | baseball_kbo | +0.0726 (64) |
| sport | test | baseball_mlb | +0.0204 (9134) |
| sport | test | baseball_npb | +0.2268 (16) |
| sport | test | basketball_fiba_womens | +0.0128 (3) |
| sport | test | basketball_nba | -0.0998 (21) |
| sport | test | basketball_wnba | -0.0402 (1025) |
| sport | test | cricket | -0.0402 (233) |
| sport | test | mma | -0.0254 (855) |
| sport | test | politics | +0.1718 (47) |
| sport | test | soccer | +0.0001 (12649) |
| sport | test | tennis | -0.0070 (15184) |
| timing | train | in-play | -0.0616 (6931) |
| timing | train | pregame | -0.0528 (6294) |
| timing | test | in-play | -0.0044 (19524) |
| timing | test | pregame | +0.0068 (20287) |
| notional decile | train | D0 | -0.0058 (1196) |
| notional decile | train | D1 | -0.1132 (1251) |
| notional decile | train | D2 | -0.0970 (1244) |
| notional decile | train | D3 | -0.0811 (1487) |
| notional decile | train | D4 | -0.0676 (1211) |
| notional decile | train | D5 | -0.0627 (1377) |
| notional decile | train | D6 | -0.0525 (1068) |
| notional decile | train | D7 | -0.0377 (1427) |
| notional decile | train | D8 | -0.0358 (1533) |
| notional decile | train | D9 | -0.0254 (1431) |
| notional decile | test | D0 | -0.0046 (4009) |
| notional decile | test | D1 | +0.0092 (4004) |
| notional decile | test | D2 | -0.0086 (4092) |
| notional decile | test | D3 | +0.0025 (4071) |
| notional decile | test | D4 | +0.0045 (4140) |
| notional decile | test | D5 | +0.0060 (3991) |
| notional decile | test | D6 | -0.0184 (3872) |
| notional decile | test | D7 | +0.0032 (3665) |
| notional decile | test | D8 | +0.0044 (3758) |
| notional decile | test | D9 | +0.0141 (4209) |


### Q=all K=2 E=hold

| diagnostic | split | level | mean ROI/$ (n) |
| --- | --- | --- | --- |
| follow/fade | train | follow | -0.0457 (1627) |
| follow/fade | train | fade | -0.0221 (1627) |
| follow/fade | test | follow | -0.0087 (5530) |
| follow/fade | test | fade | -0.0972 (5530) |
| price band | train | 0.20-0.50 | -0.0352 (564) |
| price band | train | 0.50-0.80 | -0.0641 (744) |
| price band | train | <0.20 | +0.0402 (41) |
| price band | train | >0.80 | -0.0306 (278) |
| price band | test | 0.20-0.50 | -0.0047 (1835) |
| price band | test | 0.50-0.80 | +0.0023 (2564) |
| price band | test | <0.20 | -0.2279 (150) |
| price band | test | >0.80 | -0.0117 (981) |
| sport | train | americanfootball_nfl | +0.4400 (1) |
| sport | train | baseball_mlb | -0.0952 (264) |
| sport | train | basketball_nba | -0.0164 (279) |
| sport | train | basketball_wnba | -0.1660 (52) |
| sport | train | cricket | -0.0435 (11) |
| sport | train | icehockey_nhl | -0.0613 (41) |
| sport | train | mma | +0.1055 (67) |
| sport | train | soccer | +0.0714 (144) |
| sport | train | tennis | -0.0662 (768) |
| sport | test | americanfootball_ncaaf | +0.4334 (20) |
| sport | test | americanfootball_nfl | -0.0524 (61) |
| sport | test | baseball_kbo | -0.2426 (6) |
| sport | test | baseball_mlb | -0.0310 (1356) |
| sport | test | basketball_fiba_womens | +0.0098 (1) |
| sport | test | basketball_nba | -0.5159 (3) |
| sport | test | basketball_wnba | -0.0171 (162) |
| sport | test | cricket | -0.0634 (21) |
| sport | test | mma | -0.0087 (126) |
| sport | test | soccer | +0.0202 (1683) |
| sport | test | tennis | -0.0180 (2091) |
| timing | train | in-play | -0.0507 (1140) |
| timing | train | pregame | -0.0339 (487) |
| timing | test | in-play | -0.0192 (2745) |
| timing | test | pregame | +0.0016 (2785) |
| notional decile | train | D0 | -0.0827 (137) |
| notional decile | train | D1 | +0.0159 (134) |
| notional decile | train | D2 | -0.1241 (141) |
| notional decile | train | D3 | -0.0223 (177) |
| notional decile | train | D4 | -0.0628 (160) |
| notional decile | train | D5 | -0.0046 (204) |
| notional decile | train | D6 | +0.0006 (126) |
| notional decile | train | D7 | -0.0268 (184) |
| notional decile | train | D8 | -0.1282 (185) |
| notional decile | train | D9 | -0.0234 (179) |
| notional decile | test | D0 | -0.0423 (529) |
| notional decile | test | D1 | +0.0529 (511) |
| notional decile | test | D2 | +0.0140 (561) |
| notional decile | test | D3 | -0.0163 (578) |
| notional decile | test | D4 | +0.0296 (566) |
| notional decile | test | D5 | -0.0155 (596) |
| notional decile | test | D6 | -0.0586 (580) |
| notional decile | test | D7 | -0.0669 (514) |
| notional decile | test | D8 | -0.0143 (478) |
| notional decile | test | D9 | +0.0264 (617) |


### Q=all K=2 E=exit

| diagnostic | split | level | mean ROI/$ (n) |
| --- | --- | --- | --- |
| follow/fade | train | follow | -0.0491 (1627) |
| follow/fade | train | fade | -0.0221 (1627) |
| follow/fade | test | follow | -0.0175 (5530) |
| follow/fade | test | fade | -0.0972 (5530) |
| price band | train | 0.20-0.50 | -0.0275 (564) |
| price band | train | 0.50-0.80 | -0.0825 (744) |
| price band | train | <0.20 | +0.1777 (41) |
| price band | train | >0.80 | -0.0369 (278) |
| price band | test | 0.20-0.50 | -0.0209 (1835) |
| price band | test | 0.50-0.80 | -0.0072 (2564) |
| price band | test | <0.20 | -0.1996 (150) |
| price band | test | >0.80 | -0.0101 (981) |
| sport | train | americanfootball_nfl | +0.4400 (1) |
| sport | train | baseball_mlb | -0.0805 (264) |
| sport | train | basketball_nba | +0.0041 (279) |
| sport | train | basketball_wnba | -0.1665 (52) |
| sport | train | cricket | +0.1161 (11) |
| sport | train | icehockey_nhl | -0.0663 (41) |
| sport | train | mma | +0.0903 (67) |
| sport | train | soccer | +0.0461 (144) |
| sport | train | tennis | -0.0817 (768) |
| sport | test | americanfootball_ncaaf | +0.4326 (20) |
| sport | test | americanfootball_nfl | -0.0438 (61) |
| sport | test | baseball_kbo | -0.2458 (6) |
| sport | test | baseball_mlb | -0.0374 (1356) |
| sport | test | basketball_fiba_womens | +0.0098 (1) |
| sport | test | basketball_nba | -0.5159 (3) |
| sport | test | basketball_wnba | +0.0003 (162) |
| sport | test | cricket | +0.0860 (21) |
| sport | test | mma | +0.0203 (126) |
| sport | test | soccer | +0.0113 (1683) |
| sport | test | tennis | -0.0347 (2091) |
| timing | train | in-play | -0.0513 (1140) |
| timing | train | pregame | -0.0439 (487) |
| timing | test | in-play | -0.0313 (2745) |
| timing | test | pregame | -0.0039 (2785) |
| notional decile | train | D0 | -0.0693 (137) |
| notional decile | train | D1 | +0.0375 (134) |
| notional decile | train | D2 | -0.1279 (141) |
| notional decile | train | D3 | -0.0446 (177) |
| notional decile | train | D4 | -0.0547 (160) |
| notional decile | train | D5 | -0.0091 (204) |
| notional decile | train | D6 | -0.0408 (126) |
| notional decile | train | D7 | -0.0146 (184) |
| notional decile | train | D8 | -0.1316 (185) |
| notional decile | train | D9 | -0.0371 (179) |
| notional decile | test | D0 | -0.0619 (529) |
| notional decile | test | D1 | +0.0598 (511) |
| notional decile | test | D2 | +0.0035 (561) |
| notional decile | test | D3 | -0.0114 (578) |
| notional decile | test | D4 | +0.0145 (566) |
| notional decile | test | D5 | -0.0180 (596) |
| notional decile | test | D6 | -0.0789 (580) |
| notional decile | test | D7 | -0.0614 (514) |
| notional decile | test | D8 | -0.0284 (478) |
| notional decile | test | D9 | +0.0056 (617) |


### Q=top50 K=1 E=hold

| diagnostic | split | level | mean ROI/$ (n) |
| --- | --- | --- | --- |
| follow/fade | train | follow | -0.0573 (3758) |
| follow/fade | train | fade | -0.0683 (3758) |
| follow/fade | test | follow | -0.0041 (10497) |
| follow/fade | test | fade | -0.0876 (10497) |
| price band | train | 0.20-0.50 | -0.0577 (1470) |
| price band | train | 0.50-0.80 | -0.0318 (1765) |
| price band | train | <0.20 | -0.5226 (133) |
| price band | train | >0.80 | -0.0125 (390) |
| price band | test | 0.20-0.50 | +0.0252 (4015) |
| price band | test | 0.50-0.80 | -0.0115 (5045) |
| price band | test | <0.20 | -0.2612 (323) |
| price band | test | >0.80 | -0.0018 (1114) |
| sport | train | americanfootball_nfl | +0.4591 (10) |
| sport | train | baseball_mlb | -0.1267 (576) |
| sport | train | baseball_npb | -1.0150 (1) |
| sport | train | basketball_nba | -0.1076 (572) |
| sport | train | basketball_wnba | -0.1888 (246) |
| sport | train | cricket | +0.1817 (13) |
| sport | train | crypto | -1.0333 (2) |
| sport | train | icehockey_nhl | +0.0180 (178) |
| sport | train | mma | +0.1989 (332) |
| sport | train | soccer | +0.1101 (386) |
| sport | train | tennis | -0.1041 (1442) |
| sport | test | americanfootball_ncaaf | +0.0761 (17) |
| sport | test | americanfootball_nfl | +0.0820 (21) |
| sport | test | baseball_kbo | +0.2538 (23) |
| sport | test | baseball_mlb | +0.0101 (2805) |
| sport | test | baseball_npb | -0.2114 (5) |
| sport | test | basketball_nba | -0.1848 (6) |
| sport | test | basketball_wnba | -0.0893 (279) |
| sport | test | cricket | +0.0997 (14) |
| sport | test | mma | -0.0619 (157) |
| sport | test | politics | +0.3511 (22) |
| sport | test | soccer | -0.0267 (1880) |
| sport | test | tennis | -0.0005 (5268) |
| timing | train | in-play | -0.0733 (1994) |
| timing | train | pregame | -0.0392 (1764) |
| timing | test | in-play | -0.0149 (4685) |
| timing | test | pregame | +0.0046 (5812) |
| notional decile | train | D0 | +0.1271 (262) |
| notional decile | train | D1 | -0.1117 (342) |
| notional decile | train | D2 | -0.0911 (320) |
| notional decile | train | D3 | -0.0619 (491) |
| notional decile | train | D4 | -0.0336 (333) |
| notional decile | train | D5 | -0.1444 (456) |
| notional decile | train | D6 | -0.0973 (320) |
| notional decile | train | D7 | -0.0558 (479) |
| notional decile | train | D8 | -0.0867 (395) |
| notional decile | train | D9 | +0.0503 (360) |
| notional decile | test | D0 | +0.0022 (869) |
| notional decile | test | D1 | -0.0508 (935) |
| notional decile | test | D2 | -0.0187 (982) |
| notional decile | test | D3 | +0.0032 (1191) |
| notional decile | test | D4 | +0.0195 (1186) |
| notional decile | test | D5 | +0.0052 (1204) |
| notional decile | test | D6 | -0.0511 (1040) |
| notional decile | test | D7 | +0.0143 (951) |
| notional decile | test | D8 | +0.0282 (955) |
| notional decile | test | D9 | -0.0001 (1184) |


### Q=top50 K=1 E=exit

| diagnostic | split | level | mean ROI/$ (n) |
| --- | --- | --- | --- |
| follow/fade | train | follow | -0.0602 (3758) |
| follow/fade | train | fade | -0.0683 (3758) |
| follow/fade | test | follow | -0.0184 (10497) |
| follow/fade | test | fade | -0.0876 (10497) |
| price band | train | 0.20-0.50 | -0.0543 (1470) |
| price band | train | 0.50-0.80 | -0.0431 (1765) |
| price band | train | <0.20 | -0.4588 (133) |
| price band | train | >0.80 | -0.0240 (390) |
| price band | test | 0.20-0.50 | -0.0084 (4015) |
| price band | test | 0.50-0.80 | -0.0125 (5045) |
| price band | test | <0.20 | -0.2742 (323) |
| price band | test | >0.80 | -0.0068 (1114) |
| sport | train | americanfootball_nfl | +0.4591 (10) |
| sport | train | baseball_mlb | -0.1157 (576) |
| sport | train | baseball_npb | -1.0150 (1) |
| sport | train | basketball_nba | -0.1085 (572) |
| sport | train | basketball_wnba | -0.1923 (246) |
| sport | train | cricket | +0.1817 (13) |
| sport | train | crypto | -1.0333 (2) |
| sport | train | icehockey_nhl | +0.0122 (178) |
| sport | train | mma | +0.1783 (332) |
| sport | train | soccer | +0.0953 (386) |
| sport | train | tennis | -0.1056 (1442) |
| sport | test | americanfootball_ncaaf | +0.0713 (17) |
| sport | test | americanfootball_nfl | +0.1045 (21) |
| sport | test | baseball_kbo | +0.2503 (23) |
| sport | test | baseball_mlb | +0.0098 (2805) |
| sport | test | baseball_npb | -0.2205 (5) |
| sport | test | basketball_nba | -0.0301 (6) |
| sport | test | basketball_wnba | -0.0843 (279) |
| sport | test | cricket | +0.0997 (14) |
| sport | test | mma | -0.0560 (157) |
| sport | test | politics | +0.3061 (22) |
| sport | test | soccer | -0.0247 (1880) |
| sport | test | tennis | -0.0300 (5268) |
| timing | train | in-play | -0.0805 (1994) |
| timing | train | pregame | -0.0372 (1764) |
| timing | test | in-play | -0.0369 (4685) |
| timing | test | pregame | -0.0034 (5812) |
| notional decile | train | D0 | +0.1069 (262) |
| notional decile | train | D1 | -0.1206 (342) |
| notional decile | train | D2 | -0.0865 (320) |
| notional decile | train | D3 | -0.0754 (491) |
| notional decile | train | D4 | -0.0548 (333) |
| notional decile | train | D5 | -0.1141 (456) |
| notional decile | train | D6 | -0.1058 (320) |
| notional decile | train | D7 | -0.0467 (479) |
| notional decile | train | D8 | -0.0946 (395) |
| notional decile | train | D9 | +0.0434 (360) |
| notional decile | test | D0 | +0.0080 (869) |
| notional decile | test | D1 | -0.0498 (935) |
| notional decile | test | D2 | -0.0223 (982) |
| notional decile | test | D3 | -0.0076 (1191) |
| notional decile | test | D4 | +0.0094 (1186) |
| notional decile | test | D5 | -0.0066 (1204) |
| notional decile | test | D6 | -0.0712 (1040) |
| notional decile | test | D7 | -0.0087 (951) |
| notional decile | test | D8 | +0.0073 (955) |
| notional decile | test | D9 | -0.0424 (1184) |


### Q=top50 K=2 E=hold

| diagnostic | split | level | mean ROI/$ (n) |
| --- | --- | --- | --- |
| follow/fade | train | follow | -0.0970 (274) |
| follow/fade | train | fade | +0.0474 (274) |
| follow/fade | test | follow | -0.0285 (1560) |
| follow/fade | test | fade | -0.0632 (1560) |
| price band | train | 0.20-0.50 | -0.1436 (125) |
| price band | train | 0.50-0.80 | -0.0204 (118) |
| price band | train | <0.20 | -0.3996 (8) |
| price band | train | >0.80 | -0.1320 (23) |
| price band | test | 0.20-0.50 | -0.0313 (622) |
| price band | test | 0.50-0.80 | +0.0041 (750) |
| price band | test | <0.20 | -0.4533 (50) |
| price band | test | >0.80 | -0.0388 (138) |
| sport | train | americanfootball_nfl | +0.4400 (1) |
| sport | train | baseball_mlb | -0.4677 (57) |
| sport | train | basketball_nba | -0.0559 (50) |
| sport | train | basketball_wnba | -0.2280 (6) |
| sport | train | icehockey_nhl | +0.6302 (2) |
| sport | train | mma | -0.1093 (21) |
| sport | train | soccer | -0.2291 (11) |
| sport | train | tennis | +0.0584 (126) |
| sport | test | americanfootball_ncaaf | -1.0090 (1) |
| sport | test | americanfootball_nfl | +0.9850 (1) |
| sport | test | baseball_kbo | +0.7115 (1) |
| sport | test | baseball_mlb | -0.0099 (481) |
| sport | test | basketball_nba | +0.4750 (1) |
| sport | test | basketball_wnba | -0.4897 (9) |
| sport | test | mma | -0.3662 (17) |
| sport | test | soccer | +0.0272 (200) |
| sport | test | tennis | -0.0420 (849) |
| timing | train | in-play | -0.0870 (191) |
| timing | train | pregame | -0.1200 (83) |
| timing | test | in-play | -0.0768 (788) |
| timing | test | pregame | +0.0209 (772) |
| notional decile | train | D0 | -0.3970 (17) |
| notional decile | train | D1 | -0.3091 (18) |
| notional decile | train | D2 | -0.1377 (22) |
| notional decile | train | D3 | +0.0589 (28) |
| notional decile | train | D4 | +0.1902 (29) |
| notional decile | train | D5 | -0.1979 (45) |
| notional decile | train | D6 | -0.2017 (30) |
| notional decile | train | D7 | -0.0894 (37) |
| notional decile | train | D8 | +0.0815 (25) |
| notional decile | train | D9 | -0.0949 (23) |
| notional decile | test | D0 | -0.0602 (104) |
| notional decile | test | D1 | -0.0743 (116) |
| notional decile | test | D2 | +0.0858 (166) |
| notional decile | test | D3 | -0.0407 (174) |
| notional decile | test | D4 | +0.0552 (175) |
| notional decile | test | D5 | +0.0390 (190) |
| notional decile | test | D6 | -0.0822 (167) |
| notional decile | test | D7 | +0.0300 (133) |
| notional decile | test | D8 | -0.1224 (136) |
| notional decile | test | D9 | -0.1379 (199) |


### Q=top50 K=2 E=exit

| diagnostic | split | level | mean ROI/$ (n) |
| --- | --- | --- | --- |
| follow/fade | train | follow | -0.0850 (274) |
| follow/fade | train | fade | +0.0474 (274) |
| follow/fade | test | follow | -0.0355 (1560) |
| follow/fade | test | fade | -0.0632 (1560) |
| price band | train | 0.20-0.50 | -0.1022 (125) |
| price band | train | 0.50-0.80 | -0.0481 (118) |
| price band | train | <0.20 | -0.2936 (8) |
| price band | train | >0.80 | -0.1083 (23) |
| price band | test | 0.20-0.50 | -0.0605 (622) |
| price band | test | 0.50-0.80 | +0.0024 (750) |
| price band | test | <0.20 | -0.2868 (50) |
| price band | test | >0.80 | -0.0383 (138) |
| sport | train | americanfootball_nfl | +0.4400 (1) |
| sport | train | baseball_mlb | -0.4102 (57) |
| sport | train | basketball_nba | -0.0844 (50) |
| sport | train | basketball_wnba | -0.3348 (6) |
| sport | train | icehockey_nhl | +0.6189 (2) |
| sport | train | mma | -0.0544 (21) |
| sport | train | soccer | -0.2118 (11) |
| sport | train | tennis | +0.0645 (126) |
| sport | test | americanfootball_ncaaf | -1.0090 (1) |
| sport | test | americanfootball_nfl | +0.9850 (1) |
| sport | test | baseball_kbo | +0.6920 (1) |
| sport | test | baseball_mlb | +0.0047 (481) |
| sport | test | basketball_nba | +0.4750 (1) |
| sport | test | basketball_wnba | -0.4897 (9) |
| sport | test | mma | -0.3662 (17) |
| sport | test | soccer | +0.0246 (200) |
| sport | test | tennis | -0.0626 (849) |
| timing | train | in-play | -0.0613 (191) |
| timing | train | pregame | -0.1395 (83) |
| timing | test | in-play | -0.0885 (788) |
| timing | test | pregame | +0.0185 (772) |
| notional decile | train | D0 | -0.4290 (17) |
| notional decile | train | D1 | -0.1791 (18) |
| notional decile | train | D2 | -0.1926 (22) |
| notional decile | train | D3 | +0.0853 (28) |
| notional decile | train | D4 | +0.0928 (29) |
| notional decile | train | D5 | -0.1262 (45) |
| notional decile | train | D6 | -0.1655 (30) |
| notional decile | train | D7 | -0.0710 (37) |
| notional decile | train | D8 | +0.0766 (25) |
| notional decile | train | D9 | -0.0979 (23) |
| notional decile | test | D0 | -0.0137 (104) |
| notional decile | test | D1 | -0.0707 (116) |
| notional decile | test | D2 | +0.0795 (166) |
| notional decile | test | D3 | -0.0438 (174) |
| notional decile | test | D4 | +0.0463 (175) |
| notional decile | test | D5 | +0.0025 (190) |
| notional decile | test | D6 | -0.0862 (167) |
| notional decile | test | D7 | +0.0024 (133) |
| notional decile | test | D8 | -0.1528 (136) |
| notional decile | test | D9 | -0.1262 (199) |


### Q=top20 K=1 E=hold

| diagnostic | split | level | mean ROI/$ (n) |
| --- | --- | --- | --- |
| follow/fade | train | follow | -0.0818 (1014) |
| follow/fade | train | fade | -0.0410 (1014) |
| follow/fade | test | follow | +0.0264 (4122) |
| follow/fade | test | fade | -0.1184 (4122) |
| price band | train | 0.20-0.50 | -0.1441 (407) |
| price band | train | 0.50-0.80 | +0.0427 (472) |
| price band | train | <0.20 | -0.9063 (42) |
| price band | train | >0.80 | -0.0695 (93) |
| price band | test | 0.20-0.50 | +0.0457 (1708) |
| price band | test | 0.50-0.80 | +0.0073 (1775) |
| price band | test | <0.20 | +0.0638 (119) |
| price band | test | >0.80 | +0.0195 (520) |
| sport | train | americanfootball_nfl | -1.0150 (1) |
| sport | train | baseball_mlb | -0.0031 (171) |
| sport | train | basketball_nba | -0.3795 (111) |
| sport | train | basketball_wnba | -0.1315 (114) |
| sport | train | cricket | +0.6459 (5) |
| sport | train | icehockey_nhl | +0.4662 (21) |
| sport | train | mma | +0.2177 (124) |
| sport | train | soccer | +0.0775 (35) |
| sport | train | tennis | -0.1552 (432) |
| sport | test | americanfootball_ncaaf | +0.4679 (7) |
| sport | test | americanfootball_nfl | +0.2318 (5) |
| sport | test | baseball_kbo | +0.4692 (13) |
| sport | test | baseball_mlb | +0.0326 (1309) |
| sport | test | basketball_nba | +0.9547 (1) |
| sport | test | basketball_wnba | -0.1807 (98) |
| sport | test | cricket | -0.5434 (8) |
| sport | test | mma | +0.2304 (49) |
| sport | test | politics | +0.3511 (22) |
| sport | test | soccer | +0.0013 (633) |
| sport | test | tennis | +0.0287 (1977) |
| timing | train | in-play | -0.1656 (502) |
| timing | train | pregame | +0.0003 (512) |
| timing | test | in-play | +0.0114 (1369) |
| timing | test | pregame | +0.0338 (2753) |
| notional decile | train | D0 | +0.2179 (40) |
| notional decile | train | D1 | -0.1143 (73) |
| notional decile | train | D2 | -0.1925 (62) |
| notional decile | train | D3 | -0.0567 (116) |
| notional decile | train | D4 | -0.1221 (87) |
| notional decile | train | D5 | -0.2239 (152) |
| notional decile | train | D6 | +0.0655 (102) |
| notional decile | train | D7 | -0.0811 (126) |
| notional decile | train | D8 | -0.1486 (119) |
| notional decile | train | D9 | +0.0075 (137) |
| notional decile | test | D0 | +0.0191 (342) |
| notional decile | test | D1 | -0.0607 (344) |
| notional decile | test | D2 | +0.0728 (398) |
| notional decile | test | D3 | +0.0146 (441) |
| notional decile | test | D4 | +0.0467 (532) |
| notional decile | test | D5 | +0.0562 (547) |
| notional decile | test | D6 | -0.0170 (485) |
| notional decile | test | D7 | +0.0014 (363) |
| notional decile | test | D8 | +0.0343 (377) |
| notional decile | test | D9 | +0.0919 (293) |


### Q=top20 K=1 E=exit

| diagnostic | split | level | mean ROI/$ (n) |
| --- | --- | --- | --- |
| follow/fade | train | follow | -0.0809 (1014) |
| follow/fade | train | fade | -0.0410 (1014) |
| follow/fade | test | follow | +0.0081 (4122) |
| follow/fade | test | fade | -0.1184 (4122) |
| price band | train | 0.20-0.50 | -0.1286 (407) |
| price band | train | 0.50-0.80 | +0.0301 (472) |
| price band | train | <0.20 | -0.8734 (42) |
| price band | train | >0.80 | -0.0774 (93) |
| price band | test | 0.20-0.50 | +0.0151 (1708) |
| price band | test | 0.50-0.80 | -0.0004 (1775) |
| price band | test | <0.20 | +0.0237 (119) |
| price band | test | >0.80 | +0.0103 (520) |
| sport | train | americanfootball_nfl | -1.0150 (1) |
| sport | train | baseball_mlb | -0.0223 (171) |
| sport | train | basketball_nba | -0.3710 (111) |
| sport | train | basketball_wnba | -0.1369 (114) |
| sport | train | cricket | +0.6459 (5) |
| sport | train | icehockey_nhl | +0.4579 (21) |
| sport | train | mma | +0.2153 (124) |
| sport | train | soccer | +0.0775 (35) |
| sport | train | tennis | -0.1451 (432) |
| sport | test | americanfootball_ncaaf | +0.4679 (7) |
| sport | test | americanfootball_nfl | +0.2269 (5) |
| sport | test | baseball_kbo | +0.4602 (13) |
| sport | test | baseball_mlb | +0.0298 (1309) |
| sport | test | basketball_nba | +0.9324 (1) |
| sport | test | basketball_wnba | -0.2146 (98) |
| sport | test | cricket | -0.5434 (8) |
| sport | test | mma | +0.2300 (49) |
| sport | test | politics | +0.3061 (22) |
| sport | test | soccer | +0.0071 (633) |
| sport | test | tennis | -0.0072 (1977) |
| timing | train | in-play | -0.1673 (502) |
| timing | train | pregame | +0.0038 (512) |
| timing | test | in-play | -0.0234 (1369) |
| timing | test | pregame | +0.0237 (2753) |
| notional decile | train | D0 | +0.2150 (40) |
| notional decile | train | D1 | -0.1125 (73) |
| notional decile | train | D2 | -0.2082 (62) |
| notional decile | train | D3 | -0.0643 (116) |
| notional decile | train | D4 | -0.1220 (87) |
| notional decile | train | D5 | -0.1463 (152) |
| notional decile | train | D6 | -0.0204 (102) |
| notional decile | train | D7 | -0.0841 (126) |
| notional decile | train | D8 | -0.1485 (119) |
| notional decile | train | D9 | +0.0083 (137) |
| notional decile | test | D0 | +0.0161 (342) |
| notional decile | test | D1 | -0.0890 (344) |
| notional decile | test | D2 | +0.0683 (398) |
| notional decile | test | D3 | -0.0079 (441) |
| notional decile | test | D4 | +0.0299 (532) |
| notional decile | test | D5 | +0.0298 (547) |
| notional decile | test | D6 | -0.0607 (485) |
| notional decile | test | D7 | -0.0099 (363) |
| notional decile | test | D8 | +0.0294 (377) |
| notional decile | test | D9 | +0.0837 (293) |


### Q=top20 K=2 E=hold

| diagnostic | split | level | mean ROI/$ (n) |
| --- | --- | --- | --- |
| follow/fade | train | follow | -0.2546 (37) |
| follow/fade | train | fade | +0.2028 (37) |
| follow/fade | test | follow | +0.0402 (544) |
| follow/fade | test | fade | -0.1011 (544) |
| price band | train | 0.20-0.50 | -0.2445 (25) |
| price band | train | 0.50-0.80 | -0.3808 (9) |
| price band | train | >0.80 | +0.0395 (3) |
| price band | test | 0.20-0.50 | +0.0052 (254) |
| price band | test | 0.50-0.80 | +0.0784 (223) |
| price band | test | <0.20 | +0.2475 (17) |
| price band | test | >0.80 | -0.0222 (50) |
| sport | train | baseball_mlb | -0.2046 (11) |
| sport | train | basketball_nba | -0.3883 (11) |
| sport | train | tennis | -0.1933 (15) |
| sport | test | americanfootball_nfl | +1.0677 (1) |
| sport | test | baseball_mlb | +0.0311 (248) |
| sport | test | basketball_wnba | +0.9850 (1) |
| sport | test | mma | -1.0078 (1) |
| sport | test | soccer | +0.0246 (45) |
| sport | test | tennis | +0.0485 (248) |
| timing | train | in-play | -0.3897 (25) |
| timing | train | pregame | +0.0268 (12) |
| timing | test | in-play | +0.0664 (180) |
| timing | test | pregame | +0.0273 (364) |
| notional decile | train | D0 | -0.0071 (3) |
| notional decile | train | D1 | +0.1218 (6) |
| notional decile | train | D2 | -0.6273 (5) |
| notional decile | train | D3 | -0.6679 (3) |
| notional decile | train | D4 | -1.0213 (3) |
| notional decile | train | D5 | -1.0146 (4) |
| notional decile | train | D7 | +0.6123 (5) |
| notional decile | train | D8 | +0.5119 (4) |
| notional decile | train | D9 | -0.7441 (4) |
| notional decile | test | D0 | -0.0512 (36) |
| notional decile | test | D1 | +0.1414 (44) |
| notional decile | test | D2 | +0.2533 (89) |
| notional decile | test | D3 | -0.1286 (68) |
| notional decile | test | D4 | -0.2579 (60) |
| notional decile | test | D5 | +0.0478 (72) |
| notional decile | test | D6 | +0.1339 (59) |
| notional decile | test | D7 | -0.1209 (39) |
| notional decile | test | D8 | +0.2099 (44) |
| notional decile | test | D9 | +0.1005 (33) |


### Q=top20 K=2 E=exit

| diagnostic | split | level | mean ROI/$ (n) |
| --- | --- | --- | --- |
| follow/fade | train | follow | -0.3064 (37) |
| follow/fade | train | fade | +0.2028 (37) |
| follow/fade | test | follow | +0.0155 (544) |
| follow/fade | test | fade | -0.1011 (544) |
| price band | train | 0.20-0.50 | -0.2451 (25) |
| price band | train | 0.50-0.80 | -0.5905 (9) |
| price band | train | >0.80 | +0.0355 (3) |
| price band | test | 0.20-0.50 | -0.0314 (254) |
| price band | test | 0.50-0.80 | +0.0600 (223) |
| price band | test | <0.20 | +0.2440 (17) |
| price band | test | >0.80 | -0.0222 (50) |
| sport | train | baseball_mlb | -0.2932 (11) |
| sport | train | basketball_nba | -0.4899 (11) |
| sport | train | tennis | -0.1815 (15) |
| sport | test | americanfootball_nfl | +1.0677 (1) |
| sport | test | baseball_mlb | +0.0208 (248) |
| sport | test | basketball_wnba | +0.9850 (1) |
| sport | test | mma | -1.0078 (1) |
| sport | test | soccer | +0.0228 (45) |
| sport | test | tennis | +0.0049 (248) |
| timing | train | in-play | -0.4248 (25) |
| timing | train | pregame | -0.0596 (12) |
| timing | test | in-play | +0.0212 (180) |
| timing | test | pregame | +0.0127 (364) |
| notional decile | train | D0 | -0.0071 (3) |
| notional decile | train | D1 | -0.0716 (6) |
| notional decile | train | D2 | -0.8224 (5) |
| notional decile | train | D3 | -0.6679 (3) |
| notional decile | train | D4 | -1.0213 (3) |
| notional decile | train | D5 | -0.9632 (4) |
| notional decile | train | D7 | +0.6177 (5) |
| notional decile | train | D8 | +0.5119 (4) |
| notional decile | train | D9 | -0.7472 (4) |
| notional decile | test | D0 | -0.0517 (36) |
| notional decile | test | D1 | +0.1155 (44) |
| notional decile | test | D2 | +0.2340 (89) |
| notional decile | test | D3 | -0.1370 (68) |
| notional decile | test | D4 | -0.2715 (60) |
| notional decile | test | D5 | -0.0038 (72) |
| notional decile | test | D6 | +0.0574 (59) |
| notional decile | test | D7 | -0.1125 (39) |
| notional decile | test | D8 | +0.1996 (44) |
| notional decile | test | D9 | +0.0751 (33) |


## Post-hoc cut (not in the plan; diagnostic only)

Suggested by the review of Revision 1, computed AFTER the plan's verdict was known,
and therefore not a test of anything: it cannot promote a configuration and its
bounds carry no multiplicity correction. It asks one question — does the test-window
edge survive among wallets whose selection could not have been driven by profit
earned inside the test window?

Cohort: wallets whose EARLIEST visible print predates 2026-06-01 — 17,699 of
66,246 candidate rows, 146 of 1,631 wallets.
Strata by `leaderboard_snapshots` membership: a wallet seen ONLY on the ALL-time
board was not selected for recent profit; a wallet on a DAY/WEEK/MONTH board was.

### pre-June cohort (all) — n 17,699, wallets 146 (train 11,194 / test 6,193 / live 312)

| config | train mean (lb80, n) | test mean (lb80, n) | live mean (lb80, n) |
| --- | --- | --- | --- |
| Q=all K=1 E=hold | -0.0487 (-0.0600, n 11194) | +0.0026 (-0.0113, n 6193) | -0.0215 (-0.0747, n 312) |
| Q=all K=1 E=exit | -0.0505 (-0.0617, n 11194) | -0.0087 (-0.0209, n 6193) | -0.0270 (-0.0808, n 312) |
| Q=all K=2 E=hold | -0.0230 (-0.0448, n 1346) | -0.0204 (-0.0460, n 883) | +0.3233 (+0.1153, n 8) |
| Q=all K=2 E=exit | -0.0247 (-0.0453, n 1346) | -0.0329 (-0.0581, n 883) | +0.2795 (+0.0843, n 8) |
| Q=top50 K=1 E=hold | -0.0582 (-0.0793, n 3513) | -0.0216 (-0.0410, n 3504) | -0.0582 (-0.1447, n 159) |
| Q=top50 K=1 E=exit | -0.0601 (-0.0817, n 3513) | -0.0309 (-0.0476, n 3504) | -0.0801 (-0.1646, n 159) |
| Q=top50 K=2 E=hold | -0.1118 (-0.1656, n 246) | -0.0017 (-0.0351, n 474) | -0.2563 (-0.4144, n 16) |
| Q=top50 K=2 E=exit | -0.0969 (-0.1515, n 246) | -0.0281 (-0.0619, n 474) | -0.2324 (-0.3934, n 16) |
| Q=top20 K=1 E=hold | -0.0854 (-0.1263, n 1002) | +0.0094 (-0.0206, n 1518) | -0.1895 (-0.3212, n 74) |
| Q=top20 K=1 E=exit | -0.0810 (-0.1212, n 1002) | -0.0126 (-0.0416, n 1518) | -0.1907 (-0.3253, n 74) |
| Q=top20 K=2 E=hold | -0.2880 (-0.4590, n 36) | +0.0773 (-0.0067, n 162) | -0.5227 (-0.7208, n 13) |
| Q=top20 K=2 E=exit | -0.3141 (-0.4556, n 36) | +0.0446 (-0.0357, n 162) | -0.5227 (-0.7229, n 13) |


### pre-June, ALL-time board ONLY — n 2,821, wallets 45 (train 2,707 / test 113 / live 1)

| config | train mean (lb80, n) | test mean (lb80, n) | live mean (lb80, n) |
| --- | --- | --- | --- |
| Q=all K=1 E=hold | +0.0038 (-0.0322, n 2707) | +0.0959 (-0.0684, n 113) | +0.8727 (+0.8727, n 1) |
| Q=all K=1 E=exit | +0.0039 (-0.0277, n 2707) | +0.1083 (-0.0671, n 113) | +0.8727 (+0.8727, n 1) |
| Q=all K=2 E=hold | +0.1062 (+0.0462, n 170) | +0.0025 (-0.3012, n 9) | — |
| Q=all K=2 E=exit | +0.0872 (+0.0302, n 170) | +0.0889 (-0.1744, n 9) | — |
| Q=top50 K=1 E=hold | +0.0726 (+0.0141, n 974) | +0.1360 (-0.0399, n 108) | +0.8727 (+0.8727, n 1) |
| Q=top50 K=1 E=exit | +0.0702 (+0.0128, n 974) | +0.1388 (-0.0458, n 108) | +0.8727 (+0.8727, n 1) |
| Q=top50 K=2 E=hold | +0.0589 (-0.0533, n 37) | -0.3528 (-0.5534, n 9) | — |
| Q=top50 K=2 E=exit | +0.0898 (-0.0265, n 37) | -0.3550 (-0.5507, n 9) | — |
| Q=top20 K=1 E=hold | +0.1192 (-0.0000, n 219) | +0.1105 (-0.0813, n 102) | +0.8727 (+0.8727, n 1) |
| Q=top20 K=1 E=exit | +0.1214 (+0.0020, n 219) | +0.1135 (-0.0678, n 102) | +0.8727 (+0.8727, n 1) |
| Q=top20 K=2 E=hold | -1.0144 (-1.0153, n 2) | +0.6214 (-0.0445, n 14) | — |
| Q=top20 K=2 E=exit | -1.0144 (-1.0153, n 2) | +0.6186 (-0.0595, n 14) | — |


### pre-June, on any DAY/WEEK/MONTH board — n 14,861, wallets 100 (train 8,474 / test 6,076 / live 311)

| config | train mean (lb80, n) | test mean (lb80, n) | live mean (lb80, n) |
| --- | --- | --- | --- |
| Q=all K=1 E=hold | -0.0667 (-0.0778, n 8474) | +0.0001 (-0.0129, n 6076) | -0.0244 (-0.0801, n 311) |
| Q=all K=1 E=exit | -0.0691 (-0.0800, n 8474) | -0.0116 (-0.0232, n 6076) | -0.0299 (-0.0804, n 311) |
| Q=all K=2 E=hold | -0.0428 (-0.0671, n 1174) | -0.0206 (-0.0470, n 874) | +0.3233 (+0.1193, n 8) |
| Q=all K=2 E=exit | -0.0421 (-0.0665, n 1174) | -0.0342 (-0.0599, n 874) | +0.2795 (+0.0992, n 8) |
| Q=top50 K=1 E=hold | -0.1131 (-0.1339, n 2533) | -0.0280 (-0.0456, n 3392) | -0.0641 (-0.1479, n 158) |
| Q=top50 K=1 E=exit | -0.1149 (-0.1328, n 2533) | -0.0377 (-0.0536, n 3392) | -0.0861 (-0.1731, n 158) |
| Q=top50 K=2 E=hold | -0.1538 (-0.2144, n 208) | +0.0051 (-0.0328, n 465) | -0.2563 (-0.4112, n 16) |
| Q=top50 K=2 E=exit | -0.1415 (-0.2033, n 208) | -0.0218 (-0.0547, n 465) | -0.2324 (-0.3915, n 16) |
| Q=top20 K=1 E=hold | -0.1427 (-0.1776, n 781) | -0.0011 (-0.0283, n 1412) | -0.2040 (-0.3387, n 73) |
| Q=top20 K=1 E=exit | -0.1377 (-0.1714, n 781) | -0.0251 (-0.0519, n 1412) | -0.2053 (-0.3349, n 73) |
| Q=top20 K=2 E=hold | -0.2452 (-0.4093, n 34) | +0.0259 (-0.0351, n 148) | -0.5227 (-0.7193, n 13) |
| Q=top20 K=2 E=exit | -0.2729 (-0.4523, n 34) | -0.0097 (-0.0721, n 148) | -0.5227 (-0.7213, n 13) |


### Overlap cohort — wallets that traded in BOTH the train and the test era

24,631 rows over 155 wallets. The same wallets, measured twice:
whatever changed between the two windows is not a change of population.

| config | train mean (lb80, n) | test mean (lb80, n) | live mean (lb80, n) |
| --- | --- | --- | --- |
| Q=all K=1 E=hold | -0.0604 (-0.0707, n 11254) | -0.0126 (-0.0216, n 12850) | -0.0039 (-0.0437, n 527) |
| Q=all K=1 E=exit | -0.0635 (-0.0738, n 11254) | -0.0232 (-0.0320, n 12850) | -0.0055 (-0.0451, n 527) |
| Q=top50 K=1 E=hold | -0.0900 (-0.1075, n 3206) | -0.0209 (-0.0334, n 5706) | -0.0562 (-0.1077, n 253) |
| Q=top50 K=1 E=exit | -0.0942 (-0.1123, n 3206) | -0.0298 (-0.0436, n 5706) | -0.0656 (-0.1178, n 253) |
| Q=top20 K=1 E=hold | -0.1369 (-0.1712, n 858) | +0.0216 (+0.0000, n 2259) | -0.1005 (-0.1866, n 125) |
| Q=top20 K=1 E=exit | -0.1364 (-0.1669, n 858) | +0.0004 (-0.0206, n 2259) | -0.1022 (-0.1808, n 125) |


### What the cut says

On the whole sample the baseline's test mean is +0.0110. In the pre-June cohort it is
+0.0026 (n 6,193); among the wallets that reached a DAY/WEEK/MONTH board it is
+0.0001 (n 6,076); among the ones seen ONLY on the ALL-time board it is
+0.0959 — on n 113, which is far too thin to carry a conclusion in either
direction. And the overlap cohort, the same wallets measured in both windows, runs
-0.0604 on train against -0.0126 on test.

Read together: the test-window edge does NOT survive restriction to wallets that
were already visible before June, and the ALL-time-only stratum cannot arbitrate
because it barely trades in the test window. Nothing here is a test and nothing
here promotes a configuration.

### Weekly series of the baseline (`Q=all K=1 E=hold`), by ISO week of entry

Where the sign flips, week by week, so the reader can see that the train/test split
date is not a boundary between two regimes but a line drawn through a drifting
series. A week straddling a boundary appears once per era.

| ISO week | era | n | mean ROI/$ |
| --- | --- | --- | --- |
| 2026-W05 | train | 42 | -0.2757 |
| 2026-W06 | train | 162 | +0.0240 |
| 2026-W07 | train | 112 | +0.0210 |
| 2026-W08 | train | 208 | +0.2288 |
| 2026-W09 | train | 285 | +0.0586 |
| 2026-W10 | train | 343 | -0.0576 |
| 2026-W11 | train | 291 | +0.0565 |
| 2026-W12 | train | 376 | -0.0171 |
| 2026-W13 | train | 293 | -0.0321 |
| 2026-W14 | train | 376 | +0.0306 |
| 2026-W15 | train | 527 | -0.0761 |
| 2026-W16 | train | 379 | -0.0037 |
| 2026-W17 | train | 563 | -0.1033 |
| 2026-W18 | train | 444 | -0.0553 |
| 2026-W19 | train | 642 | +0.0002 |
| 2026-W20 | train | 704 | -0.0960 |
| 2026-W21 | train | 793 | -0.0565 |
| 2026-W22 | train | 983 | -0.0663 |
| 2026-W23 | train | 613 | -0.0746 |
| 2026-W24 | train | 803 | -0.0274 |
| 2026-W25 | train | 734 | -0.1207 |
| 2026-W26 | train | 709 | -0.1009 |
| 2026-W27 | train | 1,015 | -0.0929 |
| 2026-W28 | train | 1,334 | -0.1158 |
| 2026-W29 | test | 847 | -0.0460 |
| 2026-W29 | train | 494 | +0.0024 |
| 2026-W30 | test | 2,078 | -0.0566 |
| 2026-W31 | test | 2,443 | -0.0259 |
| 2026-W32 | test | 2,796 | -0.0553 |
| 2026-W33 | test | 3,967 | +0.0198 |
| 2026-W34 | test | 5,939 | +0.0067 |
| 2026-W35 | test | 10,704 | +0.0028 |
| 2026-W36 | live | 13,210 | -0.0565 |
| 2026-W36 | test | 11,037 | +0.0601 |


## Known limitations

Limitation 1 is REWRITTEN in Revision 2 — the plan's wording ("discovered by large
recent prints") named the wrong mechanism. 2–4 are verbatim from the plan.

1. **Selection on profit earned inside the test window.** 586 of the 607
   backfilled wallets have `wallets.source = 'leaderboard'`: they came from `leaderboard_snapshots`,
   category SPORTS, ordered by PNL — **ALL** (order_by PNL, 1,002 wallets, snapshotted 2026-09-04→2026-09-05); **DAY** (order_by PNL, 1,548 wallets, snapshotted 2026-09-04→2026-09-05, so its P&L window covers 2026-09-04→2026-09-05); **MONTH** (order_by PNL, 1,224 wallets, snapshotted 2026-09-03→2026-09-05, so its P&L window covers 2026-08-06→2026-09-05); **WEEK** (order_by PNL, 1,149 wallets, snapshotted 2026-09-04→2026-09-05, so its P&L window covers 2026-08-29→2026-09-05).
   The MONTH board looks back ~30 days and the WEEK board ~7 from a snapshot taken
   2026-09-03..05, so both windows lie INSIDE the test window (2026-07-16 →
   2026-09-04). The sample is therefore conditioned on RECENT PROFIT, measured in
   the period the test window measures. That is the mechanism, and it points the
   same way as the result: the test window is the era that comes out positive.
   Compounding it, the /activity backfill caps at 2,000 prints per wallet — 151 of
   592 wallets sit at the cap and the median wallet has 1,979 prints — so for a
   high-frequency wallet the "first visible print" is recent by construction, and
   its early, possibly worse, history is simply not in the tape.
   Wallets that blew up and stopped trading are absent either way. The baseline
   (Q = all) carries the same bias, so the DIFFERENCE between Q levels is the
   honest read of skill ranking, not the absolute ROI.
2. Slippage is an assumption; there are no historical quotes. The lane's own 120 s markouts
   (mean drift ≈ 0) are the only calibration.
3. Depth is ignored (the lane's depth gate is not simulated).
4. Outcome P&L only; no mark-to-market.
