# LIVE_BASEBALL_EV_SPEC.md

Status: **DRAFT — measurement-only, paper, betting OFF.** Obeys the hard rules in CLAUDE.md
(public reads only; no order endpoints; Decimal money math; append-only ledger; every
threshold lives in `config.yaml`). This spec reworks the pasted draft. Read §0 first — it
explains what in the draft is sound and what is internally contradictory — then §1–§5 are
the corrected spec, pre-registration, and the feasibility questions that must be answered
before a line of code is written.

---

## 0. Verdict on the draft

The instinct is right and matches this project's whole philosophy: **a live "edge" is
guilty until proven not-stale.** The freshness gates, the recheck/VANISHED idea, the
"measurement-only first" stance, and the promotion gate are all in the correct spirit and
are kept. But the draft has six contradictions and three untested assumptions that would
make it either un-runnable or self-deceiving. They have to be fixed before the spec is
coherent.

**Six things that break as written**

1. **Freshness is double-counted.** You hard-reject anything older than 5s *and* subtract
   `latency_penalty = seconds_since_update * 0.01`. Inside a 5s gate that penalty can only
   ever be 0–0.05, and you cap it at 0.04 — an arbitrary magic coefficient applied to a
   window you already declared "fresh." Gate hard, OR penalize continuously from measured
   decay — not both with an invented per-second number (§1.4, §1.9).
2. **The recheck window contradicts the freshness window.** Recheck after 1–3s, but require
   state ≤5s. After a 3s wait the *original* state is up to 8s old and fails your own gate.
   The recheck must **re-fetch** fresh state and consensus, not reuse the entry snapshot
   (§1.10).
3. **Cross-feed timestamp comparison assumes a shared clock you don't have.** Venue server
   time, Odds API time, MLB Stats time, and your local clock are four different clocks.
   "Timestamps disagree by >3s" is unmeasurable without sync. You can only trust **your own
   monotonic fetch time** plus each feed's *measured* publish lag (§1.4).
4. **Baseball has no game clock.** That input is templated from a clock sport. The relevant
   timer is the **pitch clock** (~15–20s), which is what actually defines "between pitches"
   (§1.5).
5. **Three different decision bars, never reconciled.** `edge_points > 0` to record,
   `>= 0.07` to promote, and `adjusted_edge >= threshold` after penalties. Which number is
   compared to 0.07 — raw or adjusted? Collapse to one ladder with one promotion bar on the
   **adjusted** edge (§1.8).
6. **`min` of depth and Kelly mixes a cap with a target.** Sizing off noisy live fair value
   then Kelly-ing it overbets when `p_hat` is wrong. Size off the **lower bound** of the
   fair-value interval, not the point estimate (§1.13).

**Three assumptions the draft treats as facts**

- **"Fair value = de-vigged sportsbook median, and reject if consensus moves to the
  venue."** This *hardcodes US books as truth and the prediction market as wrong* — the
  exact flaw flagged for pregame EV (model_changes.md, Weakness #5). Live, Polymarket can
  lead the books. As written you would systematically reject the cases where the venue was
  *right and faster*. Fix: don't assume direction — **measure live lead-lag first**, and
  gate on the venue's **own** later price (CLV), which is direction-agnostic (§1.6, §3).
- **"A trusted live win-probability model" is available.** There is none in the repo, and
  building one is a separate project with its own calibration risk. Either drop it for v1
  (sportsbook-only fair value) or admit it as a **separately gated** source that must clear
  a Brier/log-loss bar on settled games *before* it may set fair value — reuse
  `arb/forecast/scoring.py` (`brier`, `log_loss`), which already exists (§1.6).
- **"$50 executable depth and 5s freshness are obtainable."** Live MLB books are thinner
  than pregame, MLB Stats play-by-play publishes with real latency, and live odds burn
  credits fast against a 500/mo cap. None of these are knowable a priori — they are
  **feasibility unknowns to be measured in Phase 0**, not constants to assume (§5).

Everything below is the corrected spec.

---

## 1. Corrected spec

### 1.1 Separate lane

`kind = "LIVE_BASEBALL_EV"`, isolated from pregame EV, micro EV, ARB, and DIVERGENCE in
detection, ledger, scoring, and reporting. It never contributes to any clean pregame total.
Like every signal in this project it is **measurement-only** until its pre-registered
promotion gate (§3) clears. Betting stays OFF.

### 1.2 Phases (do not skip; mirrors the repo's phase discipline)

- **Phase 0 — feasibility probe (no detection yet).** Poll one or two live games and simply
  *record the freshness reality*: distribution of MLB Stats play publish lag, venue quote
  age, number of simultaneously-available live books, and live depth at top of book. Output
  is a histogram, not a bet. This phase decides whether the rest is even possible (§5).
- **Phase 1 — candidate recorder.** Detect and log candidates with full state, never
  promote. Build the VANISHED / stale-rejection statistics.
- **Phase 2 — calibration.** Fit the latency penalty and uncertainty buffer from Phase 1
  data (not from guessed constants), and run the lead-lag null (§3).
- **Phase 3 — promotion review.** Only if the frozen gate (§3) clears.

### 1.3 Inputs (per MLB game, per poll)

Venue live quote (Kalshi/Polymarket/SX) with top-of-book bid, ask, and depth; live
sportsbook odds when available; score; inning; half (top/bottom); outs; base-runner state;
balls/strikes; **pitch-clock state** (not "game clock"); market trading/suspended status;
and — critically — **your own fetch timestamp for each feed** plus each feed's embedded
publish time (used only to *estimate lag*, never as a synced clock).

### 1.4 Freshness gates (reframed around your fetch clock)

All ages are measured as `now_monotonic - your_fetch_time`, never by differencing two feeds'
embedded clocks. Reject a candidate if any of:

- venue quote fetch age > `live.max_venue_age_secs`
- game-state fetch age > `live.max_state_age_secs`
- live-consensus fetch age > `live.max_consensus_age_secs`
- the market suspended or reopened within `live.suspend_cooldown_secs`
- the quote was fetched inside a play-update window (a pitch/play landed between the state
  fetch and the quote fetch)

The "feeds disagree by >3s" rule is replaced by a single derived quantity,
**`effective_state_age` = state fetch age + measured MLB-Stats publish lag** (lag estimated
in Phase 0). That is the only staleness number the edge math sees. Default targets are
*placeholders pending Phase 0* — do not trust 5s until the lag histogram says it's
achievable.

### 1.5 Game-state stability gate (baseball-correct)

Evaluate only in genuinely stable moments: between batters, between innings, or after a
pitching change once the market has reopened and held steady for
`live.reopen_stabilize_secs`. Suspend evaluation when: an at-bat is live and the count feed
is stale; within `live.post_event_freeze_secs` of a hit / out / walk / error; runner state
changed within the last 10s; or score changed within the last 15s. The pitch clock is the
within-at-bat timer; "between pitches" is only usable if count data is fresher than the
pitch-clock interval.

### 1.6 Fair value (direction-agnostic, no hardcoded truth)

Preference order, each with an explicit guard:

1. **Median de-vigged live sportsbook probability** across ≥3 independent live books on the
   *exact same market* (ML vs ML, exact total vs exact total). De-vig with the existing
   `consensus_probabilities` machinery so totals stay line-exact and the vector renormalizes.
2. **A live win-probability model** — permitted *only* once it has cleared a frozen
   calibration bar on settled games (Brier and log-loss via `arb/forecast/scoring.py`)
   better than the sportsbook prior. Until then it may be logged for comparison but may not
   set fair value.
3. **Hybrid** (sportsbook median nudged by the calibrated model) — allowed only after both
   #1 and #2 exist.

Pregame consensus is a **sanity prior only**, never the fair value. Crucially, the system
does **not** assume the books are sharper than the venue; that question is settled
empirically by the lead-lag null and the CLV test in §3, not by construction.

### 1.7 Consensus requirements

≥3 independent live books; exclude any book >8–10 points from the median **as stale**
(record the exclusion); compute on the identical market only. With 1–2 books, the candidate
is logged as `RESEARCH`, never as EV. Carry a `fair_value_ci` (e.g. bootstrap or
min/median/max across books) — it is used for sizing and for the uncertainty buffer.

### 1.8 Executable price and the single edge ladder

Buy at executable **ask** + fees + buffer; exit at executable **bid** − fees − buffer.
Never midpoint. Fees use the venue's correct basis (`all_in_fee_cost`, quadratic for Kalshi
/ linear for Polymarket/SX) — already in `pricing.py`.

One coherent ladder, one promotion bar:

```
raw_edge      = fair_value - executable_cost
adjusted_edge = raw_edge - latency_penalty - fee_buffer - model_uncertainty_buffer
```

- `raw_edge > 0`  → record as a **candidate** (Phase 1 archive).
- candidate passes all freshness + stability gates → **fresh candidate**.
- fresh candidate passes the recheck (§1.10) → **confirmed candidate**.
- `adjusted_edge >= live.live_ev_min_edge` → **promoted** (display; bet only post-gate).

`live_ev_min_edge` starts at **0.07** (7 points — deliberately far above pregame because
live latency risk is larger). The 0.07 is compared to **`adjusted_edge`**, not `raw_edge`.

### 1.9 Latency / uncertainty penalty (calibrated, not invented)

Until Phase 2 calibration exists, treat latency as a **flat uncertainty buffer**
(`live.latency_buffer_init`, e.g. 0.03) rather than the draft's `seconds * 0.01` linear
term — a per-second coefficient with no empirical basis inside an already-gated window is a
magic number (CLAUDE.md rule #4). In Phase 2, replace it with the **measured price decay vs
`effective_state_age`** observed in Phase 1: regress how much the venue price moves against
you per second of staleness, and use that slope. `model_uncertainty_buffer` scales with the
width of `fair_value_ci` (wider book disagreement → larger buffer).

### 1.10 Recheck (windows made consistent + a null)

After `live.recheck_delay_secs` (1–3s), **re-fetch** venue quote, game state, and consensus
— do not reuse the entry snapshot. Reject unless: same game state (inning/outs/bases/score
unchanged), edge still present, market not suspended, depth still there. If consensus moved
**to** the venue, that is recorded but **not auto-rejected** — whether venue-leads-books is
the hypothesis under test, not an assumption (see §3 lead-lag null). The recheck's own state
must independently pass §1.4 freshness.

### 1.11 No-chase / VANISHED

If the edge is gone on recheck, record it as `LIVE_VANISHED`, never as missed EV. The
VANISHED rate, **compared against a null** (rechecking random non-flagged live quotes the
same way), is the headline diagnostic: it tells you whether the strategy is mostly
stale-quote noise or something that survives.

### 1.12 Settlement and tagging

Settle by final score through the existing append-only path, tagged `LIVE_BASEBALL_EV` and
carrying the full entry context: game state (inning, half, score, outs, bases), pitch-clock
state, `effective_state_age`, venue quote age, consensus age, fair value + CI, raw and
adjusted edge, recheck pass/fail, and stale/suspension rejection reasons. As with ARBs,
realized P&L is provisional until a venue-resolution reconcile step confirms the leg
actually settled as assumed (extra innings, suspended-game rules, postponement voids differ
by venue — reuse `reconcile.py`'s pattern; do not re-book the entry P&L blindly, the mistake
flagged in Weakness #1).

### 1.13 Sizing (if ever enabled)

Measurement-only with paper $1 first. If enabled later:

```
stake = min(visible_depth,
            bankroll * live.live_ev_max_stake_pct,
            live.live_kelly_fraction * f_star(p_lower, cost) * bankroll)
```

where `f_star` is the existing `kelly_f_star` evaluated at the **lower bound** of the
fair-value interval (`p_lower`), not the point estimate — heavy haircut
(`live_kelly_fraction <= 0.10`). Caps: per-bet, per-game, and total live exposure, enforced
through `exposure.py` with **per-venue** caps (live USDC/USD pools don't move instantly).
Starting caps: $25/bet, $50/game, $100 total live.

### 1.14 Exit (stricter than pregame)

Exit when executable **bid** locks profit after fees, or adjusted remaining edge ≤ 0, while
game state is stable and bid depth is sufficient. Force-flatten / stop adding when the game
enters a late high-variance state, the fair-value source goes stale, consensus drops below 3
books, or the market repeatedly suspends. Reuse the `ev_exit` watcher pattern with a
live-specific (tighter) config block.

---

## 2. Dashboard (separate panel)

Live candidates; VANISHED-after-recheck count and rate; recorded live EV; average quote age,
average `effective_state_age`; **CLV at +30s, +2m, and next-inning** (computed with
`clv_points` against the venue's own later mid); settled P&L; stale-rejection count;
suspension-rejection count; and the **VANISHED-vs-null gap**. Live EV is never folded into
clean pregame EV totals.

---

## 3. Pre-registration (freeze before Phase 1 data accumulates)

Frozen hypotheses, in the project's existing style (medians, bootstrap 95% CIs at n≥30,
flat-$1 headline, earliest-verdict-row rule):

- **H-LIVE1 (survival):** among confirmed candidates, **mean CLV vs the venue's own price at
  +30s and +2m is > 0** with a bootstrap 95% CI excluding 0, over **n ≥ 100** confirmed
  candidates. This is the direction-agnostic edge test — it does not assume books are sharp.
- **H-LIVE2 (not-just-stale):** the VANISHED rate on flagged candidates is **materially
  below** the VANISHED rate of the matched random-quote null. If flags vanish as often as
  random quotes, the "edge" is staleness.
- **H-LIVE3 (lead direction):** establish, null-corrected, **who leads live** (venue vs
  book median) before trusting either as fair value — same null-correction the pregame
  lead-lag result demanded (RESULTS.md, Finding 3).

**Kill criteria (thesis dead — stop):** VANISHED rate not below null over n≥100; or mean
CLV ≤ 0 with CI excluding a meaningful positive over n≥100; or a large negative P&L tail
attributable to latency events.

**Promotion gate (ALL required before any real bet):** ≥100 recorded confirmed candidates;
positive CLV at +30s and +2m (H-LIVE1); VANISHED rate below null (H-LIVE2); no large
negative latency tail; settled paper ROI positive after fees; and a **manual review of a
random sample** confirming the recorded game state was actually correct at entry.

**Kill switches (auto-disable):** 5 consecutive live losses traced to stale state; quote/
state age repeatedly over threshold; venue feed lagging the score feed; a change in venue
suspension behavior; daily live drawdown over cap; or feed timestamps becoming unreliable.

---

## 4. Config block (everything tunable, per CLAUDE.md rule #4)

```yaml
live_baseball_ev:
  enabled: true                 # measurement only; betting gated separately
  betting_enabled: false        # never true until §3 promotion gate clears
  # freshness — PLACEHOLDERS pending Phase 0 feasibility histograms
  max_venue_age_secs: 5
  max_state_age_secs: 5
  max_consensus_age_secs: 5
  suspend_cooldown_secs: 10
  post_event_freeze_secs: 10
  reopen_stabilize_secs: 5
  # consensus
  min_live_books: 3
  book_outlier_points: 0.09     # exclude a book this far from median (as stale)
  # edge ladder
  live_ev_min_edge: 0.07        # compared to ADJUSTED edge
  micro_live_ev_min_edge: 0.10
  latency_buffer_init: 0.03     # flat until Phase 2 replaces it with measured decay slope
  fee_buffer: 0.005
  # recheck
  recheck_delay_secs: 2
  # depth & sizing
  min_depth_usd: 50
  micro_min_depth_usd: 5
  micro_max_depth_usd: 50
  live_ev_max_stake_pct: 0.025
  live_kelly_fraction: 0.10
  cap_per_bet_usd: 25
  cap_per_game_usd: 50
  cap_total_live_usd: 100
  # win-probability model: logged but may not set fair value until calibrated
  wp_model_enabled: false
  wp_model_min_brier_improvement: 0.0
```

---

## 5. Feasibility questions to answer in Phase 0 (before building anything)

These are the make-or-break unknowns. The draft assumes them away; they must be *measured*.

1. **Credit budget vs freshness (the hard blocker).** Live odds polling at single-digit
   seconds against The Odds API will blow the 500-credit/month cap in hours and collides
   with CLAUDE.md rule #2. Quantify the real per-poll cost, and decide: restrict to a few
   games in short windows, find a different live-odds source, or accept coarser freshness
   and widen `live_ev_min_edge` accordingly. **The 5s targets and the credit budget are
   currently contradictory.**
2. **MLB Stats publish lag.** Measure the actual play-by-play publish latency distribution
   via `MlbStatsAdapter`. If the median is >5s, the 5s state gate rejects nearly everything
   and the thresholds must move.
3. **Live venue liquidity.** Measure top-of-book depth on live MLB markets per venue. If
   $50 rarely exists live, the main lane is a micro lane in disguise — find out before
   coding, not after.
4. **Clock trust.** Confirm there is no synced clock across feeds and that
   `effective_state_age` (your fetch time + measured lag) is the only defensible staleness
   metric.

Until §5.1 and §5.2 return numbers, this spec stays a design — Phase 0 first.
