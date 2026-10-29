# PREREG_MLB_CORR_v1 — Kalshi MLB correlated-market mispricing (paper only)

Frozen 2026-10-29 ET, before any bulk Kalshi price pull. Any change is a new version file with a new sha256; every deviation is disclosed in REPORT.md.

**Paper only.** Public GET endpoints only. No orders, no credentials.

## 0. What was seen before this freeze (disclosure)
Phase 0 (`PHASE0.md`, 230 public calls) looked at metadata for every MLB series, plus trades and candles for 8 sample markets: 2 game winners, 1 total, 2 HR markets, 1 KS market and 2 others (2025-07-20, 2026-04-15, 2026-07-20, 2026-09-15). It also read 63 settled combos (Sep 15–16). The sampled prices were looked at only for their schema. They are excluded from scoring if a sampled game is drawn.

## 1. Hypotheses (two edge types, scored separately)
- **H1, marginal inconsistency.** A player-prop market (HR, HIT, KS) is mispriced against the joint game distribution implied by its sibling game markets (winner, total, spread, team totals), by more than taker fee + spread and outside the model's own uncertainty.
- **H2, combo mispricing.** This is **descriptive only**. Combo quotes are RFQ-only (auth) and same-game MLB combos are rare (4 of 63 sampled). H2 gets a count and a model-vs-trade-price table from public combo trades. It makes no P&L claim and has no verdict beyond INFORMATIVE.

## 2. Universe
- **Games:** 2026 MLB regular season only. The postseason is excluded from scoring and reported descriptively if at all. One unit = one MLB game (gamePk), matched to Kalshi events by date + teams + start time. Doubleheaders are matched by start time. A game that can't be matched unambiguously is dropped.
- **Anchor series** (they set the game's run environment): KXMLBGAME, KXMLBTOTAL, KXMLBSPREAD, KXMLBTEAMTOTAL.
- **Target series** (where signals can fire):
  - KXMLBHR: all thresholds.
  - KXMLBHIT: thresholds 1+ and 2+ only.
  - KXMLBKS: all thresholds.
  - TB, HRR, RBI, F5 and RFI are out of scope for v1.
- **Discovery set:** 300 games drawn uniformly at random (numpy `default_rng(20261029)`, drawn over the sorted gamePk list) from regular-season games scheduled 2026-04-01 → 2026-08-29 ET. It is used for model sanity, bug-finding and descriptive numbers. **No rule below may be changed from it except by a v2 file hashed before the confirmation set is opened.**
- **Confirmation set (sealed, scored once):** every regular-season game scheduled 2026-08-30 → 2026-09-27 ET. Its candles may be pulled early (the archive cutoff moves) into a separate file `data/mlb/kalshi_confirm.sqlite`. That file is not read by any analysis until §8's freeze line is committed.

## 3. Exclusions (discard, never impute)
- Any market that settled at a Kalshi "fair price" (`settlement_value` strictly between 0 and 1): player not in the starting lineup, starter with no PA or no batter faced, postponement past 48 h.
- Games that were postponed, suspended, resumed on a later date, or official but shortened (< 8.5 innings). Status comes from the MLB Stats API.
- Quotes:
  - No candle with both yes_bid and yes_ask in [T−10 min, T] (stale).
  - ask ≤ bid (crossed or locked).
  - A one-sided book.
  - A target's entry price outside [0.05, 0.95].
- Games with fewer than 2 usable anchor markets.
- Props whose player can't be matched to a single MLBAM id in that game's starting lineup (or the starting pitcher, for KS).

## 4. Decision time and information set
- **T** = the scheduled first pitch from the event ticker (HHMM ET) − 30 min.
- **Quote** = the last 1-minute candle ending in [T−10 min, T].
- **Model inputs** (no lookahead beyond the disclosed lineup leak):
  - Statcast plate appearances from 2025 plus 2026 through the day before the game (D−1).
  - Park factors from 2025 only.
- **Lineup leak:** the lineup and starting pitcher are the actual starters from the MLB Stats API boxscore. Lineups are normally posted 1–4 h before first pitch, so this matches what was knowable at T except for late scratches after T. Props on players who didn't start are void at fair price and already excluded. Teammates' batting slots may still leak slightly; this is disclosed and not corrected.
- No weather input in v1 (disclosed limitation).

## 5. Model
**Plate-appearance Monte Carlo** (`arb/mlb/sim.py`):
- **PA outcomes:** {K, BB+HBP, 1B, 2B, 3B, HR, out in play}.
- **Batter and pitcher rates:** empirical-Bayes shrinkage to the league rate (prior weight in PA fixed in `config.yaml mlb.shrink_pa`, set before data).
- **Matchup:** combined by the multinomial odds-ratio (log5) relative to the league rate, then multiplied by the 2025 park factor for HR and hits.
- **Pitching:** the starter faces batters up to his expected batters faced (2026 YTD mean, shrunk; default 22, so an opener comes out early through his own history). After that the batting team faces its opponent's bullpen aggregate.
- **Baserunning:** a fixed advancement table, plus double plays and sac flies at fixed rates. RBIs are credited to the batter on whose PA the runs score.
- **Game rules:** the bottom of the 9th is skipped when the home team leads, and a walk-off ends the game. Extra innings in the regular season start with the automatic runner on 2nd.
- **Size and uncertainty:** 20,000 sims per game in 20 chunks of 1,000. Each chunk draws every batter's and pitcher's rate vector from its Dirichlet posterior, which carries parameter uncertainty.

**Market-relative fair value (anchoring):**
- Sim weights are w_i ∝ exp(θ_h·R_home,i + θ_a·R_away,i).
- (θ_h, θ_a) minimize the squared error between the weighted model probabilities and the anchor mids.
- Props are then read off the same weighted sims, so the correlation structure comes from the model and the run level from the market.
- This is done per chunk. **fair** = the mean over chunks. **[lo, hi]** = the 5th and 95th percentiles of the 20 chunk estimates, plus a ±1/√(eff. n) Monte Carlo term.

## 6. Signal and fill (one rule, no variants)
- **Buy YES** when lo − entry_ask − fee(entry_ask) > 0.
- **Buy NO** when (1 − hi) − (1 − entry_bid) − fee(1 − entry_bid) > 0.
- **Latency:** entry_ask = max(ask at T, ask at T+1 min). entry_bid = min(bid at T, bid at T+1 min). If the T+1 candle is missing, the T value is used.
- **Fee:** Kalshi taker, ceil-to-cent on the whole order. That is ceil(0.07 × M × C × P(1−P)), where M is the series multiplier on the trade date from `GET /series/fee_changes`: 1 before 2026-08-07 04:57 UTC, 0.5 after. Sensitivity: M = 1 throughout.
- **Size:** 10 contracts per signal, capped by a fill proxy. The cap is the volume of the 1-minute candles in (T, T+10 min] whose trade-price high ≥ entry (YES) or low ≤ entry (NO). A cap of 0 means no fill. Fill rate = filled / signalled. No fills at mid, ever.
- **Exposure:** at most one position per market. All fills in one game form **one unit**, with game P&L = the sum of its fills.

## 7. Scoring (GATES_v2 E1–E6, confirmation set)
- **E1:** game-unit net P&L after fees > 0. One-sided p < 0.05/3 (3 target families) by game-cluster bootstrap (10,000 resamples). n ≥ 100 games with fills, ≥ 20 dates.
- **E2:** net > 0 in ≥ 3 of 4 chronological folds.
- **E3:** no family > 40% of positive P&L, and the top 5% of games < 50% of positive P&L.
- **E4:** the confirmation set is the sealed holdout.
- **E5:** a forward leg is impossible before the 2027 season, so **the best possible verdict is SOFT.**
- **E6:** there is no historical depth; this is reported as untested.
- **Verdicts:**
  - **DEAD** if confirmation net ≤ 0, or if a null (§9) reproduces ≥ 50% of the edge.
  - **SOFT** if E1–E3 pass.
  - **INFORMATIVE** otherwise.
- **Reported:**
  - Signals, fill rate, P&L, P&L per $ risked, hit rate vs predicted, max drawdown by date, concentration.
  - The same set restricted to the mid ≤ 0.10 and mid ≥ 0.90 bands, reported separately.

## 8. Model calibration (outcomes only, no prices)
- **Marginals:** for each marginal (team runs ≥ k, total, winner, HR 1+, HIT 1+/2+, KS k+), a reliability table and Brier score on the discovery-date games, against a naive independence model. The naive model is player prop = binomial over expected PA from the player rate alone, with team runs treated as independent of player outcomes.
- **Conditionals:** empirical vs model P(team ≥ 5 runs | player HR) and P(opp ≥ 5 | SP KS ≥ 7).
- **Market sanity:** untilted model marginals vs market mids on the discovery set. If the market's Brier beats the model's by > 0.005 on a family, that family is flagged *model-suspect*. This is reported, not a gate, because anchoring already takes the run level from the market.
- **Freeze line:** before the confirmation file is read, a commit adds `CONFIRM_OPENED.md` with this file's sha256 and the code commit hash.

## 9. Null tests (run on both sets)
- **N1, shuffled anchors:** θ is fit on anchors from a different random game on the same date (seeded).
- **N2, independence fair:** the prop fair value comes from the naive independence model, with no game context.
- **N3, sign flip:** take the opposite side of every signal at its own entry price.

An edge that survives N1 is an artifact.

## 10. Kalshi call budget
- Pacing: ≥ 2 s between calls, ≤ 90 per 300 s.
- Quiet windows: 09:25–09:45 ET daily and Mon 10:25–10:45 ET.
- 429s: honor Retry-After (else wait 120 s); a second 429 aborts.
- Every call is logged to `logs/mlb_kalshi_requests.log`.
- Candles are pulled with `GET /historical/markets/{t}/candlesticks` per market for markets settled before the archive cutoff, and batch `GET /markets/candlesticks` otherwise. The window is 1-min candles over [T−15 min, T+15 min].
