# Whale-follow backtest — pre-specified plan (2026-09-05)

Operator ask: "Keep messing with the data we already have to see what strategy would've gotten us
favorable results — don't over fit." This plan is fixed BEFORE any result is computed. The
analysis is a report, not a lane change: anything it suggests goes through PREREGISTRATION.md as
a dated amendment, never straight into config.yaml.

## Data (read-only, `file:data/whale_tape.sqlite?mode=ro`; never data/arb.sqlite)

- `positions_mat`: 76,589 whale positions (FIFO-reconstructed by the lane), 68,971 in resolved
  markets, 2,270 wallets; columns avg_price, size, notional_usd, entry_ts, last_ts, pregame,
  sport, market_type, root, source, sold_size, sell_value.
- `prints`: 827k `activity` rows (575 fully backfilled wallets, dense from 2026-03) + 206k `tape`
  rows (2026-09-04→). Used for entry timing/price (first BUY fill of a position) and for the
  whale's later sells.
- `resolutions`: winner_outcome_index for 73k markets. `markets`: sport, market_type, start_utc.
- Fees: per root from config.yaml (`whale` fee keys); sports root fee applies to every sports row.

Unit of analysis: one followed position = (wallet, condition_id, outcome_index). One paper stake
of $1 per followed position at entry. P&L per $ = payout/entry − 1 (payout 1 if outcome_index
is the winner else 0), minus the venue fee exactly as `arb/whale_score.realized_edge` charges it.

## Walk-forward wallet scoring (no look-ahead)

Skill = the lane's own `arb.whale_score.scores(positions, resolutions, as_of, cfg, base_rates)`
called with `as_of` = the first day of each calendar month; a position entered in month M is
judged with scores computed from markets RESOLVED before the first day of M. Nothing resolved
after entry may influence the decision to follow.

## Hypothesis grid (fixed — 12 configurations, Bonferroni over 12)

| knob | levels |
|---|---|
| Q wallet filter | all wallets in the set (baseline) · walk-forward score in the top 50% · top 20% |
| K consensus | 1 wallet · ≥ 2 distinct qualified wallets on the same (market, outcome) within 24 h (enter at the 2nd) |
| E exit | hold to resolution · exit when the followed wallet's cumulative sells reach ≥ 50% of its size (exit at the wallet's sell price − slippage) |

Costs: entry = the wallet's first BUY fill price + slippage 0.01 (main). Sensitivity at 0.00 and
0.02 reported for the same 12 configs, never used to pick.

Diagnostics — reported for every config, NEVER used to choose: follow vs fade (buy the opposite
outcome); entry price band (<0.20, 0.20–0.50, 0.50–0.80, >0.80); sport; pregame vs in-play;
wallet notional decile; calendar month.

## Split and statistics

- Train: entry_ts before 2026-07-16. Test: entry_ts in [2026-07-16, 2026-09-04). Tape era
  (2026-09-04→) reported separately as "live era" (contains the lane's own decisions).
- Per config and split: n positions, n events (cluster = markets.event_key, else condition_id),
  mean ROI/$, win rate, event-clustered bootstrap (2,000 resamples, seed 20260905) lower 80%
  bound (lb80) and upper 90% bound (ub90), and the Bonferroni-12 lower bound (quantile 0.2/12).
- Monthly walk-forward table (mean ROI by entry month) for every config.
- A configuration is called FAVORABLE only if its TEST Bonferroni-12 lower bound is > 0 and the
  train sign agrees. Everything else is reported as not favorable, including near misses.

## Known limitations (state them in the report)

1. Selection bias: the 575 backfilled wallets were discovered by large recent prints; wallets
   that blew up and stopped trading are absent. The baseline (Q = all) carries the same bias, so
   the DIFFERENCE between Q levels is the honest read of skill ranking, not the absolute ROI.
2. Slippage is an assumption; there are no historical quotes. The lane's own 120 s markouts
   (mean drift ≈ 0) are the only calibration.
3. Depth is ignored (the lane's depth gate is not simulated).
4. Outcome P&L only; no mark-to-market.

## Outputs

- `tools/backtest_whale_follow.py` — stdlib + sqlite3 only, read-only URI, `--split`,
  `--slippage`, deterministic seed; prints the tables and writes
  `reports/backtest-whale-follow-2026-09-05.md`.
- The report lists ALL 12 configs on train and test with the statistics above, the sensitivity
  tables, the diagnostics, and the limitations verbatim.

## Addendum (2026-09-05 18:05Z) — descriptive read of the live-era `signals` table, NOT a test

Controller query, read-only, before the historical backtest reported. 136 markets evaluated
since 2026-09-04 14:16Z, 77 resolved (mostly MLB and tennis, one weekend). ROI/$ at the touch
ask recorded at each (variant, market, side)'s FIRST evaluation, fees ignored, one row per pair:

- everything the whales pointed at lost on average: V1 min-points −0.178 (n 83), V1 ratio
  −0.084 (30), V6 FIRED −0.303 (11), V6 ratio +0.252 (18), V6 too-close +0.410 (5);
- pairs that ever fired: −0.130 (n 32, 29 markets); pairs that never fired: −0.062 (92);
- by ask band (V1): <0.30 −1.000 (7, all lost), 0.30–0.50 −0.142 (39), 0.50–0.70 −0.018 (55),
  >0.70 −0.195 (14); by ratio band: ≥0.95 −0.168 (84), <0.60 −0.010 (19);
- V6 by holders share: <0.67 positive on n 6, ≥0.90 −0.175 (59);
- crude fade control (buy the other side at 1 − ask, spread ignored): +0.215 (n 115).

This is 1.5 days and ≤ 69 markets: it cannot select anything. It only says the sign of
"follow" is negative in the live era so far and "fade" is worth reading carefully in the
historical backtest's follow-vs-fade diagnostic (already in the plan; still diagnostic-only).
