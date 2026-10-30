# Kalshi MLB correlated-market mispricing: REPORT (paper only)

**Verdict: DEAD.**
- The test asked whether player props (HR, hits, strikeouts) are mispriced relative to the game distribution implied by their sibling game markets. After fees and spread, they are not tradably mispriced.
- On the sealed confirmation set (2026-08-30 → 09-27, 377 games), the primary rule lost **−$240 on 744 fills (−9.0¢ per $ risked, one-sided p = 0.94)**. The secondary v1 model lost −$256 (p = 0.97).
- The market's Brier score is at least as good as the model's in every prop family.
- Combos (edge type 2) were not scorable: quotes are private RFQs and same-game MLB combos are rare.

Specs: `PREREG_MLB_CORR_v1.md` (sha256 `c2f1b6ac…a04431`) and the `PREREG_MLB_CORR_v2.md` amendment (`373c6d81…cbb644`). Both were frozen before any confirmation price was read; the freeze line is in `CONFIRM_OPENED.md` (2026-10-29 21:45 ET). Phase-0 facts are in `PHASE0.md`.

## 1. Method
- **Joint model** (`arb/mlb/sim.py`):
  - A plate-appearance Monte Carlo with outcomes K/BB/1B/2B/3B/HR/out.
  - Batter and pitcher rates are empirical-Bayes shrunk (2025 plus 2026 through the day before), then combined by log5, with 2025 park factors.
  - The starter faces batters up to his expected count, then the bullpen takes over.
  - It handles the home team not batting in the bottom 9th, walk-offs, and the extra-innings runner.
  - Parameter uncertainty comes from Dirichlet draws per chunk (20 chunks × 1,000 sims).
- **Market-relative fair value** (`arb/mlb/tilt.py`):
  - The simulated games are reweighted so their run distribution matches all anchor mids. Anchors are winner, total ladder, spread ladder and team-total ladders.
  - Props are then read off the same weighted simulations. The correlation structure comes from the model and the run level from the market.
- **Signal** (`arb/mlb/lane.py`):
  - Decision time is first pitch − 30 min. Quotes are 1-minute candles from [T−10, T]; stale, crossed or one-sided books are discarded.
  - The trade is buy YES if the interval's lower bound exceeds ask + fee, or buy NO if the bid exceeds the upper bound + fee.
  - Entry is the worse of the T and T+1-minute quotes (latency).
  - Fee is the Kalshi taker fee, ceil-to-cent on the whole order, with the series multiplier: 1 before 2026-08-07, 0.5 after.
- **Fill:** 10 contracts, capped by the volume traded at or through our price in the next 10 minutes. There are never fills at mid.
- **Unit of inference:** the game. All fills in one game form one bet, scored by game-cluster bootstrap.

## 2. Data and its limits
| item | what we had |
|---|---|
| Kalshi MLB props | HR, HIT, KS, TB, HRR from 2026-03-25; RBI from 06-22. Totals and spreads from the 2025 postseason; winner from 2025-04. |
| Prices | 1-minute candles (best bid/ask, last-trade OHLC, volume). **There is no historical order-book depth.** The fill size is a proxy built from trade prints. |
| Confirmation pull | 44,718 markets, 645 batched calls |
| Discovery pull | 26 of the 300 drawn games. The pull was paused to free the shared call budget; discovery is descriptive only. |
| Kalshi calls | 4,457 public GETs in total. No 429s, nothing in the quiet windows, every call logged. |
| MLB data | 366,775 Statcast plate appearances (2025–26), 2,469 boxscores, MLB Stats API schedule |
| Combos | Only a cross-category combo collection. 4 of 63 sampled combos had two or more legs from the same MLB game. Quotes are private (authenticated RFQ). |
| Season | Over on 2026-10-28. A forward leg is impossible before 2027, so the best verdict available was SOFT. |

## 3. Calibration (outcomes only, 1,964 discovery-range games)
| marginal | base rate | model mean | Brier model | Brier naive | Brier climatology |
|---|---|---|---|---|---|
| HR 1+ | .115 | .124 | .1003 | .1002 | .1015 |
| HIT 1+ | .607 | .641 | .2369 | .2363 | .2386 |
| HIT 2+ | .215 | .243 | .1673 | .1676 | .1687 |
| KS 5+ | .500 | .523 | .2214 | .2271 | .2500 |
| team runs ≥ 5 | .428 | .462 | .2447 | — | .2448 |
| total > 8.5 | .484 | .534 | .2498 | — | .2497 |
| home win | .527 | .500 | .2465 | — | .2493 |

- **Bias:** v1 ran hot on runs and hits. Team-level marginals carry almost no skill over climatology, which is expected without lineup-to-lineup context beyond rates.
- **Conditionals, the part that matters for correlation:**

| conditional | empirical | model | independence |
|---|---|---|---|
| P(team ≥ 5 runs \| a player homers) | .646 | .673 | .471 |
| P(opponent ≥ 5 \| starter 7+ K) | .260 | .345 | .446 |

  - The model captures most of the HR ↔ runs coupling.
  - It under-captures the strikeout ↔ runs coupling.
- **v2** (fit on outcomes only, `fit_v2.json`) added a −0.05 run-level shift and crediting of substitutes' plate appearances. Day-form random effects added no Brier skill and were left out, so the K ↔ runs weakness remained.

## 4. Results
### Discovery (26 games, descriptive)
| | v1 | v2 |
|---|---|---|
| fills / signals | 29 / 101 | 32 / 82 |
| P&L | −$14.67 | −$31.02 |
| hit rate vs predicted | .41 vs .54 | .38 vs .59 |

### Confirmation (377 games with quotes, 21,074 target quotes): primary v2
| | signals | fills | games | P&L | per $ | hit vs pred | one-sided p |
|---|---|---|---|---|---|---|---|
| **all** | 2,103 | 744 (35%) | 249 | **−$240.26** | −9.0¢ | .366 vs .526 | 0.94 |
| HR | 202 | 85 | 65 | +$23.92 | +20.4¢ | .188 vs .206 | 0.23 |
| HIT | 240 | 33 | 31 | +$31.89 | +27.5¢ | .515 vs .522 | 0.08 |
| KS | 1,661 | 626 | 215 | −$296.06 | −12.2¢ | .382 vs .569 | 0.97 |

- Folds (chronological): −183.7 / −0.7 / −36.7 / −19.1, so 0 of 4 are positive.
- Max drawdown: $217.
- Top 5% of games: 37% of positive P&L.
- Gates: E1 fail, E2 fail, E3 fail (KS carries 57% of positive P&L).
- Prices near 0 or 1 (mid ≤ .10 or ≥ .90): 119 fills, +$30.4. Treat this as noise: tick size and fees dominate there.

**Secondary v1:** −$255.79 on 687 fills (p = 0.97). HR −$18, HIT +$37, KS −$274.

**Market sanity** (all quoted targets, confirmation):
| family | Brier market | Brier model (untilted) | Brier model (anchored) |
|---|---|---|---|
| HIT (n = 10,016) | .2027 | .2032 | .2031 |
| HR (n = 6,781) | .0982 | .0989 | .0987 |
| KS (n = 4,199) | .1670 | .1800 | .1792 (*model-suspect*) |

### Null tests (confirmation, v2)
| null | fills | P&L | reading |
|---|---|---|---|
| N1, shuffled anchors (same date) | 972 | −$263 | about the same loss: signals come from model error, not from information in sibling prices |
| N2, independence fair | 4,451 | −$179 | also loses, less per $ (−1.8¢) |
| N3, sign flip | 119 | +$41.5 (p = 0.24) | the opposite side does slightly better, consistent with the model being the mispriced party |

## 5. Interpretation
- **The market is the better forecaster.** Kalshi prop prices are about as well calibrated as a reasonable plate-appearance model, and on strikeouts they are better.
- **Strikeout signals are the model's error.** The model's weak K ↔ runs coupling made KS overs look cheap whenever the market priced the opponent's runs low. That produced 79% of all signals, and they lost.
- **HR and hit props are not significant.** They were slightly positive under v2 (n = 85 and 33 fills, p = 0.23 and 0.08, against a bar of p < 0.0167). That is consistent with zero edge. It is not a finding, and it must not be re-scored on this data.
- **Liquidity is thin.** Only 35% of signals would have filled at all, even under a lenient fill proxy.

## 6. Edge cases: how each was handled
- **Player not in the starting lineup, scratched, or no plate appearance:** Kalshi settles at "fair price", so the market is excluded. 5,608 fair-price markets were dropped.
- **Postponed, suspended or resumed games, and games shortened below 9 innings:** excluded via MLB Stats API status.
- **Doubleheaders:** matched by ticker start time. 35 ambiguous events were dropped.
- **Extra innings:** count toward totals and spreads (rulebook). The simulator plays them with the regular-season automatic runner.
- **Stat corrections after settlement:** Kalshi says they do not change settlement, so the P&L uses Kalshi's `result`.
- **Openers:** the starter's expected batters faced comes from his own 2026 history, so openers exit early. KS props for anyone other than the actual starter are excluded.
- **Lookahead:** model inputs only use data before game day. The lineup is the actual one (disclosed leak, §4 v1).
- **Correlated exposure:** all fills in one game are one unit.
- **Multiple testing:** α/3 across three families, plus the sealed confirmation set.

## 7. Deviations (all disclosed in v2 or here)
1. Discovery used 26 games, not 300 (pull paused for budget).
2. The simulator changed to v2 (outcome calibration). The day-form latents were tested and rejected.
3. Code bug fixed after game 1: props never matched a team because of a NaN-truthy test.
4. Null N1 has no discovery reading because the random draw gave few same-date games.
5. The H2 model-vs-trade table was not built: only 4 same-game combos were found, and quotes are private.
6. Unknown 2025 park factors (Rays at Tropicana in 2026, neutral sites) were set to 1.0.
7. The ±1/√ESS term uses the ESS summed across chunks.

## 8. What would have to be true for this to work in spring 2027
- **Model:** a better player-level forecaster than the market on at least one prop family. That would mean Brier at least about 0.002 below market on held-out games, not the current 0.0005 to 0.013 *above*. The realistic route is information the market prices slowly: confirmed lineups and weather at the lineup post, umpire, bullpen fatigue.
- **Strikeout props:** a starter "stuff on the day" effect that reproduces P(opponent ≥ 5 | 7+ K) ≈ 0.26 before KS props are touched again.
- **Fees and spread:** at today's ×0.5 multiplier the taker fee is under 1¢ at the prices that matter. The 1–3¢ spread is the binding cost, so a maker or limit approach is needed, not a taker one.
- **Liquidity:** a 35% fill rate on 10 contracts means displayed depth needs recording. Start the WebSocket order-book recorder on opening day 2027, so execution is measured on real depth.
- **Combos:** test the correlation thesis where it lives, on same-game combos priced off independence, using authenticated read-only RFQ data. MLB has too few same-game combos; NFL and NBA same-game combos are in season now.
