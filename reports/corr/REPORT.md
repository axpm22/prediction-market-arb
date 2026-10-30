# Three market-internal correlation lanes: discovery REPORT (paper only)

**Spec:** `PREREG_CORR3_v1.md` (sha256 `dfc830e8…4c4636`), frozen before any candle pull.

**Status:** discovery scored (event dates 2026-08-30 → 10-04). **The confirmation sets (10-05 → 10-28) were pulled and remain sealed and unread.** They are kept as a holdout for any v2.

**Bottom line:** none of the three lanes finds a tradable inconsistency between Kalshi's liquid markets and their thinner siblings. In every lane the market mid scores as well as or better than the market-anchored model on Brier, and every lane fails E1, E2 and E3 on discovery. Together with the MLB lane (`reports/mlb_corr/REPORT.md`, DEAD), this says Kalshi prices within-game correlation about right, to within fees and spread, in the families tested.

| lane | idea | fills | P&L | per $ | p | Brier market vs model | sign-flip null |
|---|---|---|---|---|---|---|---|
| **S** soccer scoreline (EPL, La Liga, Serie A, Bundesliga, UCL, MLS) | Dixon-Coles fit to 1X2 + total, pricing BTTS, spread, team total and correct score | 147 | −$92.30 | −24¢ | 0.95 | market better on BTTS (.2325 vs .2344) and spread; tie on the others | +$48.50 (p .08) |
| **F** football team totals (NFL + NCAAF) | bivariate normal (margin, total) with realized ρ, pricing team totals | 63 | +$8.49 | +2.8¢ | 0.40 | tie (.16884 vs .16874) | +$0.59 |
| **P** NFL passing identity, QB arm | QB passing-yards ladder vs Σ receiving-yards ladders | 72 | −$44.06 | −14¢ | 0.80 | market better (.1846 vs .1966) | +$16 |
| **P** NFL passing identity, REC arm | same identity, signals on receiver ladders | 82 | −$53.64 | −17¢ | 0.82 | market better (.1747 vs .1797) | +$60 |

The bar is α = 0.05/7. Event counts with fills were 91 (S), 48 (F), 26 (P), all below the n ≥ 100 requirement.

## What each lane taught us
- **S, soccer.** Kalshi prices more both-teams-to-score than an independent-Poisson fit to 1X2 and total implies. The market is right: the BTTS signals lost and the sign flip won. The outcome-only Dixon-Coles ρ is −0.05 to −0.11 (SE ≈ 0.07–0.13), negative but not significant. That correction moves the model the right way but is too small to close the 2¢ gap.
- **F, football.** The correlation between margin and total that Kalshi's team-total prices imply (ρ ≈ 0.13 pooled) matches the realized one: 0.15 for NFL with n = 42, and 0.19 for NCAAF, 95% CI 0.07–0.30. Setting ρ = 0 gives identical P&L, so the correlation term carries no edge. This was the cleanest test of the "does the market price the right amount of correlation" question, and the answer is yes.
- **P, NFL passing identity.**
  - The identity itself holds exactly: team passing yards equal the sum of receiving yards in 80 of 80 team-games, per ESPN box scores.
  - The market-implied coverage ratio R̄ = 0.735 sits below the realized 0.86 because a normal fit to right-skewed yardage ladders recovers medians, not means.
  - Market R and realized R correlate at only 0.16, so the spread in R mostly reflects which receivers are listed, not information.
  - As specified, the lane measured a modelling artifact.

## Data issues found (fix before any v2 or forward run)
1. **Kickoff time.** NFL `expected_expiration_time` is kickoff + 3 h in two thirds of games and kickoff + 6 h in the other third. So the prereg's exp − D rule put T after kickoff in 21–22 of 63 NFL discovery games. Lanes F and P excluded them: F on Kalshi `settlement_ts`, P on ESPN kickoffs. Any v2 needs kickoffs from an external schedule (ESPN, keyless), and the NFL confirmation windows need a re-pull.
2. **The in-play volume-spike check is noise.** Pregame volume spikes fire it in all sports. The buffer rule raised T by 30 min everywhere, which is conservative, not harmful. Kalshi's `settlement_ts`, or external kickoffs, are the reliable test.
3. **Refit intervals collapse.** Drawing anchors within their bid/ask range cancels out over 10–30 anchors, so intervals end up about 0.4¢ wide. Signals then fire on any 2–3¢ model-shape error. A model-error term is needed, for example the discovery anchor-fit residual sd.

## Deviations (details in each lane's results JSON and in the agents' notes)
- **Decision time:** T = puller `T_ts` − 30 min in all lanes (the literal prereg reading), then −30 more after the failed in-play check.
- **Exclusions:** the NFL in-play exclusions above.
- **Anchor minimums:** 1 GAME + 1 TOTAL quote for S; at least 2 per normal fit for F.
- **N1:** restricted to the same league (F).
- **E3:** by league share; the top-5% test only for P, which is NFL-only.
- **Discovery size:** none of the lanes could reach n ≥ 100 events with fills, given about 5 weeks of in-season data.

## If anything here is worth a v2
Only P has a structural correction with a clear mechanism: means from the ladder's survival sum instead of a normal fit, plus a fixed receiver set and real kickoffs. Even then, the realized coverage ratio varies a lot from game to game (10th–90th percentile 0.60–1.00), and the market already beats the model on every rung. Expect INFORMATIVE at best. The sealed confirmation sets are kept for exactly this kind of one-shot v2 test.
