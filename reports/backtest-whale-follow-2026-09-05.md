# Whale-follow backtest — results (2026-09-05)

Pre-specified plan: `reports/backtest-whale-follow-plan-2026-09-05.md`, frozen before
any result below was computed. Nothing here changes the lane; anything it suggests goes
through PREREGISTRATION.md as a dated amendment.

Command: `python tools/backtest_whale_follow.py --slippage 0.01 --seed 20260905`
 (split 2026-07-16, 2000 bootstrap resamples, Bonferroni-12 quantile 0.016667).

## What the run says (numbers only; the verdict rule is the plan's)

- **No configuration is FAVORABLE** under the plan's rule (test Bonferroni-12 LB > 0 and the train sign agrees).
- Baseline `Q=all K=1 E=hold`: train -0.0572 (n 12863), test +0.0117 (n 38960), live era -0.0574 (n 12108), per $.
- Tightest cell `Q=top20 K=2 E=hold`: train -0.1919 (n 56), test +0.1045 (n 1089), live era -0.3224 (n 308). The skill filter's SIGN is not stable across the three eras.
- The exit rule costs money nearly everywhere: mean of (exit − hold) over the 18 (split, Q, K) cells = -0.0063 per $.
- Every one of the 12 configurations is negative on train AND on the live era; only
  the test window (2026-07-16 → 2026-09-04, which is entirely backfilled /activity
  prints) is positive, and the Bonferroni-12 correction removes even that.


## Deviations from the plan

1. **The fee.** The plan asks for the venue fee "exactly as `arb/whale_score.realized_edge` charges it". `realized_edge` charges NO fee — it is a gross per-share
   edge. Implemented instead: `realized_edge`'s own accounting (proceeds − cost, per
   share, divided by the entry price to become per dollar) minus ONE Polymarket taker
   fee at entry from the lane's single fee site, `venues.fees.leg_fee_cost`, priced with
   the position's root as the fee category — the same charge `arb/lanes/live.py` puts on
   the whale lane's own paper bets. The exit leg pays no second fee.
2. **Top-50%/top-20% is the lane's own bar**, `arb/whale_elite.cutoff` with `elite_pct`
   50 and 20: the reference population is every wallet with >= 1 resolved outcome in the
   root as of that month, each scored into the candidate's bucket by the transfer chain,
   and the cutoff is nearest-rank from the top. A wallet outside the population (no
   resolved history) is still compared to the cutoff, exactly as `whale_elite.point`
   does, so it can pass on the prior alone. Where a population is too small to have one
   elite member the cutoff is None and NOTHING qualifies in that bucket that month.
3. **Consensus counts every later entry**, not only the literal 2nd: the unit of analysis
   is one position, so the 3rd and 4th qualified wallet within 24 h each carry their own
   $1 stake.
4. **Win rate is ROI > 0**, so the hold and exit rules are measured the same way.
5. Only entry months 2026-02..2026-09 are in scope (the plan's `as_of` grid); 5,334 older
   positions have no walk-forward score and are excluded.

## The data as read

Snapshot at run time: 77,076 `positions_mat` rows, 1,044,145 prints,
74,451 resolutions, 86,478 markets.
Candidates (resolved market, entry month in scope, entry print found): **63,931**
— train 12,863, test 38,960, live era 12,108.

### Wallets, cutoffs and base rates per month (walk-forward)

| as_of | sports pop | scored wallets | sports base rate m0 | candidates | wallets | pass top50 | pass top20 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-02 | 51 | 52 | +0.0352 (n 633) | 619 | 35 | 24 | 7 |
| 2026-03 | 61 | 62 | -0.0200 (n 387) | 1436 | 48 | 13 | 3 |
| 2026-04 | 80 | 81 | -0.0072 (n 763) | 2003 | 64 | 20 | 6 |
| 2026-05 | 101 | 102 | -0.0278 (n 865) | 3348 | 100 | 25 | 11 |
| 2026-06 | 137 | 138 | -0.0363 (n 1481) | 2933 | 108 | 32 | 14 |
| 2026-07 | 163 | 165 | -0.0326 (n 1394) | 6960 | 184 | 54 | 21 |
| 2026-08 | 225 | 227 | -0.0059 (n 3242) | 25931 | 380 | 93 | 48 |
| 2026-09 | 390 | 390 | +0.0113 (n 12583) | 20701 | 1434 | 168 | 93 |


### Positions followed, per configuration

| config | train n | test n | live n |
| --- | --- | --- | --- |
| Q=all K=1 E=hold | 12863 | 38960 | 12108 |
| Q=all K=1 E=exit | 12863 | 38960 | 12108 |
| Q=all K=2 E=hold | 3258 | 21420 | 10594 |
| Q=all K=2 E=exit | 3258 | 21420 | 10594 |
| Q=top50 K=1 E=hold | 3757 | 10356 | 1282 |
| Q=top50 K=1 E=exit | 3757 | 10356 | 1282 |
| Q=top50 K=2 E=hold | 485 | 3850 | 780 |
| Q=top50 K=2 E=exit | 485 | 3850 | 780 |
| Q=top20 K=1 E=hold | 1026 | 4067 | 716 |
| Q=top20 K=1 E=exit | 1026 | 4067 | 716 |
| Q=top20 K=2 E=hold | 56 | 1089 | 308 |
| Q=top20 K=2 E=exit | 56 | 1089 | 308 |


### Per-sport base facts (all candidates, follow, hold, main slippage)

| sport | n | mean ROI/$ | outcome win rate |
| --- | --- | --- | --- |
| tennis | 26251 | -0.0245 | 0.587 |
| soccer | 16634 | -0.0053 | 0.647 |
| baseball_mlb | 13685 | -0.0034 | 0.549 |
| basketball_nba | 2414 | -0.0909 | 0.554 |
| basketball_wnba | 1531 | -0.0603 | 0.547 |
| mma | 1472 | +0.0562 | 0.602 |
| americanfootball_ncaaf | 474 | +0.1346 | 0.652 |
| icehockey_nhl | 447 | -0.0210 | 0.521 |
| americanfootball_nfl | 409 | -0.0220 | 0.545 |
| cricket | 394 | -0.0604 | 0.614 |
| baseball_kbo | 129 | -0.0181 | 0.605 |
| politics | 57 | +0.1872 | 0.930 |
| baseball_npb | 24 | +0.1400 | 0.750 |
| basketball_fiba_womens | 8 | +0.0320 | 0.875 |
| crypto | 2 | -1.0333 | 0.000 |


### Where the prints come from (a plain fact, not one of the plan's diagnostics)

`source` is the print that OPENED the position: `activity` = the cold-start /activity
backfill of discovered wallets, `tape` = the global real-time collector, which only
started 2026-09-04. Limitation 1 has a calendar shape, and this is it.

| source | split | n | mean ROI/$ (follow, hold) |
| --- | --- | --- | --- |
| activity | train | 12863 | -0.0572 |
| activity | test | 38960 | +0.0117 |
| activity | live | 3027 | -0.0304 |
| tape | train | 0 | — |
| tape | test | 0 | — |
| tape | live | 9081 | -0.0664 |


## The 12 configurations

Main slippage 0.01. lb80 / ub90 / Bonferroni-12 LB are event-clustered bootstrap
quantiles (0.20 / 0.90 / 0.0167) over 2000 resamples, seed 20260905.

### train

| config | n | n_events | mean ROI/$ | win rate (ROI>0) | lb80 | ub90 | Bonf-12 LB |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Q=all K=1 E=hold | 12863 | 6449 | -0.0572 | 0.542 | -0.0683 | -0.0418 | -0.0831 |
| Q=all K=1 E=exit | 12863 | 6449 | -0.0581 | 0.526 | -0.0689 | -0.0433 | -0.0841 |
| Q=all K=2 E=hold | 3258 | 1329 | -0.0616 | 0.541 | -0.0878 | -0.0248 | -0.1205 |
| Q=all K=2 E=exit | 3258 | 1329 | -0.0697 | 0.519 | -0.0922 | -0.0364 | -0.1220 |
| Q=top50 K=1 E=hold | 3757 | 2383 | -0.0696 | 0.522 | -0.0894 | -0.0412 | -0.1177 |
| Q=top50 K=1 E=exit | 3757 | 2383 | -0.0716 | 0.505 | -0.0902 | -0.0431 | -0.1193 |
| Q=top50 K=2 E=hold | 485 | 268 | -0.0625 | 0.503 | -0.1242 | +0.0268 | -0.2124 |
| Q=top50 K=2 E=exit | 485 | 268 | -0.0554 | 0.493 | -0.1189 | +0.0367 | -0.2070 |
| Q=top20 K=1 E=hold | 1026 | 745 | -0.0781 | 0.522 | -0.1207 | -0.0221 | -0.1782 |
| Q=top20 K=1 E=exit | 1026 | 745 | -0.0759 | 0.514 | -0.1132 | -0.0207 | -0.1675 |
| Q=top20 K=2 E=hold | 56 | 35 | -0.1919 | 0.375 | -0.4144 | +0.1338 | -0.6806 |
| Q=top20 K=2 E=exit | 56 | 35 | -0.2276 | 0.339 | -0.4357 | +0.0943 | -0.7125 |


### test

| config | n | n_events | mean ROI/$ | win rate (ROI>0) | lb80 | ub90 | Bonf-12 LB |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Q=all K=1 E=hold | 38960 | 10834 | +0.0117 | 0.580 | +0.0052 | +0.0214 | -0.0053 |
| Q=all K=1 E=exit | 38960 | 10834 | +0.0028 | 0.570 | -0.0037 | +0.0121 | -0.0130 |
| Q=all K=2 E=hold | 21420 | 3989 | +0.0288 | 0.583 | +0.0183 | +0.0439 | +0.0026 |
| Q=all K=2 E=exit | 21420 | 3989 | +0.0186 | 0.572 | +0.0097 | +0.0328 | -0.0046 |
| Q=top50 K=1 E=hold | 10356 | 4278 | -0.0020 | 0.551 | -0.0143 | +0.0180 | -0.0346 |
| Q=top50 K=1 E=exit | 10356 | 4278 | -0.0155 | 0.546 | -0.0274 | +0.0036 | -0.0475 |
| Q=top50 K=2 E=hold | 3850 | 1258 | +0.0437 | 0.567 | +0.0172 | +0.0845 | -0.0183 |
| Q=top50 K=2 E=exit | 3850 | 1258 | +0.0246 | 0.562 | -0.0017 | +0.0626 | -0.0348 |
| Q=top20 K=1 E=hold | 4067 | 2068 | +0.0245 | 0.563 | +0.0037 | +0.0580 | -0.0271 |
| Q=top20 K=1 E=exit | 4067 | 2068 | +0.0074 | 0.552 | -0.0132 | +0.0380 | -0.0410 |
| Q=top20 K=2 E=hold | 1089 | 461 | +0.1045 | 0.572 | +0.0562 | +0.1774 | -0.0171 |
| Q=top20 K=2 E=exit | 1089 | 461 | +0.0741 | 0.564 | +0.0257 | +0.1446 | -0.0425 |


### live

| config | n | n_events | mean ROI/$ | win rate (ROI>0) | lb80 | ub90 | Bonf-12 LB |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Q=all K=1 E=hold | 12108 | 882 | -0.0574 | 0.553 | -0.0709 | -0.0367 | -0.0895 |
| Q=all K=1 E=exit | 12108 | 882 | -0.0592 | 0.541 | -0.0713 | -0.0393 | -0.0888 |
| Q=all K=2 E=hold | 10594 | 457 | -0.0581 | 0.552 | -0.0734 | -0.0356 | -0.0925 |
| Q=all K=2 E=exit | 10594 | 457 | -0.0595 | 0.539 | -0.0735 | -0.0390 | -0.0948 |
| Q=top50 K=1 E=hold | 1282 | 271 | -0.0762 | 0.535 | -0.1150 | -0.0130 | -0.1779 |
| Q=top50 K=1 E=exit | 1282 | 271 | -0.0731 | 0.529 | -0.1131 | -0.0151 | -0.1786 |
| Q=top50 K=2 E=hold | 780 | 117 | -0.1024 | 0.518 | -0.1524 | -0.0071 | -0.2449 |
| Q=top50 K=2 E=exit | 780 | 117 | -0.0974 | 0.509 | -0.1523 | -0.0077 | -0.2431 |
| Q=top20 K=1 E=hold | 716 | 214 | -0.1397 | 0.504 | -0.1918 | -0.0512 | -0.2788 |
| Q=top20 K=1 E=exit | 716 | 214 | -0.1333 | 0.503 | -0.1901 | -0.0427 | -0.2798 |
| Q=top20 K=2 E=hold | 308 | 74 | -0.3224 | 0.406 | -0.4116 | -0.1605 | -0.5432 |
| Q=top20 K=2 E=exit | 308 | 74 | -0.3113 | 0.406 | -0.3984 | -0.1442 | -0.5146 |


### Verdict (the plan's rule: TEST Bonferroni-12 LB > 0 AND the train sign agrees)

**No configuration is FAVORABLE.** Every one of the 12 fails the rule — the test
Bonferroni-12 lower bound is not above zero, or the train sign disagrees.

Near misses, reported and NOT promoted: test Bonferroni-12 LB > 0 but the train sign
disagrees — Q=all K=2 E=hold. Test lb80 > 0 (the un-corrected
80% bound, which the plan does not accept as a verdict) — Q=all K=1 E=hold, Q=all K=2 E=hold, Q=all K=2 E=exit, Q=top50 K=2 E=hold, Q=top20 K=1 E=hold, Q=top20 K=2 E=hold, Q=top20 K=2 E=exit.

## Sensitivity to slippage (mean ROI/$, never used to pick)

### slippage 0.00

| config | train | test | live |
| --- | --- | --- | --- |
| Q=all K=1 E=hold | -0.0374 | +0.0327 | -0.0380 |
| Q=all K=1 E=exit | -0.0340 | +0.0263 | -0.0375 |
| Q=all K=2 E=hold | -0.0413 | +0.0506 | -0.0385 |
| Q=all K=2 E=exit | -0.0444 | +0.0431 | -0.0376 |
| Q=top50 K=1 E=hold | -0.0494 | +0.0198 | -0.0561 |
| Q=top50 K=1 E=exit | -0.0466 | +0.0088 | -0.0517 |
| Q=top50 K=2 E=hold | -0.0399 | +0.0681 | -0.0823 |
| Q=top50 K=2 E=exit | -0.0272 | +0.0518 | -0.0753 |
| Q=top20 K=1 E=hold | -0.0595 | +0.0476 | -0.1218 |
| Q=top20 K=1 E=exit | -0.0513 | +0.0321 | -0.1144 |
| Q=top20 K=2 E=hold | -0.1711 | +0.1326 | -0.3085 |
| Q=top20 K=2 E=exit | -0.2028 | +0.1040 | -0.2959 |


### slippage 0.02

| config | train | test | live |
| --- | --- | --- | --- |
| Q=all K=1 E=hold | -0.0758 | -0.0081 | -0.0755 |
| Q=all K=1 E=exit | -0.0808 | -0.0194 | -0.0794 |
| Q=all K=2 E=hold | -0.0807 | +0.0083 | -0.0763 |
| Q=all K=2 E=exit | -0.0935 | -0.0045 | -0.0800 |
| Q=top50 K=1 E=hold | -0.0885 | -0.0226 | -0.0951 |
| Q=top50 K=1 E=exit | -0.0952 | -0.0386 | -0.0933 |
| Q=top50 K=2 E=hold | -0.0836 | +0.0210 | -0.1213 |
| Q=top50 K=2 E=exit | -0.0817 | -0.0009 | -0.1180 |
| Q=top20 K=1 E=hold | -0.0959 | +0.0028 | -0.1567 |
| Q=top20 K=1 E=exit | -0.0993 | -0.0158 | -0.1513 |
| Q=top20 K=2 E=hold | -0.2116 | +0.0785 | -0.3357 |
| Q=top20 K=2 E=exit | -0.2510 | +0.0464 | -0.3260 |


## Monthly walk-forward (mean ROI/$ by entry month, main slippage)

| config | 2026-02 | 2026-03 | 2026-04 | 2026-05 | 2026-06 | 2026-07 | 2026-08 | 2026-09 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Q=all K=1 E=hold | +0.021 (619) | -0.020 (1436) | -0.046 (2003) | -0.058 (3348) | -0.081 (2933) | -0.054 (6960) | +0.000 (25931) | -0.003 (20701) |
| Q=all K=1 E=exit | +0.019 (619) | -0.024 (1436) | -0.041 (2003) | -0.064 (3348) | -0.083 (2933) | -0.060 (6960) | -0.008 (25931) | -0.008 (20701) |
| Q=all K=2 E=hold | -0.019 (79) | -0.028 (203) | -0.005 (325) | -0.050 (870) | -0.101 (845) | -0.054 (2608) | +0.008 (13475) | -0.002 (16867) |
| Q=all K=2 E=exit | +0.031 (79) | -0.045 (203) | +0.007 (325) | -0.077 (870) | -0.105 (845) | -0.060 (2608) | -0.002 (13475) | -0.007 (16867) |
| Q=top50 K=1 E=hold | +0.188 (229) | -0.049 (400) | +0.008 (479) | -0.055 (645) | -0.124 (919) | -0.095 (2326) | -0.024 (6302) | +0.028 (4095) |
| Q=top50 K=1 E=exit | +0.181 (229) | -0.048 (400) | -0.009 (479) | -0.070 (645) | -0.121 (919) | -0.093 (2326) | -0.039 (6302) | +0.018 (4095) |
| Q=top50 K=2 E=hold | -0.010 (13) | -0.478 (15) | +0.296 (33) | -0.056 (67) | -0.032 (132) | -0.063 (405) | -0.018 (2097) | +0.054 (2353) |
| Q=top50 K=2 E=exit | +0.045 (13) | -0.480 (15) | +0.294 (33) | -0.049 (67) | -0.028 (132) | -0.070 (405) | -0.036 (2097) | +0.042 (2353) |
| Q=top20 K=1 E=hold | +0.041 (98) | -0.005 (82) | +0.086 (79) | -0.115 (71) | -0.154 (307) | -0.070 (723) | -0.004 (2194) | +0.010 (2255) |
| Q=top20 K=1 E=exit | +0.046 (98) | -0.007 (82) | +0.040 (79) | -0.119 (71) | -0.131 (307) | -0.079 (723) | -0.021 (2194) | +0.001 (2255) |
| Q=top20 K=2 E=hold | — | — | +0.073 (1) | -0.413 (5) | -0.618 (15) | -0.033 (59) | -0.010 (435) | +0.023 (938) |
| Q=top20 K=2 E=exit | — | — | +0.061 (1) | -0.413 (5) | -0.692 (15) | -0.051 (59) | -0.030 (435) | +0.000 (938) |


## Diagnostics (per configuration; NEVER used to choose)

Each cell is `mean ROI/$ (n)` at the main slippage. `fade` buys the opposite outcome
at `1 − price + slippage` and holds to resolution (the exit rule does not apply to
a side the whale never traded).

### Q=all K=1 E=hold

| diagnostic | split | level | mean ROI/$ (n) |
| --- | --- | --- | --- |
| follow/fade | train | follow | -0.0572 (12863) |
| follow/fade | train | fade | -0.0247 (12863) |
| follow/fade | test | follow | +0.0117 (38960) |
| follow/fade | test | fade | -0.0945 (38960) |
| price band | train | 0.20-0.50 | -0.0660 (4199) |
| price band | train | 0.50-0.80 | -0.0509 (6328) |
| price band | train | <0.20 | -0.1667 (395) |
| price band | train | >0.80 | -0.0362 (1941) |
| price band | test | 0.20-0.50 | +0.0465 (12853) |
| price band | test | 0.50-0.80 | +0.0042 (17289) |
| price band | test | <0.20 | -0.0965 (1096) |
| price band | test | >0.80 | -0.0141 (7722) |
| sport | train | americanfootball_nfl | +0.3239 (13) |
| sport | train | baseball_kbo | -0.5744 (4) |
| sport | train | baseball_mlb | -0.0532 (1967) |
| sport | train | baseball_npb | -1.0150 (1) |
| sport | train | basketball_nba | -0.0901 (2393) |
| sport | train | basketball_wnba | -0.1017 (525) |
| sport | train | cricket | -0.0178 (93) |
| sport | train | crypto | -1.0333 (2) |
| sport | train | icehockey_nhl | -0.0210 (447) |
| sport | train | mma | +0.1641 (631) |
| sport | train | politics | -0.0814 (21) |
| sport | train | soccer | -0.0132 (1714) |
| sport | train | tennis | -0.0848 (5052) |
| sport | test | americanfootball_ncaaf | +0.1699 (168) |
| sport | test | americanfootball_nfl | -0.0333 (396) |
| sport | test | baseball_kbo | +0.0884 (64) |
| sport | test | baseball_mlb | +0.0260 (8848) |
| sport | test | baseball_npb | +0.2322 (16) |
| sport | test | basketball_fiba_womens | +0.0128 (3) |
| sport | test | basketball_nba | -0.1796 (21) |
| sport | test | basketball_wnba | -0.0387 (1006) |
| sport | test | cricket | -0.0584 (233) |
| sport | test | mma | -0.0247 (841) |
| sport | test | politics | +0.3438 (36) |
| sport | test | soccer | +0.0106 (12385) |
| sport | test | tennis | +0.0090 (14943) |
| timing | train | in-play | -0.0523 (6730) |
| timing | train | pregame | -0.0624 (6133) |
| timing | test | in-play | +0.0101 (19295) |
| timing | test | pregame | +0.0133 (19665) |
| notional decile | train | D0 | +0.0082 (1148) |
| notional decile | train | D1 | -0.1087 (1203) |
| notional decile | train | D2 | -0.0888 (1193) |
| notional decile | train | D3 | -0.0844 (1421) |
| notional decile | train | D4 | -0.0605 (1168) |
| notional decile | train | D5 | -0.0693 (1351) |
| notional decile | train | D6 | -0.0453 (1063) |
| notional decile | train | D7 | -0.0348 (1421) |
| notional decile | train | D8 | -0.0468 (1516) |
| notional decile | train | D9 | -0.0398 (1379) |
| notional decile | test | D0 | +0.0011 (3828) |
| notional decile | test | D1 | +0.0236 (3850) |
| notional decile | test | D2 | +0.0059 (3944) |
| notional decile | test | D3 | +0.0038 (3945) |
| notional decile | test | D4 | +0.0085 (4004) |
| notional decile | test | D5 | +0.0093 (3916) |
| notional decile | test | D6 | -0.0053 (3876) |
| notional decile | test | D7 | +0.0123 (3654) |
| notional decile | test | D8 | +0.0156 (3748) |
| notional decile | test | D9 | +0.0406 (4195) |


### Q=all K=1 E=exit

| diagnostic | split | level | mean ROI/$ (n) |
| --- | --- | --- | --- |
| follow/fade | train | follow | -0.0581 (12863) |
| follow/fade | train | fade | -0.0247 (12863) |
| follow/fade | test | follow | +0.0028 (38960) |
| follow/fade | test | fade | -0.0945 (38960) |
| price band | train | 0.20-0.50 | -0.0645 (4199) |
| price band | train | 0.50-0.80 | -0.0551 (6328) |
| price band | train | <0.20 | -0.1235 (395) |
| price band | train | >0.80 | -0.0406 (1941) |
| price band | test | 0.20-0.50 | +0.0274 (12853) |
| price band | test | 0.50-0.80 | -0.0005 (17289) |
| price band | test | <0.20 | -0.1038 (1096) |
| price band | test | >0.80 | -0.0154 (7722) |
| sport | train | americanfootball_nfl | +0.3239 (13) |
| sport | train | baseball_kbo | -0.5744 (4) |
| sport | train | baseball_mlb | -0.0493 (1967) |
| sport | train | baseball_npb | -1.0150 (1) |
| sport | train | basketball_nba | -0.0864 (2393) |
| sport | train | basketball_wnba | -0.1203 (525) |
| sport | train | cricket | +0.0102 (93) |
| sport | train | crypto | -1.0333 (2) |
| sport | train | icehockey_nhl | -0.0290 (447) |
| sport | train | mma | +0.1464 (631) |
| sport | train | politics | -0.0368 (21) |
| sport | train | soccer | -0.0191 (1714) |
| sport | train | tennis | -0.0843 (5052) |
| sport | test | americanfootball_ncaaf | +0.1728 (168) |
| sport | test | americanfootball_nfl | -0.0118 (396) |
| sport | test | baseball_kbo | +0.0730 (64) |
| sport | test | baseball_mlb | +0.0216 (8848) |
| sport | test | baseball_npb | +0.2270 (16) |
| sport | test | basketball_fiba_womens | +0.0128 (3) |
| sport | test | basketball_nba | -0.0983 (21) |
| sport | test | basketball_wnba | -0.0350 (1006) |
| sport | test | cricket | -0.0360 (233) |
| sport | test | mma | -0.0219 (841) |
| sport | test | politics | +0.2672 (36) |
| sport | test | soccer | +0.0034 (12385) |
| sport | test | tennis | -0.0068 (14943) |
| timing | train | in-play | -0.0575 (6730) |
| timing | train | pregame | -0.0587 (6133) |
| timing | test | in-play | -0.0029 (19295) |
| timing | test | pregame | +0.0085 (19665) |
| notional decile | train | D0 | +0.0118 (1148) |
| notional decile | train | D1 | -0.1109 (1203) |
| notional decile | train | D2 | -0.0886 (1193) |
| notional decile | train | D3 | -0.0860 (1421) |
| notional decile | train | D4 | -0.0703 (1168) |
| notional decile | train | D5 | -0.0631 (1351) |
| notional decile | train | D6 | -0.0533 (1063) |
| notional decile | train | D7 | -0.0351 (1421) |
| notional decile | train | D8 | -0.0425 (1516) |
| notional decile | train | D9 | -0.0444 (1379) |
| notional decile | test | D0 | -0.0008 (3828) |
| notional decile | test | D1 | +0.0144 (3850) |
| notional decile | test | D2 | -0.0077 (3944) |
| notional decile | test | D3 | +0.0007 (3945) |
| notional decile | test | D4 | +0.0048 (4004) |
| notional decile | test | D5 | +0.0061 (3916) |
| notional decile | test | D6 | -0.0182 (3876) |
| notional decile | test | D7 | +0.0070 (3654) |
| notional decile | test | D8 | +0.0047 (3748) |
| notional decile | test | D9 | +0.0167 (4195) |


### Q=all K=2 E=hold

| diagnostic | split | level | mean ROI/$ (n) |
| --- | --- | --- | --- |
| follow/fade | train | follow | -0.0616 (3258) |
| follow/fade | train | fade | +0.0066 (3258) |
| follow/fade | test | follow | +0.0288 (21420) |
| follow/fade | test | fade | -0.1300 (21420) |
| price band | train | 0.20-0.50 | -0.0582 (1067) |
| price band | train | 0.50-0.80 | -0.0523 (1488) |
| price band | train | <0.20 | -0.2416 (121) |
| price band | train | >0.80 | -0.0541 (582) |
| price band | test | 0.20-0.50 | +0.0707 (7041) |
| price band | test | 0.50-0.80 | +0.0126 (9454) |
| price band | test | <0.20 | +0.0537 (622) |
| price band | test | >0.80 | -0.0078 (4303) |
| sport | train | americanfootball_nfl | +0.2955 (4) |
| sport | train | baseball_mlb | -0.1144 (391) |
| sport | train | basketball_nba | -0.0146 (691) |
| sport | train | basketball_wnba | -0.0031 (94) |
| sport | train | cricket | -0.0148 (13) |
| sport | train | icehockey_nhl | +0.2106 (69) |
| sport | train | mma | +0.1297 (146) |
| sport | train | soccer | +0.0485 (244) |
| sport | train | tennis | -0.1195 (1606) |
| sport | test | americanfootball_ncaaf | +0.3520 (49) |
| sport | test | americanfootball_nfl | +0.0796 (190) |
| sport | test | baseball_kbo | +0.0452 (12) |
| sport | test | baseball_mlb | +0.0456 (5425) |
| sport | test | basketball_fiba_womens | +0.0143 (2) |
| sport | test | basketball_nba | -0.5159 (3) |
| sport | test | basketball_wnba | -0.0093 (312) |
| sport | test | cricket | -0.3384 (45) |
| sport | test | mma | -0.0509 (402) |
| sport | test | soccer | +0.0229 (5289) |
| sport | test | tennis | +0.0264 (9691) |
| timing | train | in-play | -0.0613 (2375) |
| timing | train | pregame | -0.0625 (883) |
| timing | test | in-play | +0.0267 (12701) |
| timing | test | pregame | +0.0318 (8719) |
| notional decile | train | D0 | +0.0057 (240) |
| notional decile | train | D1 | -0.0701 (278) |
| notional decile | train | D2 | -0.0874 (287) |
| notional decile | train | D3 | -0.1331 (341) |
| notional decile | train | D4 | -0.0485 (304) |
| notional decile | train | D5 | -0.0586 (408) |
| notional decile | train | D6 | +0.0289 (291) |
| notional decile | train | D7 | -0.0720 (393) |
| notional decile | train | D8 | -0.1022 (365) |
| notional decile | train | D9 | -0.0465 (351) |
| notional decile | test | D0 | +0.0159 (1825) |
| notional decile | test | D1 | +0.0413 (1926) |
| notional decile | test | D2 | +0.0335 (2024) |
| notional decile | test | D3 | +0.0140 (1901) |
| notional decile | test | D4 | +0.0268 (2193) |
| notional decile | test | D5 | +0.0246 (2061) |
| notional decile | test | D6 | +0.0222 (2212) |
| notional decile | test | D7 | +0.0142 (2154) |
| notional decile | test | D8 | +0.0453 (2350) |
| notional decile | test | D9 | +0.0426 (2774) |


### Q=all K=2 E=exit

| diagnostic | split | level | mean ROI/$ (n) |
| --- | --- | --- | --- |
| follow/fade | train | follow | -0.0697 (3258) |
| follow/fade | train | fade | +0.0066 (3258) |
| follow/fade | test | follow | +0.0186 (21420) |
| follow/fade | test | fade | -0.1300 (21420) |
| price band | train | 0.20-0.50 | -0.0735 (1067) |
| price band | train | 0.50-0.80 | -0.0666 (1488) |
| price band | train | <0.20 | -0.1566 (121) |
| price band | train | >0.80 | -0.0527 (582) |
| price band | test | 0.20-0.50 | +0.0475 (7041) |
| price band | test | 0.50-0.80 | +0.0080 (9454) |
| price band | test | <0.20 | +0.0518 (622) |
| price band | test | >0.80 | -0.0101 (4303) |
| sport | train | americanfootball_nfl | +0.2955 (4) |
| sport | train | baseball_mlb | -0.0969 (391) |
| sport | train | basketball_nba | -0.0208 (691) |
| sport | train | basketball_wnba | +0.0095 (94) |
| sport | train | cricket | +0.1215 (13) |
| sport | train | icehockey_nhl | +0.2067 (69) |
| sport | train | mma | +0.0634 (146) |
| sport | train | soccer | +0.0225 (244) |
| sport | train | tennis | -0.1292 (1606) |
| sport | test | americanfootball_ncaaf | +0.3633 (49) |
| sport | test | americanfootball_nfl | +0.0956 (190) |
| sport | test | baseball_kbo | +0.0391 (12) |
| sport | test | baseball_mlb | +0.0406 (5425) |
| sport | test | basketball_fiba_womens | +0.0143 (2) |
| sport | test | basketball_nba | -0.5159 (3) |
| sport | test | basketball_wnba | +0.0092 (312) |
| sport | test | cricket | -0.1915 (45) |
| sport | test | mma | -0.0444 (402) |
| sport | test | soccer | +0.0139 (5289) |
| sport | test | tennis | +0.0097 (9691) |
| timing | train | in-play | -0.0656 (2375) |
| timing | train | pregame | -0.0809 (883) |
| timing | test | in-play | +0.0116 (12701) |
| timing | test | pregame | +0.0287 (8719) |
| notional decile | train | D0 | -0.0199 (240) |
| notional decile | train | D1 | -0.0810 (278) |
| notional decile | train | D2 | -0.1043 (287) |
| notional decile | train | D3 | -0.1292 (341) |
| notional decile | train | D4 | -0.0565 (304) |
| notional decile | train | D5 | -0.0531 (408) |
| notional decile | train | D6 | +0.0042 (291) |
| notional decile | train | D7 | -0.0764 (393) |
| notional decile | train | D8 | -0.0982 (365) |
| notional decile | train | D9 | -0.0637 (351) |
| notional decile | test | D0 | +0.0097 (1825) |
| notional decile | test | D1 | +0.0364 (1926) |
| notional decile | test | D2 | +0.0236 (2024) |
| notional decile | test | D3 | +0.0081 (1901) |
| notional decile | test | D4 | +0.0208 (2193) |
| notional decile | test | D5 | +0.0192 (2061) |
| notional decile | test | D6 | +0.0086 (2212) |
| notional decile | test | D7 | +0.0038 (2154) |
| notional decile | test | D8 | +0.0318 (2350) |
| notional decile | test | D9 | +0.0219 (2774) |


### Q=top50 K=1 E=hold

| diagnostic | split | level | mean ROI/$ (n) |
| --- | --- | --- | --- |
| follow/fade | train | follow | -0.0696 (3757) |
| follow/fade | train | fade | -0.0584 (3757) |
| follow/fade | test | follow | -0.0020 (10356) |
| follow/fade | test | fade | -0.0899 (10356) |
| price band | train | 0.20-0.50 | -0.0884 (1427) |
| price band | train | 0.50-0.80 | -0.0351 (1792) |
| price band | train | <0.20 | -0.5226 (133) |
| price band | train | >0.80 | -0.0071 (405) |
| price band | test | 0.20-0.50 | +0.0282 (3972) |
| price band | test | 0.50-0.80 | -0.0094 (4979) |
| price band | test | <0.20 | -0.2589 (322) |
| price band | test | >0.80 | -0.0019 (1083) |
| sport | train | americanfootball_nfl | +0.4591 (10) |
| sport | train | baseball_mlb | -0.1106 (616) |
| sport | train | baseball_npb | -1.0150 (1) |
| sport | train | basketball_nba | -0.1092 (573) |
| sport | train | basketball_wnba | -0.1888 (246) |
| sport | train | cricket | +0.1817 (13) |
| sport | train | crypto | -1.0333 (2) |
| sport | train | icehockey_nhl | +0.0180 (178) |
| sport | train | mma | +0.1989 (332) |
| sport | train | soccer | -0.0244 (274) |
| sport | train | tennis | -0.0997 (1512) |
| sport | test | americanfootball_ncaaf | +0.0761 (17) |
| sport | test | americanfootball_nfl | +0.1156 (25) |
| sport | test | baseball_kbo | +0.2538 (23) |
| sport | test | baseball_mlb | +0.0141 (2719) |
| sport | test | baseball_npb | -0.2114 (5) |
| sport | test | basketball_nba | -0.1848 (6) |
| sport | test | basketball_wnba | -0.0893 (279) |
| sport | test | cricket | +0.0980 (16) |
| sport | test | mma | -0.0619 (157) |
| sport | test | politics | +0.3511 (22) |
| sport | test | soccer | -0.0258 (1851) |
| sport | test | tennis | +0.0012 (5236) |
| timing | train | in-play | -0.0726 (2076) |
| timing | train | pregame | -0.0659 (1681) |
| timing | test | in-play | -0.0142 (4630) |
| timing | test | pregame | +0.0079 (5726) |
| notional decile | train | D0 | +0.1453 (282) |
| notional decile | train | D1 | -0.1393 (365) |
| notional decile | train | D2 | -0.0927 (336) |
| notional decile | train | D3 | -0.0627 (492) |
| notional decile | train | D4 | -0.0487 (332) |
| notional decile | train | D5 | -0.1513 (450) |
| notional decile | train | D6 | -0.1063 (321) |
| notional decile | train | D7 | -0.0495 (471) |
| notional decile | train | D8 | -0.1101 (392) |
| notional decile | train | D9 | -0.0150 (316) |
| notional decile | test | D0 | -0.0006 (846) |
| notional decile | test | D1 | -0.0401 (904) |
| notional decile | test | D2 | -0.0105 (968) |
| notional decile | test | D3 | +0.0052 (1173) |
| notional decile | test | D4 | +0.0123 (1146) |
| notional decile | test | D5 | +0.0082 (1189) |
| notional decile | test | D6 | -0.0434 (1046) |
| notional decile | test | D7 | +0.0150 (947) |
| notional decile | test | D8 | +0.0280 (954) |
| notional decile | test | D9 | +0.0007 (1183) |


### Q=top50 K=1 E=exit

| diagnostic | split | level | mean ROI/$ (n) |
| --- | --- | --- | --- |
| follow/fade | train | follow | -0.0716 (3757) |
| follow/fade | train | fade | -0.0584 (3757) |
| follow/fade | test | follow | -0.0155 (10356) |
| follow/fade | test | fade | -0.0899 (10356) |
| price band | train | 0.20-0.50 | -0.0834 (1427) |
| price band | train | 0.50-0.80 | -0.0456 (1792) |
| price band | train | <0.20 | -0.4573 (133) |
| price band | train | >0.80 | -0.0186 (405) |
| price band | test | 0.20-0.50 | -0.0043 (3972) |
| price band | test | 0.50-0.80 | -0.0100 (4979) |
| price band | test | <0.20 | -0.2704 (322) |
| price band | test | >0.80 | -0.0060 (1083) |
| sport | train | americanfootball_nfl | +0.4591 (10) |
| sport | train | baseball_mlb | -0.0996 (616) |
| sport | train | baseball_npb | -1.0150 (1) |
| sport | train | basketball_nba | -0.1097 (573) |
| sport | train | basketball_wnba | -0.1900 (246) |
| sport | train | cricket | +0.1817 (13) |
| sport | train | crypto | -1.0333 (2) |
| sport | train | icehockey_nhl | +0.0123 (178) |
| sport | train | mma | +0.1789 (332) |
| sport | train | soccer | -0.0394 (274) |
| sport | train | tennis | -0.1011 (1512) |
| sport | test | americanfootball_ncaaf | +0.0715 (17) |
| sport | test | americanfootball_nfl | +0.1352 (25) |
| sport | test | baseball_kbo | +0.2507 (23) |
| sport | test | baseball_mlb | +0.0135 (2719) |
| sport | test | baseball_npb | -0.2202 (5) |
| sport | test | basketball_nba | -0.0276 (6) |
| sport | test | basketball_wnba | -0.0836 (279) |
| sport | test | cricket | +0.0980 (16) |
| sport | test | mma | -0.0558 (157) |
| sport | test | politics | +0.3075 (22) |
| sport | test | soccer | -0.0225 (1851) |
| sport | test | tennis | -0.0269 (5236) |
| timing | train | in-play | -0.0792 (2076) |
| timing | train | pregame | -0.0623 (1681) |
| timing | test | in-play | -0.0349 (4630) |
| timing | test | pregame | +0.0002 (5726) |
| notional decile | train | D0 | +0.1224 (282) |
| notional decile | train | D1 | -0.1472 (365) |
| notional decile | train | D2 | -0.0898 (336) |
| notional decile | train | D3 | -0.0787 (492) |
| notional decile | train | D4 | -0.0650 (332) |
| notional decile | train | D5 | -0.1194 (450) |
| notional decile | train | D6 | -0.1132 (321) |
| notional decile | train | D7 | -0.0390 (471) |
| notional decile | train | D8 | -0.1178 (392) |
| notional decile | train | D9 | -0.0153 (316) |
| notional decile | test | D0 | +0.0069 (846) |
| notional decile | test | D1 | -0.0410 (904) |
| notional decile | test | D2 | -0.0127 (968) |
| notional decile | test | D3 | -0.0054 (1173) |
| notional decile | test | D4 | +0.0034 (1146) |
| notional decile | test | D5 | -0.0029 (1189) |
| notional decile | test | D6 | -0.0672 (1046) |
| notional decile | test | D7 | -0.0032 (947) |
| notional decile | test | D8 | +0.0083 (954) |
| notional decile | test | D9 | -0.0390 (1183) |


### Q=top50 K=2 E=hold

| diagnostic | split | level | mean ROI/$ (n) |
| --- | --- | --- | --- |
| follow/fade | train | follow | -0.0625 (485) |
| follow/fade | train | fade | -0.0277 (485) |
| follow/fade | test | follow | +0.0437 (3850) |
| follow/fade | test | fade | -0.1184 (3850) |
| price band | train | 0.20-0.50 | -0.1814 (210) |
| price band | train | 0.50-0.80 | +0.0773 (212) |
| price band | train | <0.20 | -0.2931 (22) |
| price band | train | >0.80 | -0.0529 (41) |
| price band | test | 0.20-0.50 | +0.0851 (1530) |
| price band | test | 0.50-0.80 | +0.0146 (1768) |
| price band | test | <0.20 | +0.0704 (130) |
| price band | test | >0.80 | +0.0076 (422) |
| sport | train | americanfootball_nfl | +0.2711 (2) |
| sport | train | baseball_mlb | -0.3547 (82) |
| sport | train | basketball_nba | -0.0026 (126) |
| sport | train | basketball_wnba | -0.2280 (6) |
| sport | train | icehockey_nhl | +0.6302 (2) |
| sport | train | mma | -0.0295 (38) |
| sport | train | soccer | -0.2698 (13) |
| sport | train | tennis | +0.0152 (216) |
| sport | test | americanfootball_ncaaf | -1.0090 (1) |
| sport | test | americanfootball_nfl | +1.0264 (2) |
| sport | test | baseball_kbo | +0.7581 (2) |
| sport | test | baseball_mlb | +0.0810 (1079) |
| sport | test | basketball_nba | +0.4750 (1) |
| sport | test | basketball_wnba | -0.6514 (13) |
| sport | test | mma | -0.5390 (26) |
| sport | test | soccer | -0.0559 (369) |
| sport | test | tennis | +0.0514 (2357) |
| timing | train | in-play | -0.0586 (374) |
| timing | train | pregame | -0.0759 (111) |
| timing | test | in-play | +0.0419 (2239) |
| timing | test | pregame | +0.0464 (1611) |
| notional decile | train | D0 | -0.2826 (34) |
| notional decile | train | D1 | -0.0907 (45) |
| notional decile | train | D2 | +0.2438 (37) |
| notional decile | train | D3 | -0.0422 (60) |
| notional decile | train | D4 | +0.0320 (45) |
| notional decile | train | D5 | -0.1119 (68) |
| notional decile | train | D6 | -0.1079 (50) |
| notional decile | train | D7 | -0.1002 (69) |
| notional decile | train | D8 | -0.0431 (45) |
| notional decile | train | D9 | -0.0845 (32) |
| notional decile | test | D0 | -0.0402 (263) |
| notional decile | test | D1 | +0.0296 (313) |
| notional decile | test | D2 | +0.1218 (369) |
| notional decile | test | D3 | +0.0514 (377) |
| notional decile | test | D4 | +0.0703 (417) |
| notional decile | test | D5 | +0.0377 (445) |
| notional decile | test | D6 | -0.0237 (379) |
| notional decile | test | D7 | +0.0671 (364) |
| notional decile | test | D8 | +0.0896 (421) |
| notional decile | test | D9 | +0.0122 (502) |


### Q=top50 K=2 E=exit

| diagnostic | split | level | mean ROI/$ (n) |
| --- | --- | --- | --- |
| follow/fade | train | follow | -0.0554 (485) |
| follow/fade | train | fade | -0.0277 (485) |
| follow/fade | test | follow | +0.0246 (3850) |
| follow/fade | test | fade | -0.1184 (3850) |
| price band | train | 0.20-0.50 | -0.1402 (210) |
| price band | train | 0.50-0.80 | +0.0464 (212) |
| price band | train | <0.20 | -0.2536 (22) |
| price band | train | >0.80 | -0.0409 (41) |
| price band | test | 0.20-0.50 | +0.0379 (1530) |
| price band | test | 0.50-0.80 | +0.0108 (1768) |
| price band | test | <0.20 | +0.1266 (130) |
| price band | test | >0.80 | +0.0028 (422) |
| sport | train | americanfootball_nfl | +0.2711 (2) |
| sport | train | baseball_mlb | -0.2926 (82) |
| sport | train | basketball_nba | -0.0191 (126) |
| sport | train | basketball_wnba | -0.3331 (6) |
| sport | train | icehockey_nhl | +0.6192 (2) |
| sport | train | mma | +0.0013 (38) |
| sport | train | soccer | -0.2552 (13) |
| sport | train | tennis | +0.0140 (216) |
| sport | test | americanfootball_ncaaf | -1.0090 (1) |
| sport | test | americanfootball_nfl | +1.0264 (2) |
| sport | test | baseball_kbo | +0.7386 (2) |
| sport | test | baseball_mlb | +0.0872 (1079) |
| sport | test | basketball_nba | +0.4750 (1) |
| sport | test | basketball_wnba | -0.6514 (13) |
| sport | test | mma | -0.5390 (26) |
| sport | test | soccer | -0.0458 (369) |
| sport | test | tennis | +0.0157 (2357) |
| timing | train | in-play | -0.0448 (374) |
| timing | train | pregame | -0.0911 (111) |
| timing | test | in-play | +0.0117 (2239) |
| timing | test | pregame | +0.0426 (1611) |
| notional decile | train | D0 | -0.2951 (34) |
| notional decile | train | D1 | -0.0702 (45) |
| notional decile | train | D2 | +0.2094 (37) |
| notional decile | train | D3 | -0.0304 (60) |
| notional decile | train | D4 | +0.0158 (45) |
| notional decile | train | D5 | -0.0482 (68) |
| notional decile | train | D6 | -0.0983 (50) |
| notional decile | train | D7 | -0.1055 (69) |
| notional decile | train | D8 | -0.0460 (45) |
| notional decile | train | D9 | -0.0866 (32) |
| notional decile | test | D0 | -0.0182 (263) |
| notional decile | test | D1 | +0.0194 (313) |
| notional decile | test | D2 | +0.1189 (369) |
| notional decile | test | D3 | +0.0346 (377) |
| notional decile | test | D4 | +0.0715 (417) |
| notional decile | test | D5 | +0.0240 (445) |
| notional decile | test | D6 | -0.0525 (379) |
| notional decile | test | D7 | +0.0193 (364) |
| notional decile | test | D8 | +0.0527 (421) |
| notional decile | test | D9 | -0.0265 (502) |


### Q=top20 K=1 E=hold

| diagnostic | split | level | mean ROI/$ (n) |
| --- | --- | --- | --- |
| follow/fade | train | follow | -0.0781 (1026) |
| follow/fade | train | fade | -0.0417 (1026) |
| follow/fade | test | follow | +0.0245 (4067) |
| follow/fade | test | fade | -0.1141 (4067) |
| price band | train | 0.20-0.50 | -0.1322 (412) |
| price band | train | 0.50-0.80 | +0.0394 (479) |
| price band | train | <0.20 | -0.9063 (42) |
| price band | train | >0.80 | -0.0695 (93) |
| price band | test | 0.20-0.50 | +0.0409 (1701) |
| price band | test | 0.50-0.80 | +0.0074 (1752) |
| price band | test | <0.20 | +0.0730 (118) |
| price band | test | >0.80 | +0.0171 (496) |
| sport | train | americanfootball_nfl | -1.0150 (1) |
| sport | train | baseball_mlb | -0.0033 (173) |
| sport | train | basketball_nba | -0.3320 (117) |
| sport | train | basketball_wnba | -0.1467 (116) |
| sport | train | cricket | +0.6459 (5) |
| sport | train | icehockey_nhl | +0.4662 (21) |
| sport | train | mma | +0.2177 (124) |
| sport | train | soccer | +0.1182 (37) |
| sport | train | tennis | -0.1552 (432) |
| sport | test | americanfootball_ncaaf | +0.4679 (7) |
| sport | test | americanfootball_nfl | +0.0244 (6) |
| sport | test | baseball_kbo | +0.4692 (13) |
| sport | test | baseball_mlb | +0.0372 (1297) |
| sport | test | basketball_nba | +0.9547 (1) |
| sport | test | basketball_wnba | -0.1807 (98) |
| sport | test | cricket | -0.5434 (8) |
| sport | test | mma | +0.2304 (49) |
| sport | test | soccer | -0.0010 (625) |
| sport | test | tennis | +0.0267 (1963) |
| timing | train | in-play | -0.1634 (503) |
| timing | train | pregame | +0.0040 (523) |
| timing | test | in-play | +0.0107 (1371) |
| timing | test | pregame | +0.0315 (2696) |
| notional decile | train | D0 | +0.2179 (40) |
| notional decile | train | D1 | -0.0886 (79) |
| notional decile | train | D2 | -0.1297 (64) |
| notional decile | train | D3 | -0.0736 (116) |
| notional decile | train | D4 | -0.1323 (88) |
| notional decile | train | D5 | -0.2181 (151) |
| notional decile | train | D6 | +0.0817 (104) |
| notional decile | train | D7 | -0.0811 (126) |
| notional decile | train | D8 | -0.1593 (122) |
| notional decile | train | D9 | +0.0055 (136) |
| notional decile | test | D0 | +0.0179 (328) |
| notional decile | test | D1 | -0.0662 (323) |
| notional decile | test | D2 | +0.0743 (392) |
| notional decile | test | D3 | +0.0181 (429) |
| notional decile | test | D4 | +0.0340 (518) |
| notional decile | test | D5 | +0.0572 (540) |
| notional decile | test | D6 | -0.0104 (485) |
| notional decile | test | D7 | +0.0036 (363) |
| notional decile | test | D8 | +0.0326 (381) |
| notional decile | test | D9 | +0.0681 (308) |


### Q=top20 K=1 E=exit

| diagnostic | split | level | mean ROI/$ (n) |
| --- | --- | --- | --- |
| follow/fade | train | follow | -0.0759 (1026) |
| follow/fade | train | fade | -0.0417 (1026) |
| follow/fade | test | follow | +0.0074 (4067) |
| follow/fade | test | fade | -0.1141 (4067) |
| price band | train | 0.20-0.50 | -0.1148 (412) |
| price band | train | 0.50-0.80 | +0.0277 (479) |
| price band | train | <0.20 | -0.8726 (42) |
| price band | train | >0.80 | -0.0771 (93) |
| price band | test | 0.20-0.50 | +0.0115 (1701) |
| price band | test | 0.50-0.80 | +0.0006 (1752) |
| price band | test | <0.20 | +0.0329 (118) |
| price band | test | >0.80 | +0.0113 (496) |
| sport | train | americanfootball_nfl | -1.0150 (1) |
| sport | train | baseball_mlb | -0.0213 (173) |
| sport | train | basketball_nba | -0.3232 (117) |
| sport | train | basketball_wnba | -0.1508 (116) |
| sport | train | cricket | +0.6459 (5) |
| sport | train | icehockey_nhl | +0.4581 (21) |
| sport | train | mma | +0.2154 (124) |
| sport | train | soccer | +0.1171 (37) |
| sport | train | tennis | -0.1429 (432) |
| sport | test | americanfootball_ncaaf | +0.4679 (7) |
| sport | test | americanfootball_nfl | +0.0205 (6) |
| sport | test | baseball_kbo | +0.4605 (13) |
| sport | test | baseball_mlb | +0.0345 (1297) |
| sport | test | basketball_nba | +0.9330 (1) |
| sport | test | basketball_wnba | -0.2141 (98) |
| sport | test | cricket | -0.5434 (8) |
| sport | test | mma | +0.2300 (49) |
| sport | test | soccer | +0.0079 (625) |
| sport | test | tennis | -0.0081 (1963) |
| timing | train | in-play | -0.1637 (503) |
| timing | train | pregame | +0.0086 (523) |
| timing | test | in-play | -0.0230 (1371) |
| timing | test | pregame | +0.0228 (2696) |
| notional decile | train | D0 | +0.2151 (40) |
| notional decile | train | D1 | -0.0859 (79) |
| notional decile | train | D2 | -0.1439 (64) |
| notional decile | train | D3 | -0.0803 (116) |
| notional decile | train | D4 | -0.1271 (88) |
| notional decile | train | D5 | -0.1395 (151) |
| notional decile | train | D6 | -0.0001 (104) |
| notional decile | train | D7 | -0.0823 (126) |
| notional decile | train | D8 | -0.1592 (122) |
| notional decile | train | D9 | +0.0068 (136) |
| notional decile | test | D0 | +0.0165 (328) |
| notional decile | test | D1 | -0.0945 (323) |
| notional decile | test | D2 | +0.0719 (392) |
| notional decile | test | D3 | -0.0042 (429) |
| notional decile | test | D4 | +0.0185 (518) |
| notional decile | test | D5 | +0.0318 (540) |
| notional decile | test | D6 | -0.0580 (485) |
| notional decile | test | D7 | -0.0002 (363) |
| notional decile | test | D8 | +0.0301 (381) |
| notional decile | test | D9 | +0.0606 (308) |


### Q=top20 K=2 E=hold

| diagnostic | split | level | mean ROI/$ (n) |
| --- | --- | --- | --- |
| follow/fade | train | follow | -0.1919 (56) |
| follow/fade | train | fade | +0.1701 (56) |
| follow/fade | test | follow | +0.1045 (1089) |
| follow/fade | test | fade | -0.1499 (1089) |
| price band | train | 0.20-0.50 | -0.1450 (32) |
| price band | train | 0.50-0.80 | -0.1750 (18) |
| price band | train | <0.20 | -1.0260 (3) |
| price band | train | >0.80 | +0.0395 (3) |
| price band | test | 0.20-0.50 | +0.1524 (512) |
| price band | test | 0.50-0.80 | +0.0362 (434) |
| price band | test | <0.20 | +0.5196 (34) |
| price band | test | >0.80 | +0.0216 (109) |
| sport | train | baseball_mlb | +0.0230 (16) |
| sport | train | basketball_nba | -0.6096 (17) |
| sport | train | tennis | -0.0328 (23) |
| sport | test | americanfootball_nfl | +1.0677 (1) |
| sport | test | baseball_mlb | +0.1209 (426) |
| sport | test | basketball_wnba | +0.9850 (1) |
| sport | test | mma | -1.0078 (2) |
| sport | test | soccer | -0.1648 (72) |
| sport | test | tennis | +0.1262 (587) |
| timing | train | in-play | -0.3337 (38) |
| timing | train | pregame | +0.1074 (18) |
| timing | test | in-play | +0.1780 (455) |
| timing | test | pregame | +0.0517 (634) |
| notional decile | train | D0 | -0.0071 (3) |
| notional decile | train | D1 | -0.1613 (10) |
| notional decile | train | D2 | -0.1389 (7) |
| notional decile | train | D3 | -0.2288 (6) |
| notional decile | train | D4 | -0.3776 (6) |
| notional decile | train | D5 | -0.6951 (6) |
| notional decile | train | D6 | +0.2987 (2) |
| notional decile | train | D7 | +0.1458 (7) |
| notional decile | train | D8 | +0.2052 (5) |
| notional decile | train | D9 | -0.7441 (4) |
| notional decile | test | D0 | +0.0915 (64) |
| notional decile | test | D1 | +0.0216 (80) |
| notional decile | test | D2 | +0.2452 (131) |
| notional decile | test | D3 | +0.0860 (117) |
| notional decile | test | D4 | -0.1036 (114) |
| notional decile | test | D5 | +0.1591 (143) |
| notional decile | test | D6 | +0.0832 (128) |
| notional decile | test | D7 | -0.0097 (102) |
| notional decile | test | D8 | +0.1893 (119) |
| notional decile | test | D9 | +0.2293 (91) |


### Q=top20 K=2 E=exit

| diagnostic | split | level | mean ROI/$ (n) |
| --- | --- | --- | --- |
| follow/fade | train | follow | -0.2276 (56) |
| follow/fade | train | fade | +0.1701 (56) |
| follow/fade | test | follow | +0.0741 (1089) |
| follow/fade | test | fade | -0.1499 (1089) |
| price band | train | 0.20-0.50 | -0.1490 (32) |
| price band | train | 0.50-0.80 | -0.2780 (18) |
| price band | train | <0.20 | -1.0260 (3) |
| price band | train | >0.80 | +0.0356 (3) |
| price band | test | 0.20-0.50 | +0.1002 (512) |
| price band | test | 0.50-0.80 | +0.0217 (434) |
| price band | test | <0.20 | +0.5179 (34) |
| price band | test | >0.80 | +0.0213 (109) |
| sport | train | baseball_mlb | -0.0370 (16) |
| sport | train | basketball_nba | -0.6746 (17) |
| sport | train | tennis | -0.0297 (23) |
| sport | test | americanfootball_nfl | +1.0677 (1) |
| sport | test | baseball_mlb | +0.1109 (426) |
| sport | test | basketball_wnba | +0.9850 (1) |
| sport | test | mma | -1.0078 (2) |
| sport | test | soccer | -0.1659 (72) |
| sport | test | tennis | +0.0772 (587) |
| timing | train | in-play | -0.3594 (38) |
| timing | train | pregame | +0.0507 (18) |
| timing | test | in-play | +0.1151 (455) |
| timing | test | pregame | +0.0446 (634) |
| notional decile | train | D0 | -0.0071 (3) |
| notional decile | train | D1 | -0.2760 (10) |
| notional decile | train | D2 | -0.2797 (7) |
| notional decile | train | D3 | -0.2356 (6) |
| notional decile | train | D4 | -0.3776 (6) |
| notional decile | train | D5 | -0.6598 (6) |
| notional decile | train | D6 | +0.2724 (2) |
| notional decile | train | D7 | +0.1502 (7) |
| notional decile | train | D8 | +0.2052 (5) |
| notional decile | train | D9 | -0.7471 (4) |
| notional decile | test | D0 | +0.0889 (64) |
| notional decile | test | D1 | -0.0221 (80) |
| notional decile | test | D2 | +0.2347 (131) |
| notional decile | test | D3 | +0.0347 (117) |
| notional decile | test | D4 | -0.1131 (114) |
| notional decile | test | D5 | +0.1139 (143) |
| notional decile | test | D6 | +0.0096 (128) |
| notional decile | test | D7 | -0.0407 (102) |
| notional decile | test | D8 | +0.1756 (119) |
| notional decile | test | D9 | +0.2258 (91) |


## Known limitations (verbatim from the plan)

1. Selection bias: the 575 backfilled wallets were discovered by large recent prints; wallets
   that blew up and stopped trading are absent. The baseline (Q = all) carries the same bias, so
   the DIFFERENCE between Q levels is the honest read of skill ranking, not the absolute ROI.
2. Slippage is an assumption; there are no historical quotes. The lane's own 120 s markouts
   (mean drift ≈ 0) are the only calibration.
3. Depth is ignored (the lane's depth gate is not simulated).
4. Outcome P&L only; no mark-to-market.
