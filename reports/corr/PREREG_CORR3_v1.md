# PREREG_CORR3_v1: three market-internal correlation lanes (paper only)

Frozen 2026-10-29 ET, before any candle pull for these lanes. Any change is a new version file with a new sha256, and every deviation is disclosed.

Public Kalshi GETs only, with the shared paced client (≥ 2 s between calls, ≤ 90 per 300 s, quiet windows, abort on a second 429).

**What was seen before this freeze:** the Phase-0 inventory (`data/corr/INVENTORY.md`). That is series metadata, one liquidity snapshot per family, and 12 candle probes used only for their schema. No prices were used to set any rule. The MLB lane (`reports/mlb_corr/REPORT.md`, DEAD) motivated the design choice that **the level always comes from the market's own liquid markets; there is no external player or team forecast.**

## Common rules (all lanes)
- **Data window:** events whose markets settled after the archive cutoff (2026-08-30), so all candles come from the batch endpoint.
  - **Discovery:** event date 2026-08-30 → 2026-10-04 ET.
  - **Confirmation (sealed):** 2026-10-05 → 2026-10-28 ET. Its candles go into separate `data/corr/<lane>_confirm.sqlite` files, and no analysis reads them until `reports/corr/CONFIRM_OPENED_<lane>.md` is committed.
  - One amendment per lane is allowed between discovery and confirmation, as a v2 file.
- **Decision time:**
  - T = kickoff − 30 min.
  - Kickoff comes from the start time encoded in the event ticker when present. Otherwise it is the event's `expected_expiration_time` − D, where D = 3.5 h for NFL, 3.75 h for NCAAF and 2.0 h for soccer, minus a further 30 min safety buffer.
  - Check: on 20 discovery events per sport, T must precede the first in-play candle. That candle is identified as the first 1-minute candle with volume > 5× the median pregame minute. If any check fails, the buffer is raised to 60 min before anything is scored.
- **Quotes:** 1-minute candles over [T−15, T+15]. The quote is the last two-sided, uncrossed candle ending in [T−10, T]. Stale, crossed or one-sided books are discarded, never imputed.
- **Uncertainty:**
  - Each event's model is refit 200 times. Each refit draws every anchor mid uniformly from that anchor's [bid, ask].
  - fair = mean, and [lo, hi] = the 5th and 95th percentiles of the refits.
  - Signal: buy YES if lo − entry_ask − fee/C > 0, or buy NO if entry_bid − hi − fee/C > 0.
  - entry_ask = max(ask at T, ask at T+1 min); entry_bid = min(bid at T, bid at T+1).
  - Entry prices must be in [0.05, 0.95].
- **Fees and fills:**
  - Kalshi taker fee, ceil-to-cent on the whole order. The rate is 0.07 × C × P(1−P) × M, with M from `/series/fee_changes` (all lane series are currently quadratic ×1).
  - 10 contracts per signal, capped by the volume of 1-minute candles in (T, T+10] that trade at or through the entry. There are no fills at mid.
  - P&L uses Kalshi's `result`. Markets that settle at fair price are excluded.
- **Unit and scoring:**
  - All fills in one event are one unit. Game-cluster bootstrap with 10,000 resamples.
  - **k = 7 families in total across the three lanes, so the bar is α = 0.05/7.**
  - E1: net > 0 and p < α, with n ≥ 100 events with fills and ≥ 20 dates.
  - E2: ≥ 3 of 4 chronological folds positive.
  - E3: no single league > 50% of positive P&L, and the top 5% of events < 50%.
  - The forward leg (E5) can run this season but is outside this prereg. The verdict ceiling here is SOFT.
- **Nulls:**
  - N1: anchors taken from a different event on the same date (re-applied by role).
  - N3: sign flip.
  - Lane-specific nulls are listed below.

## Lane S: soccer scoreline coherence (families: BTTS, SPREAD, TEAMTOTAL, SCORE)
- **Leagues:** EPL, La Liga, Serie A, Bundesliga, plus UCL and MLS if their series exist in the window. All legs are settled on 90 minutes plus stoppage time; this is verified from each series' rules_primary and any series that deviates is dropped.
- **Anchors:** GAME (home / tie / away) and the TOTAL goals ladder.
- **Targets:** BTTS, the SPREAD ladder, TEAMTOTAL ladders, and SCORE (correct score).
- **Model:**
  - Dixon-Coles scoreline distribution (Poisson λ_home, λ_away with low-score τ correction ρ), truncated at 10 goals.
  - Per match, (λ_h, λ_a) minimize the squared error to the anchor mids.
  - **v1 fixes ρ = 0** (independent Poisson).
  - A v2 amendment may set ρ to its MLE on discovery match outcomes. That uses outcomes only, not prices.
- **Lane null N2:** the target fair value is the market's own league-average target price for the same line (no match context).

## Lane F: football team totals vs spread and total (family: TEAMTOTAL; NFL and NCAAF pooled, league reported)
- **Anchors:** GAME winner, the SPREAD ladder and the TOTAL ladder.
- **Targets:** TEAMTOTAL ladders.
- **Model:**
  - Margin M = favorite − underdog points and total S are bivariate normal.
  - (μ_M, σ_M) are fit to the spread ladder plus the winner; (μ_S, σ_S) to the total ladder.
  - The team score is (S ± M)/2. P(team > x) is evaluated with the normal at x (lines are half-points, so there is no continuity correction).
  - **ρ(M, S)** is the correlation the market cannot reveal through anchors alone. **v1 fixes ρ = the Pearson correlation of realized (M, S) on discovery events per league** (outcomes only), computed before any discovery target price is compared.
- **Lane null N2:** ρ = 0.

## Lane P: NFL passing-yards identity (families: QB arm, REC arm)
- **Identity:** a QB's passing yards equal the sum of his receivers' receiving yards. The listed receivers cover most of them.
- **Implied mean:** from each player's "X+ yards" ladder, fit N(μ, σ) by least squares to the rung mids. At least 2 rungs with mids in [0.05, 0.95] are required.
- **Ratio:** per team-game, R = Σ_listed μ_rec / μ_QB. Only games with exactly one QB PASSYDS ladder per team count. R̄ = the median R on discovery games.
- **QB arm:** fair μ*_QB = Σ μ_rec / R̄. The QB's rungs are priced with N(μ*_QB, σ_QB), and signals fire on QB rungs.
- **REC arm:** every receiver's μ is scaled by μ_QB · R̄ / Σ μ_rec, and signals fire on receiver rungs.
- **Uncertainty:** the 200 refits also bootstrap R̄ over discovery games.
- **Lane null N2:** R̄ is replaced by a game-shuffled R (same date).
