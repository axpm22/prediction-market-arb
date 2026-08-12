# BUILD_PLAN.md — Upgrade plan to make ArbitrageModel recruiter-grade

This plan turns the current honest-measurement scanner into something defensible in a
quant/prop interview. It is written to be executed **in order**. Each phase has: **Why**,
**Files**, **Steps**, **Config additions**, **Schema/DDL**, **Tests**, **Acceptance**.
Do not start a phase until the previous phase's acceptance check passes.

## Guardrails (carry over from CLAUDE.md / SPEC.md — never violate)

1. **Paper / measurement only.** Nothing in this plan places real orders. Every "trade" is
   a logged paper bet. The whale-tracker and BTC-arb work are *measurement experiments*,
   not execution. (Hard rule #1.)
2. **Money math = `Decimal`.** Pricing, fees, stakes, Kelly, P&L. Floats for display only.
3. **No magic numbers.** Every threshold/weight added here goes in `config.yaml`.
4. **`flat_bets` stays append-only.** New columns are fine; retro-edits are not. All new
   derived numbers (category scores, Kelly P&L) are recomputed from the raw log.
5. **Don't invent endpoints/fields.** For any new venue/data source (Polymarket on-chain,
   Kalshi BTC markets), record a real fixture first, verify fields, then code. If an
   endpoint 404s, stop and report — do not guess.
6. **Cross-venue = REVIEW until a rulebook is read and the pair is whitelisted.**
7. **Pre-registration is append-only.** New hypotheses are appended with date+reason before
   their data accumulates. Never edit a frozen rule.

---

# PHASE 0 — Repo hygiene (do this first; it unblocks "show a recruiter")

### Why
There is currently **no git history**, and two parallel packages (`arb/` matching SPEC and
`arbnext/` containing unspecced sprawl). A reviewer sees this in 10 seconds. Fix before
adding features.

### Steps

0.1 **Initialize git and a clean ignore file.**
```bash
cd /Users/albanm/Downloads/Codex/ArbitrageModel
git init
```
Create/replace `.gitignore` with at least:
```
.venv/
__pycache__/
*.pyc
.pytest_cache/
.env
data/*.sqlite
data/*.sqlite-wal
data/*.sqlite-shm
data/.fuse_hidden*
logs/*.log
reports/daily/*.csv
reports/daily/*.md
```
Rationale: the 238 MB SQLite DB and `.venv` must never enter git history. Keep `.env.example`,
keep `data/aliases.json`, `data/*_series.json`, `data/nonsports_map.yaml` (small seed files).

0.2 **Decide the canonical package.** Pick `arb/` (it matches SPEC and holds the core
math/tracker). Audit `arbnext/` module by module:
- For each `arbnext/*.py`, decide: **merge** (move logic into an `arb/` module),
  **quarantine** (move to `arb/experimental/` with a docstring saying "research, not in
  headline pipeline"), or **delete**.
- Suggested mapping: `closing.py`/`ev/clv.py` → `arb/analysis/` (CLV is referenced in your
  pre-registration, keep it); `political/`, `hot.py`, `focus.py`, `nonsports.py`,
  `parlay.py`, `analysis/leadlag.py` → `arb/experimental/` unless a test depends on them.
- Goal: **one** importable package, no duplicate module names, no dead code in the top level.

0.3 **Make `pytest` green.** Run `pytest -q`. Fix or `xfail` (with a written reason) every
failure. A repo you hand a recruiter must be green on a clean checkout.

0.4 **Rewrite `README.md`** around the *result*, not the feature list. Structure:
- One-paragraph thesis: "A paper-trading measurement harness for cross-venue prediction-
  market pricing, with a pre-registered methodology and an append-only ledger."
- **Headline finding** (fill in after Phase 1): the optimistic-vs-confirmed gap and what it
  means.
- How to reproduce: clone, `pip install -r requirements.txt`, `arb watch --once --dry-run`,
  `arb report --daily`.
- Link to `PREREGISTRATION.md` prominently — it is your strongest artifact.

0.5 **First commit** with a real message. Then commit per phase from here on. A clean,
incremental history is itself a hiring signal.

### Acceptance
`git log` shows structured commits; `pytest -q` is green; `arb/` is the only package;
`README.md` leads with methodology + result; the DB and `.venv` are git-ignored.

---

# PHASE 1 — Diagnose and fix the EV-signal contamination

### Why
Current settled-EV data is incoherent: the 2–5% edge bucket *loses* money while the >5%
bucket shows an average "edge" of ~4,913%. An edge of 49.1 means `EV=(p̂−c)/c≈49`, i.e. the
scanner recorded an ask near `$0.01–0.02` against a consensus `p̂≈0.5`. That is not edge — it
is a stale/empty/one-sided book leaking in (SPEC §12 traps: empty Kalshi book, stale Gamma
mid, single tiny SX maker order). **You cannot build sizing or scoring on top of a
contaminated signal.** Clean the input first.

### Files
`arb/pricing.py`, `arb/arbitrage.py` (detection), `arb/store.py`, `tests/test_ev_model.py`,
new `tests/fixtures/ev_contamination_*.json`.

### Steps

1.1 **Reproduce the contamination.** Query the existing DB to characterise it (read-only):
```sql
SELECT kind, edge, optimistic_cost, legs_json, notes
FROM flat_bets
WHERE kind='EV' AND edge > 0.25
ORDER BY edge DESC LIMIT 50;
```
For each row, look at the leg's `cost`, `depth_usd`, and venue. Confirm the hypothesis
(tiny cost + thin/empty book). Write findings as a comment block in `test_ev_model.py`.

1.2 **Add three detection guards (all config-driven):**
- **Executable-depth floor at the quoted price.** Reject any quote whose depth at best ask
  `< min_executable_usd`. You already have `min_executable_usd: 50` — enforce it *before* a
  quote can create an EV bet, not just for display.
- **One-sided / empty book reject.** Drop quotes where the opposing side is missing or where
  Kalshi shows `ask 1.0000 / bid 0.0000 / liquidity 0`. (SPEC §3.1, §12.)
- **Edge sanity cap.** Add `ev.edge_suspect_above` (default `0.25`). Any EV whose computed
  edge exceeds this is flagged `suspect-edge`, written to a **separate** ledger section, and
  excluded from headline numbers and from category scoring (Phase 2). Do **not** silently
  delete — log it (append-only ethos), just quarantine it.

1.3 **Re-grade historically.** Because `flat_bets` is append-only and the report is
recomputed from raw, add a `quality_flag` column and a one-time backfill script that
*labels* (never deletes) existing rows failing the guards. The daily report then offers a
"clean" column (guards applied) alongside the raw column — exactly the parallel-column
convention your pre-registration already mandates.

### Config additions
```yaml
ev:
  edge_suspect_above: 0.25     # EV edge above this is quarantined as stale/thin-book
  enforce_depth_floor: true    # require depth >= min_executable_usd to create an EV bet
  reject_one_sided_books: true
```

### Schema/DDL
```sql
ALTER TABLE flat_bets ADD COLUMN quality_flag TEXT;  -- NULL=clean, else 'suspect-edge'|'thin-book'|'one-sided'
```

### Tests (exact, Decimal)
- Feed a fixture with ask `$0.02`, depth `$10`, consensus `p̂=0.55` → expect the bet is
  created with `quality_flag='thin-book'` (depth<50) and **excluded** from the clean column.
- Feed ask `$0.50`, depth `$200`, `p̂=0.55`, Kalshi fee `0.0175` → `c=0.5175`,
  `EV=0.062802` (your existing §8 vector), `quality_flag IS NULL`, **included**.

### Acceptance
Re-run `arb report --daily` over the existing DB: the clean-column EV edge buckets are now
monotonic-ish and the >5% bucket no longer shows a four-digit average edge. Document the
before/after in the README's "Lessons Learned" stub.

---

# PHASE 2 — Per-category / per-segment edge scorer (borrow from 0mnjb)

### Why
Your edge is not uniform across segments. A scorer that (a) blocks segments with no proven
edge and (b) surfaces the ones that do is the single highest-value upgrade. It is computed
**from settled `flat_bets` only**, so it stays consistent with the append-only rule.

### Files
New `arb/scoring.py`; wire into `arb/report.py` and `arb/cli.py` (`arb scores`).

### Steps

2.1 **Define a segment.** A segment = `(category, sport_or_topic, side)` where `side` ∈
{YES/NO} or {OVER/UNDER} or {moneyline}. Start coarse: `sport` × `kind`.

2.2 **Score formula (0–100), all weights in config.** Compute per segment over its settled,
**clean** (Phase 1) bets:
```
score = 100 * ( w_roi  * norm(roi)
              + w_trend * norm(recent_trend)   # recency-weighted sign of last N bets
              + w_n     * norm(min(n, n_cap)/n_cap)
              + w_wr    * win_rate )
```
where `norm(roi)` maps ROI through a sigmoid/clip into [0,1]. Use the 0mnjb defaults as a
starting point (`w_roi=0.40, w_trend=0.25, w_n=0.20, w_wr=0.15`) but **put them in config**.

2.3 **Allocation tiers + hard block.** Segments scoring `< block_below` (default 30) are
hard-blocked: no new bets recorded as "eligible" (still *logged* for data, but tagged
`segment-blocked` and kept out of headline + Kelly sizing). Tiers (config):
`80–100→full, 60–79→high, 40–59→low, 30–39→minimal, <30→blocked`.

2.4 **Minimum sample gate.** No segment gets a non-zero allocation until `n >= score_min_n`
(default 30 settled bets). Below that it is `INSUFFICIENT_DATA`, never blocked or trusted.
This prevents the scorer from overreacting to noise — exactly the mistake the raw EV buckets
show.

2.5 **CLI.** `arb scores` prints a table: segment, n, win rate, ROI (clean), score, tier.
`arb report --daily` includes the scores block.

### Config additions
```yaml
scoring:
  weights: { roi: 0.40, trend: 0.25, sample: 0.20, win_rate: 0.15 }
  n_cap: 100             # sample-size component saturates here
  score_min_n: 30        # below this -> INSUFFICIENT_DATA, no allocation
  block_below: 30        # score < 30 -> segment hard-blocked
  recent_window: 10      # bets in the recency-weighted trend term
  tiers: { full: 80, high: 60, low: 40, minimal: 30 }
```

### Schema/DDL
```sql
CREATE TABLE IF NOT EXISTS segment_scores (
  computed_at TEXT, segment TEXT, n INTEGER, win_rate REAL,
  roi REAL, score REAL, tier TEXT
);  -- snapshot table, recomputed each report; raw bets remain the source of truth
```

### Tests
- Seed a fixture ledger: NCAAB-NO 50 bets @ 74% WR / +10% ROI → score in "high/full" tier,
  not blocked. CPI 20 bets @ 25% WR / −65% ROI → score < 30 → blocked.
- A segment with 5 bets → `INSUFFICIENT_DATA`, regardless of ROI sign.

### Acceptance
`arb scores` shows sane tiers; blocked segments produce zero eligible new bets in a
subsequent `watch --once`; everything reproducible from `flat_bets`.

---

# PHASE 3 — Parallel fractional-Kelly sizing column (borrow from 0mnjb)

### Why
Flat-$1 is the correct **honest measurement** headline — keep it. But adding a *parallel*
quarter-Kelly column demonstrates you understand bankroll growth vs. measurement, and gives
a second, sizing-aware P&L curve. This is an interview talking point, not a strategy change.

### Files
New `arb/sizing.py`; columns in `flat_bets`; wire into `arb/tracker.py`, `arb/report.py`.

> **Scope (logic note).** Kelly needs a probability `p̂` and a payout variance, so it applies
> only to **directional probabilistic** bets: `EV`, `FLB`, `FORECAST`. **Locked arbitrage**
> (`ARB`, `NEGRISK`, `ARB_BTC`) has no `p̂` and (in principle) no variance — size those by the
> depth-limited basket, not Kelly, and leave the Kelly columns NULL for them. Don't blend the
> two in the Kelly equity curve.

### Steps

3.1 **Kelly for a $1-payout binary** (directional bets only). For a contract bought at
effective cost `c` with model probability `p̂` (consensus for EV; model blend for FORECAST;
realized-rate for FLB), net odds `b = (1 − c) / c`, `q = 1 − p̂`:
```
f* = (b * p̂ − q) / b      # clip to [0, max_kelly_fraction_of_bankroll]
stake = kelly_fraction * f* * bankroll_usd
```
Use `Decimal` throughout. `kelly_fraction` default **0.25** (quarter-Kelly). If `f* <= 0`,
stake = 0 (Kelly says don't bet — record the row with `kelly_stake=0`).

3.2 **Store alongside flat, never replacing it.** Headline daily number stays flat-$1.
Kelly is a parallel section. Gate Kelly sizing on Phase 2: blocked/insufficient segments get
`kelly_stake=0`.

3.3 **Report both equity curves** (flat-$1 and quarter-Kelly), both optimistic and confirmed
fills → four curves total. Show max drawdown for the Kelly curves (Kelly's whole point is
drawdown control).

### Config additions
```yaml
sizing:
  kelly_fraction: 0.25              # quarter-Kelly
  max_kelly_fraction_of_bankroll: 0.05   # hard cap per bet regardless of f*
  bankroll_usd: 1000                # reuse existing top-level if present
```

### Schema/DDL
```sql
ALTER TABLE flat_bets ADD COLUMN kelly_f_star REAL;
ALTER TABLE flat_bets ADD COLUMN kelly_stake_usd TEXT;   -- Decimal as string
ALTER TABLE flat_bets ADD COLUMN pnl_kelly_optimistic TEXT;
ALTER TABLE flat_bets ADD COLUMN pnl_kelly_confirmed TEXT;
```

### Tests (exact, Decimal)
- `p̂=0.55`, `c=0.5175` (Kalshi ask 0.50 + fee 0.0175): `b=(1−0.5175)/0.5175=0.93237…`,
  `f*=(b·0.55−0.45)/b`. Compute by hand, assert to 6 dp. Quarter-Kelly stake on $1000 asserted.
- `p̂=0.40`, `c=0.50` → `f* < 0` → `kelly_stake=0`, row still recorded.

### Acceptance
Report shows four equity curves with drawdowns; flat-$1 headline unchanged; Kelly numbers
reproducible from raw log.

---

# PHASE 4 — Same-oracle BTC 15-minute arb experiment (borrow from AsArchitects)

### Why
Your cross-venue **sports** arbs net ≈$0 on confirmed fills because settlement rules differ
between venues (extra innings, voids, different oracles — SPEC §12). 15-minute BTC up/down
markets can settle off a *price index*, so a genuine (tiny, fleeting) cross-venue edge can
exist **only if both venues reference the same settlement source**. This phase tests that —
honestly, as a paper experiment.

### Steps (fixtures-first, per hard rule #5)

4.1 **Verify the markets exist and read both rulebooks BEFORE coding.**
- Kalshi: find the BTC short-horizon series via `GET /series?category=...` then
  `GET /markets?series_ticker=...&status=open`. Record one real response to
  `tests/fixtures/kalshi_btc_15m.json`. Note the **exact settlement source** in the market
  rules (e.g. which BTC index/timestamp).
- Polymarket: find the 15-min BTC up/down event via Gamma
  (`/events?...&closed=false`), executable book via CLOB `/book`. Record
  `tests/fixtures/polymarket_btc_15m.json`. Note **its** settlement source.
- **If the two settlement sources are not provably identical, the pair stays REVIEW** and is
  never auto-filled (hard rule #6). Write the comparison into `PREREGISTRATION.md` as an
  appended, dated note.

4.2 **Matching key** = `(asset=BTC, direction=UP/DOWN, settlement_window_end_utc)` matched
within a tight tolerance (config `btc_window_match_secs`, e.g. ±5 s). Never interpolate
windows — exact end-time match only (mirror the totals "exact line" rule).

4.3 **Detection** reuses your existing true-arb math (§8): effective cost of UP on venue A +
DOWN on venue B, fees included, `< $1` → arb. Record to a **new ledger section**
`kind='ARB_BTC'`, with optimistic + confirmed fills like everything else.

4.4 **Pre-register first.** Append to `PREREGISTRATION.md` (dated): hypothesis "same-oracle
BTC 15-min cross-venue arb produces positive *confirmed* P&L over n≥100 flags," with a kill
criterion (confirmed median ≤ 0). Only then accumulate data.

### Config additions
```yaml
btc_arb:
  enabled: true
  assets: [BTC]
  window_match_secs: 5
  trusted: false          # stays REVIEW until both rulebooks confirm identical oracle
  max_window_minutes: 15
```

### Tests
- Fixture where UP(A)=0.48, DOWN(B)=0.49, fees make sum 0.985 → ARB_BTC flagged, ROI per §8.
- Window end-times 7 s apart with `window_match_secs=5` → **not** matched.

### Acceptance
`arb watch --once` (dry-run, fixtures) emits ARB_BTC flags; pre-registration note exists;
pair shows REVIEW until you've documented identical settlement sources. Net confirmed P&L
over the live run is the result — positive or not, it's citable.

---

# PHASE 5 — Polymarket whale-flow tracker + copy-trade experiment (measurement only)

### Why
Copy-trading as *blind mirroring* is mostly unprofitable (adverse selection, one-leg-of-a-
hedge, zombie-order win-rate inflation, bot spread-capture you can't replicate). But whale
flow is a **useful signal** and, run as a pre-registered experiment, a strong, novel résumé
result. **Polymarket only** — it's on-chain; Kalshi has no public trader identities.

### Steps (fixtures-first)

5.1 **Verify the data source BEFORE coding (hard rule #5).** Identify the real, public
Polymarket source for per-wallet trade/position history (on-chain CTF transfers via Polygon,
or Polymarket's public data API — whichever you can fetch unauthenticated). Record one real
response to `tests/fixtures/polymarket_wallet_activity.json`. Do not guess endpoints; if one
404s, stop and report.

5.2 **Wallet universe + filters (config).** Ingest candidate wallets and compute per-wallet,
fee-adjusted, **on settled markets only**: trade count, win rate, holding time, category
concentration, realized P&L. Keep only wallets with:
- `>= min_trades` (default 100),
- `>= min_history_days` (default 120),
- win rate `>= wr_floor` (default 0.60). Wallets **above** `wr_ceiling` (0.72) stay qualified
  but are **flagged `suspect`** (usually zombie-order illusion) — kept on purpose so you can
  test whether the "too good" cohort actually underperforms after slippage, rather than
  silently dropping them,
- single-category concentration `>= min_category_share` (they have a lane).

5.3 **Signal, not autopilot.** When a qualifying wallet opens a position, log a **paper**
copy bet at the *next observable executable price* (CLOB book), with realistic slippage —
i.e. you fill *after* the move, as a taker. This is the honest test. Record optimistic
(whale's price) vs confirmed (your post-move price) — the gap *is* the adverse-selection cost.

5.4 **Exclude the un-copyable.** Hard-skip BTC/ETH 15-min bot wallets (spread-capture; you'd
eat the edge). Flag multi-leg hedgers and do **not** copy a single visible leg.

5.5 **Pre-register first.** Append to `PREREGISTRATION.md` (dated): "filtered smart-money
entries beat their own market close after realistic slippage, mean CLV>0, bootstrap 95% CI
excluding 0, n≥40 per wallet-cohort." Kill criterion: CLV ≤ 0.

### Config additions
```yaml
whale_tracker:
  enabled: true
  venue: polymarket
  min_trades: 100
  min_history_days: 120
  wr_floor: 0.60
  wr_ceiling: 0.72         # above this -> suspect (zombie-order inflation)
  min_category_share: 0.50
  skip_categories: [btc_15m, eth_15m]   # bot spread-capture, un-copyable
  copy_fill: next_executable            # honest: fill after the move, as taker
```

### Schema/DDL
```sql
CREATE TABLE IF NOT EXISTS wallets (
  address TEXT PRIMARY KEY, first_seen TEXT, n_trades INTEGER,
  win_rate REAL, pnl_usd TEXT, top_category TEXT, category_share REAL,
  qualified INTEGER, suspect INTEGER
);
CREATE TABLE IF NOT EXISTS wallet_trades (
  id INTEGER PRIMARY KEY, address TEXT, observed_at TEXT, market TEXT,
  side TEXT, price TEXT, size_usd TEXT, settled INTEGER, result TEXT
);
-- copy bets reuse flat_bets with kind='COPY', optimistic=whale price, confirmed=your fill
```

### Tests
- Wallet with 80 trades → not qualified. Wallet 150 trades / 0.66 WR / 0.8 politics share →
  qualified, not suspect. Wallet 0.85 WR → qualified but `suspect=1`.
- A copy bet records optimistic=whale entry, confirmed=next CLOB executable; CLV computed.

### Acceptance
`arb whales` lists qualified wallets; a live run produces COPY paper bets with an
optimistic−confirmed gap; pre-registration note exists. The finding (likely "raw signal
exists, slippage eats most of it") is the citable result.

---

# PHASE 6 — Read-only live dashboard (borrow from ImMike)

### Why
You have 919k quotes and CSV reports but nothing visual. A simple read-only view over the
existing ledger materially upgrades the "show a recruiter" story. **Read-only** — it must
never place trades.

### Option A (simplest, recommended): static HTML + generated JSON
6.1 Add `arb dashboard` that queries `data/arb.sqlite` and writes
`reports/dashboard.json` (equity curves, per-segment scores, win rate, ROI, n, drawdowns,
optimistic-vs-confirmed gap, "left on table").
6.2 A single `reports/dashboard.html` (Chart.js from CDN) loads that JSON and renders:
cumulative P&L (4 curves), per-segment score table, EV edge-bucket bars (clean vs raw),
copy-trade CLV. No backend, no secrets, host on GitHub Pages.

### Option B: live artifact
If you want it to refresh from connectors on open, build it as a live HTML artifact instead
of static — but Option A is enough for a portfolio.

### Acceptance
`arb dashboard` regenerates the JSON; opening the HTML shows the four equity curves and the
segment-score table; no network calls to trading endpoints; safe to publish.

---

# PHASE 7 — The writeup and recruiting package (the actual deliverable)

### Why
The point isn't the bot — it's a **citable result you can defend in a first round**. Package
it.

### Steps
7.1 **`RESULTS.md`** — the headline study. Structure:
- Question, data (venues, date range, n quotes, n settled bets).
- Method: link `PREREGISTRATION.md`; describe flat-$1 ledger, optimistic vs confirmed fills,
  de-vig consensus, settlement reconciliation.
- **Finding 1:** cross-venue sports arb edge collapses to ≈$0 on confirmed fills (the
  mirage). Show the optimistic−confirmed gap with bootstrap 95% CI.
- **Finding 2:** EV signal is segment-dependent; after Phase 1 cleaning, which segments
  survive (and the broad ones don't). Show the score table.
- **Finding 3 (if run):** BTC same-oracle arb result; whale-copy CLV-after-slippage result.
- Limitations: paper fills, sample size, no execution latency model. State them plainly —
  honesty is the selling point.
7.2 **One-page interview cheat-sheet** (`INTERVIEW_NOTES.md`): the 3 findings, the 3 hardest
methodology questions and your answers (de-vig bias, why flat-$1 not Kelly for headline,
why confirmed-fill proxies slippage).
7.3 **README polish:** badges, a single screenshot of the dashboard, a "reproduce in 3
commands" block, link to RESULTS.md and PREREGISTRATION.md.
7.4 **Make the repo public** (after confirming no keys/PII in history — `git log -p | grep`
for `ODDS_API_KEY`, private keys, emails; if anything leaked, rewrite history or start a
fresh clean repo).

### Acceptance
A stranger can clone, install, run, and read RESULTS.md to understand the finding in 5
minutes. The pre-registration, the honest mirage result, and the clean history are the three
things that survive scrutiny.

---

# Suggested order & rough effort

1. **Phase 0** repo hygiene — 0.5 day (do first, always).
2. **Phase 1** EV de-contamination — 1–2 days (blocks 2 & 3; do before any sizing).
3. **Phase 2** category scorer — 1–2 days.
4. **Phase 3** Kelly column — 1 day.
5. **Phase 6** dashboard — 1 day (can run in parallel; uses only existing data).
6. **Phase 4** BTC same-oracle arb — 2–3 days (new venue plumbing + rulebook reading).
7. **Phase 5** whale tracker — 3–5 days (new data source; highest novelty).
8. **Phase 7** writeup — 1–2 days, ongoing.

Phases 0–3 + 6 + 7 alone produce a recruiter-grade artifact. Phases 4–5 add novelty if time
allows before sophomore recruiting.

# Definition of done (recruiting bar)
- [ ] Clean public git repo, green `pytest`, single package, README leads with method+result.
- [ ] EV signal de-contaminated; clean vs raw columns shown.
- [ ] Category scorer blocks no-edge segments; reproducible from raw log.
- [ ] Flat-$1 headline + parallel quarter-Kelly curves with drawdowns.
- [ ] Dashboard renders the equity curves and segment scores.
- [ ] `RESULTS.md` states the mirage finding with bootstrap CIs and limitations.
- [ ] `PREREGISTRATION.md` shows every hypothesis was frozen before its data.
- [ ] (Bonus) BTC same-oracle arb and whale-copy-after-slippage experiments, each
      pre-registered, each with an honest positive-or-negative result.

---

# PART II — STRATEGY CATALOG (researched, graded, with build instructions)

This part catalogs the significant prediction-market trading strategies, grades each on
**real-world effectiveness** and **fit with this paper-measurement project**, and gives build
instructions for the ones worth adding. Grades:

- **A** = real, durable edge, low settlement risk, implementable as a paper experiment now.
- **B** = real edge but latency/infra-sensitive; implement as a measured signal.
- **C** = real edge but requires placing **live maker orders** → conflicts with hard rule #1
  (paper-only). Build the *measurement/simulation*; do not execute. Going live is your call,
  not this plan's.
- **D** = overrated / structurally negative; do **not** build as a strategy (measure to
  debunk, or avoid).

### Effectiveness summary table

| # | Strategy | Grade | Settlement risk | Edge source | Status in your repo |
|---|----------|-------|-----------------|-------------|---------------------|
| S1 | Intra-market negative-risk / convert arb (multi-outcome, single venue) | **A** | very low (same venue, same resolution) | law-of-one-price violation | **missing — highest-value add** |
| S2 | Favorite-longshot bias fade | **A** | none (pure directional, measured) | robust behavioral bias | missing |
| S3 | Same-oracle cross-venue latency arb (BTC 15-min) | **B** | low *iff* identical oracle | speed vs index price | Phase 4 (build it) |
| S4 | Event/news-latency directional | **B** | low | information speed | missing |
| S5 | De-vigged sharp-consensus +EV | **B** | medium (your current EV) | crowd vs sharp books | exists, needs Phase 1 clean |
| S6 | Market making / spread capture + liquidity-reward farming | **C** | low | rebates + spread | measure only (needs orders) |
| S7 | Cross-venue sports moneyline/totals arb | **D** | **high** (oracle mismatch) | apparent only | exists, nets ≈0 confirmed |
| S8 | Blind whale copy-trading | **D** | n/a | adverse-selected | Phase 5 (as debunk study) |
| S9 | AI-ensemble "pick the winner" on macro (CPI/Fed) | **D** | n/a | none (efficient) | avoid |

Evidence base (cite these in RESULTS.md — they make the work look serious): a 2024–2025
study documented **>$40M of arbitrage extracted from Polymarket** across 86M bets even in
liquid markets; favorite-longshot bias is "the single most robust finding" in the literature
(longshots over 40/1 lose >40%, heavy favorites lose ~7%); Yale's "wisdom of the few" shows
markets are driven by a small number of skilled traders. Sources listed at the end.

---

## S1 — Intra-market negative-risk / convert arbitrage  (Grade A — build first)

### Why this is the most important add
Your current cross-venue arbs net ≈$0 confirmed because **settlement rules differ between
venues**. S1 has **no cross-venue risk at all**: it trades multiple mutually-exclusive,
exhaustive outcomes **within a single Polymarket event**, which all settle off the *same*
resolution. The law of one price says the YES prices of the full outcome set must sum to $1.

- **Convert arb:** if `Σ ask(YES_i) < 1 − fees`, buy one share of YES on *every* outcome.
  Exactly one resolves to $1 → guaranteed payout $1 for cost < $1. (Polymarket even has a
  built-in "Convert" function for the negative-risk redemption.)
- **Negative-risk arb:** if `Σ price(YES_i) > 1` (overshoot), buy NO on every outcome / sell
  the overpriced basket.

This is model-free, single-venue, and academically documented as a real, recurring edge
(the $40M study; arXiv NBA-markets and "Probabilistic Forest" papers). It is the single
cleanest thing you can add and the strongest interview story ("I found law-of-one-price
violations inside Polymarket and measured how fast they close").

### Files
New `arb/negrisk.py`; new Polymarket discovery for multi-outcome events in
`arb/venues/polymarket.py`; wire into detection + tracker as `kind='NEGRISK'`.

### Steps (fixtures-first, hard rule #5)
1. **Discover negative-risk event groups.** In Gamma, multi-outcome events expose the member
   markets and a `negRisk`/`negRiskMarketID` style grouping. Record one real response to
   `tests/fixtures/polymarket_negrisk_event.json`. **Verify the group is exhaustive AND
   mutually exclusive** from the event metadata. This is the intra-venue analogue of hard
   rule #6: rule #6 itself is about *cross-venue* settlement mismatch, but the same caution
   applies here — the failure mode is a set that looks exhaustive/exclusive but isn't (a
   missing outcome, or two that can both resolve YES). If you cannot prove
   exhaustive+exclusive from metadata, it is REVIEW, never auto-filled.
2. **Pull executable books** for every member outcome from the CLOB (`/book`), never Gamma
   mids (SPEC §3.2). You need best ask for convert, best bid for the overshoot side.
3. **Detect (Decimal):**
   - Convert: `cost = Σ best_ask(YES_i) + fees`. If `cost < 1` → edge `= 1/cost − 1`.
   - Overshoot: `proceeds = Σ best_bid(YES_i) − fees`. If `proceeds > 1` → edge `= proceeds − 1`.
   - **Depth-aware:** size the basket to the *minimum* executable depth across all legs (one
     thin leg caps the whole basket). Apply `min_executable_usd` per leg.
4. **Record** as `kind='NEGRISK'`, locked-at-entry like ARB, with optimistic and confirmed
   fills. Because it's single-venue + same resolution, it can be `trusted` once you've
   verified exhaustive+exclusive — but keep a manual whitelist of verified event types.
5. **Pre-register** (dated append): "intra-Polymarket negative-risk flags with edge ≥ X
   produce positive confirmed P&L; how many appear/day and median lifetime (how fast they
   close)." The *lifetime* distribution is itself a publishable result.

### Config additions
```yaml
negrisk:
  enabled: true
  min_edge: 0.005             # 0.5% after fees
  require_exhaustive_exclusive: true   # else REVIEW, never auto-fill
  max_outcomes: 30            # sanity cap on basket size
  verified_event_types: []    # whitelist after manual rulebook check
```

### Schema/DDL
```sql
-- reuse flat_bets with kind='NEGRISK'; legs_json holds every outcome leg + its book.
ALTER TABLE flat_bets ADD COLUMN basket_n INTEGER;     -- # outcomes in the set
ALTER TABLE flat_bets ADD COLUMN basket_sum TEXT;      -- Σ prices at entry (Decimal str)
```

### Tests (exact, Decimal)
- 3-outcome set, asks 0.30/0.30/0.35, fee 0 → Σ=0.95 → convert edge `1/0.95−1=0.052632`.
- 3-outcome set, bids 0.40/0.35/0.30 → Σ=1.05 → overshoot edge `0.05`.
- One leg depth `$10` with `min_executable_usd=50` → basket size capped to $10, flagged
  `thin-leg`, excluded from the clean column.
- Non-exhaustive set (metadata missing an outcome) → status REVIEW, not auto-filled.

### Acceptance
`arb watch --once` (fixtures) emits NEGRISK flags with correct edges; verified event types
settle to locked P&L; pre-registration note records expected daily count + lifetime study.

---

## S2 — Favorite-longshot bias fade  (Grade A — build second)

### Why
The most robust, decades-persistent bias in the literature: longshots are overpriced,
favorites underpriced. It needs **no cross-venue matching and no live orders** — it is a pure
directional signal you can backtest on the 919k quotes you already store, then forward-test
flat-$1. Strong, citable, low-infra.

### Files
New `arb/bias.py`; a backtest entry `arb backtest-flb`; forward signal in detection as
`kind='FLB'`.

### Steps
1. **Backtest on existing data first.** You already persist every quote + settlement. Bucket
   all settled binary outcomes by entry price (e.g. 0.00–0.05, 0.05–0.10, …, 0.95–1.00).
   For each bucket compute realized win rate vs implied probability and the P&L of (a) buying
   YES, (b) buying the favorite side. This reproduces the bias curve **on your own venues**
   — that plot alone is interview gold.
2. **Define the signal from the fitted curve**, not from literature constants: back outcomes
   in price bands where your data shows realized > implied by ≥ `flb_min_edge`; avoid/short
   bands where realized < implied (longshots). Put the bands in config, fitted from ≥ N
   settled samples.
3. **Forward-test flat-$1** as `kind='FLB'`, gated by the Phase 2 category scorer (the bias
   strength differs by category — sports vs politics vs crypto).
4. **Pre-register** the price bands and `flb_min_edge` before forward data accumulates.

> **Execution caveats (logic note — the bias is real but not free to harvest):**
> (a) **You can't short** on these venues. "Fade the longshot" means *buying NO* on the cheap
> outcome — i.e. buying the **expensive** ($0.90+) side. (b) That collides with the **fee death
> zone** (see Part III): a flat per-contract fee is a large % tax near $0 and the thin upside
> near $1 is easily eaten by fees. So the *tradable* FLB bands are the mid-priced ones, not the
> extreme tails where the bias is largest. Add a price floor/ceiling and run the **verified**
> Kalshi fee through every band's break-even before betting it. The calibration *curve* is the
> citable result; the *tradable* slice is narrower than the bias suggests.

### Config additions
```yaml
flb:
  enabled: true
  min_samples_per_band: 200
  flb_min_edge: 0.02          # realized−implied required to bet a band
  bands: [0.0,0.05,0.1,0.2,0.3,0.4,0.5,0.6,0.7,0.8,0.9,0.95,1.0]
  per_category: true          # fit the curve separately per category
  price_floor: 0.15           # fee death-zone guard (verify Kalshi fee first)
  price_ceiling: 0.85
```

### Tests
- Synthetic ledger where the 0.05 band wins 2% of the time (implied 5%) → band flagged
  *avoid/short*; the 0.90 band wins 94% (implied 90%) → flagged *back favorite*.
- A band with < `min_samples_per_band` settled bets → `INSUFFICIENT_DATA`, no signal.

### Acceptance
`arb backtest-flb` outputs the calibration curve (realized vs implied per band, per
category) with bootstrap CIs; forward FLB bets are recorded flat-$1; bands were pre-registered.

---

## S3 — Same-oracle cross-venue latency arb (BTC 15-min)  (Grade B)
Already specified as **Phase 4**. Research confirms it is a real bot edge but latency-
sensitive (Polymarket can lag the spot index by seconds). Keep it REVIEW until you've proven
both venues use the same settlement index. Measure the *confirmed* (post-latency) capture —
that's the honest number.

---

## S4 — Event / news-latency directional  (Grade B — measure as a signal)

### Why
Prices jump violently on discrete news (0.50→0.10 in a second). Traders who ingest news
faster win. You can't beat HFT bots on raw latency, but you **can** measure whether your
venues lag a *public, timestamped* signal — and that measurement is the resume artifact, not
the (infeasible-for-a-student) ultra-low-latency execution.

### Files
New `arb/experimental/newslag.py`; reuse your existing `analysis/leadlag.py` if present.

### Steps
1. **Pick a clean, timestamped reference signal** you can fetch unauthenticated (e.g. the BTC
   index print for crypto markets, or an official results feed for sports finals). Record
   fixtures.
2. **Measure lead-lag:** for events where the reference moves sharply, timestamp when the
   venue mid re-prices. Build the distribution of "venue lag after signal." A consistent,
   null-corrected positive lag = a (theoretical) edge window. This is exactly the H-EV2
   "lead-lag" hypothesis already in your PREREGISTRATION.
3. **Do NOT add live execution.** Log a paper bet at the *next executable price after the
   signal* and measure CLV. If CLV after realistic lag is ≤ 0, the edge isn't capturable by
   you — report that honestly.

### Config additions
```yaml
newslag:
  enabled: true
  reference_signals: [btc_index]   # only sources you can fetch unauthenticated
  min_move_to_trigger: 0.05        # reference move that defines an "event"
  measure_only: true               # never auto-execute
```

### Acceptance
A lag-distribution plot per signal; paper CLV-after-lag with CI; written verdict on
capturability.

---

## S5 — De-vigged sharp-consensus +EV  (Grade B — already yours; fix via Phase 1)
This is your existing EV engine. The edge (crowd venue vs sharp sportsbook consensus) is
real but thin and contaminated; **Phase 1 + Phase 2 are the fix** (depth floors, edge cap,
per-category scoring). No new strategy code — just don't trust it until cleaned and
segmented.

---

## S6 — Market making / spread capture + liquidity-reward farming  (Grade C — measure only)

### Why and the hard-rule caveat
Documented real yield: Polymarket pays **daily maker rebates** (funded by taker fees,
~20–25% rebate share) and **liquidity rewards** for resting limit orders near the midpoint —
rewards accrue **even if unfilled**. Makers pay zero trading fees. This is the closest thing
to "passive yield" in the space. **BUT it requires placing live limit orders**, which
violates hard rule #1. So: build the **simulator/estimator**, not a live quoter.

### Files
New `arb/experimental/mm_sim.py`.

### Steps (simulation only)
1. From your stored order books, simulate resting a maker order of size `S` at `k` cents
   inside the spread. Estimate fill probability from observed subsequent trades/quote moves
   in your data.
2. Model P&L = (spread captured on filled size) + (estimated liquidity reward for resting
   size/tightness) − (adverse-selection cost when the market runs through your quote).
   The adverse-selection term is the one that kills naive MM — model it explicitly.
3. Output an expected daily yield curve vs quote tightness and size. **Label clearly as a
   simulation.** Going live is a separate decision that means lifting hard rule #1.

### Config additions
```yaml
mm_sim:
  enabled: true
  quote_sizes_usd: [50, 100, 250]
  inside_spread_cents: [1, 2, 3]
  reward_model: polymarket_v1     # documented rebate/liquidity-reward params (verify in docs)
```

### Acceptance
A simulated yield-vs-tightness table with the adverse-selection term separated out; a written
note that this is theoretical capture, not executed.

---

## S7–S9 — Known-negative / overrated  (Grade D — do not build as strategies)

- **S7 cross-venue sports arb:** your data already shows confirmed ≈ $0; keep it only as the
  *control* that demonstrates settlement-mismatch risk. Don't invest more in it.
- **S8 blind whale copy:** Phase 5 builds it specifically to **measure that it fails** after
  slippage — frame as a debunk, not a strategy.
- **S9 AI-ensemble macro picking (CPI/Fed):** efficient markets, no retail edge; 0mnjb's own
  data shows ~−65% ROI. Do not allocate. If you want an LLM in the loop, use it as *one
  feature* feeding the EV/FLB models, never as a standalone "pick the winner."

> **Open tension to resolve, not assume (logic note).** S9 says macro is efficient, yet the
> Phase-8.2 base-rate work and some practitioners claim retail *over-prices uncertainty* on
> macro vs historical base rates (e.g. fading an over-hyped "Fed cut" market). These are not
> flatly contradictory: the **topline, heavily-traded** macro contract is efficient (don't try
> to out-forecast the Fed), but a **specific retail-dominated framing** may misprice relative
> to a base rate. Treat this as a *testable hypothesis inside the Phase-8 forecast ledger*, not
> a settled fact: log base-rate-vs-market divergences on macro framings, score Brier vs market,
> and let N≥100 settle it. Until then, allocate nothing to macro — measure only.

---

## Build order for Part II
1. **S1 negative-risk arb** — highest value, lowest risk; do right after Phase 1.
2. **S2 favorite-longshot fade** — backtests on data you already have.
3. **S3 BTC latency arb** — = Phase 4.
4. **S4 news-lag** + **S6 MM simulator** — measured experiments, time-permitting.
5. Treat **S7–S9** as controls / debunks, not allocations.

Each new strategy reuses the same discipline: fixtures-first, `Decimal` math, append-only
ledger with optimistic vs confirmed fills, per-category scoring gate, and a **dated
pre-registration entry before its data accumulates.** That uniformity — many strategies, one
honest measurement harness — is what makes the whole repo defensible.

## Sources (for RESULTS.md)
- IMDEA / $40M Polymarket arbitrage study (86M bets, 2024–2025).
- arXiv 2605.00864 "Arbitrage Analysis in Polymarket NBA Markets"; arXiv 2508.03474
  "Unravelling the Probabilistic Forest: Arbitrage in Prediction Markets"; arXiv 2601.01706
  "Semantic Non-Fungibility and Violations of the Law of One Price."
- Pinnacle / Mgmt Science / Economica on favorite-longshot bias magnitude.
- Yale Insights "Wisdom of the Few — prediction markets driven by a small number of skilled
  traders."
- Polymarket docs: Maker Rebates + Liquidity Rewards programs.

---

# PHASE 8 — Forecasting & calibration module (the "out-predict the market" deliverable)

### Why
Every edge so far is **structural** (arbitrage, law-of-one-price) or **behavioral**
(longshot fade). This phase tests whether you can generate **your own calibrated
probabilities that beat the market price** — the highest-ceiling, most interview-legible
claim you can make. The deliverable is not P&L; it is a **Brier score / log loss / calibration
curve / CLV** showing you out-forecast the market over a real sample. That number is more
defensible in a first round than any return figure, because it can't be faked by a lucky run.

Keep the same discipline as everything else: append-only ledger, optimistic-vs-confirmed
fills, per-category scoring gate, dated pre-registration before data accumulates.

### Core principle
For every question you forecast, log a triple at decision time T:
`(p_you, p_market, t)` → at settlement compute your score **and the market's score on the
same question**, then compare. You only get to claim "out-predicting" if you beat the market's
Brier on N≥100 settled questions with a bootstrap CI excluding zero.

---

## 8.1 — Market-selection screener (do this first; it's 80% of the edge)

### Why
You cannot out-predict deep, sharp-traded markets (election toplines, Fed). Edge lives in
**neglected, low-liquidity, niche** markets. Build the screener before any model.

### Files
New `arb/forecast/screener.py`; CLI `arb forecast-screen`.

### Steps
1. For every open market, compute an **attention/efficiency score** from data you already
   pull: `liquidity_usd`, recent quote/trade count, number of distinct price levels, spread.
2. Rank markets by `efficiency = f(liquidity, activity, spread)`. **Target the bottom
   tranche** (thin, low-activity, wide-spread) within categories where you have or can build
   domain knowledge.
3. Output a daily watchlist of "forecastable" markets (neglected + settle within horizon +
   in a vertical you cover).

### Config additions
```yaml
forecast_screener:
  enabled: true
  max_liquidity_usd: 5000        # ABOVE this = too efficient to out-predict, skip
  min_days_to_settle: 1
  max_days_to_settle: 30
  target_categories: [weather, niche_sports]   # your chosen lane(s)
```

### Acceptance
`arb forecast-screen` prints a ranked watchlist of neglected markets in your target vertical.

---

## 8.2 — Base-rate library (the outside view)

### Why
Most long-tail mispricing comes from people anchoring on narrative, not base rates. A
maintained reference-class library is cheap and is where retail error concentrates.

### Files
New `data/base_rates.yaml` (human-curated, versioned in git); `arb/forecast/baserates.py`.

### Steps
1. For each question type in your vertical, record a reference class and its historical
   frequency with source + date (e.g. "home underdog +7 in league X wins outright: 18% over
   last 4 seasons, source, date").
2. The forecaster starts from the base rate, then applies documented adjustments. Log both
   `p_base` and `p_final` so you can later measure how much your adjustments helped or hurt
   (a great diagnostic: are you adding signal or noise on top of base rates?).

### Acceptance
`base_rates.yaml` is populated for your vertical; forecasts record `p_base` and `p_final`.

---

## 8.3 — One niche model (depth beats breadth)

### Why
Profitable traders have a lane. Build the best probability model you can in **one** vertical.

### Concrete starting points
- **Weather (most *accessible* edge — but not the easiest to profit from):** public
  NWS/ECMWF/GEFS **ensemble** forecasts are free and world-class, and Kalshi settles on
  objective station data. The catch: the naive version is a documented loser (fat-tailed
  errors, a fee death zone, and fast arb bots). Build it the honest way per **Part III** —
  empirical error model, price floor, neglected-city/slow-horizon focus. Fixture first.
- **Niche sports:** Elo or Massey-type ratings + home/rest/injury adjustments, blended with
  market-implied probability; focus on a **less-covered league** where the market is thin.
- Blend rule: `p_you = w·p_model + (1−w)·p_market`, with `w` fit on historical calibration,
  never hand-picked.

### Files
`arb/forecast/models/<vertical>.py` (one module per vertical), fixtures under `tests/fixtures/`.

### Config additions
```yaml
forecast_model:
  vertical: weather
  blend_weight_model: 0.5        # fit from backtest, not guessed
  data_source: ecmwf_ensemble    # verify endpoint + record fixture before coding
```

### Acceptance
Given a fixture, the model emits a `Decimal` `p_you` for a specific market threshold; blend
weight was fit, not assumed.

---

## 8.4 — Forecast ledger + proper scoring (the citable result)

### Why
This is the artifact. It turns "I'm good at predicting" into a measured number versus the
market on identical questions.

### Files
New `arb/forecast/ledger.py`, `arb/forecast/scoring.py`; CLI `arb forecast-log`,
`arb forecast-report`.

### Steps
1. **Log a forecast** as an append-only row: `p_you`, `p_base`, `p_market` (mid at T),
   `executable_price` (best ask at T, for CLV), category, vertical, settle horizon.
2. **At settlement**, outcome `y ∈ {0,1}`. Compute, per forecast, both for you and for the
   market:
   - **Brier** `= (p − y)^2` (lower is better).
   - **Log loss** `= −[y·ln(p) + (1−y)·ln(1−p)]` (clip p to [1e-6, 1−1e-6]).
3. **Headline metrics** (recomputed from the raw log, like the rest of the project):
   - Your mean Brier vs market mean Brier; **Brier skill score** `= 1 − Brier_you/Brier_market`.
   - Same for log loss.
   - **Calibration curve:** bin forecasts by `p_you` (deciles); plot realized frequency vs
     predicted; report calibration error (ECE).
   - **CLV:** did `executable_price` at T beat the venue's closing mid? Mean CLV + CI.
   - All with **bootstrap 95% CIs** once N≥30 (reuse your existing bootstrap method).
4. **Combine-with-crowd betting rule:** only record a *paper bet* (flat-$1, `kind='FORECAST'`)
   when `|p_you − p_market| ≥ edge_min` AND the market passed the 8.1 screener AND the
   vertical's Phase-2 segment score isn't blocked. Otherwise log the forecast for scoring but
   place no bet. (You can be a good forecaster on a question yet have no tradable edge — keep
   the two measurements separate.)
5. **Pre-register** (dated append to PREREGISTRATION.md, before data): "in vertical V, my
   forecasts beat the market's Brier with a bootstrap 95% CI excluding 0 over N≥100 settled
   questions; calibration error < target." Kill criterion: Brier skill ≤ 0 over N≥100.

### Config additions
```yaml
forecast:
  enabled: true
  edge_min: 0.05                 # |p_you − p_market| needed to place a paper bet
  min_n_for_claim: 100           # no "out-predict" claim below this
  logloss_clip: 0.000001
  calibration_bins: 10
```

### Schema/DDL
```sql
CREATE TABLE IF NOT EXISTS forecasts (
  id INTEGER PRIMARY KEY, made_at TEXT, vertical TEXT, category TEXT, market TEXT,
  p_base TEXT, p_you TEXT, p_market TEXT, executable_price TEXT,   -- Decimal strings
  settle_by TEXT, settled_at TEXT, outcome INTEGER,
  brier_you REAL, brier_market REAL, logloss_you REAL, logloss_market REAL,
  clv REAL
);  -- append-only; report recomputes all aggregates from these rows
-- tradable forecasts also create a flat_bets row with kind='FORECAST' (flat-$1).
```

### Tests (exact)
- Brier: `p=0.7, y=1` → `0.09`; `p=0.7, y=0` → `0.49`.
- Log loss: `p=0.7, y=1` → `−ln(0.7)=0.356675`.
- Brier skill: your Brier 0.18, market 0.20 → skill `= 1 − 0.18/0.20 = 0.10`.
- Calibration: feed 100 forecasts at `p_you=0.6` that resolve YES 58% of the time → decile
  bin 0.6 shows realized 0.58, calibration error 0.02.
- A forecast with `|p_you − p_market| = 0.03 < edge_min` → scored but **no** `FORECAST` bet.

### Acceptance
`arb forecast-report` prints: your vs market mean Brier + skill score, log loss, a 10-bin
calibration table, mean CLV, all with bootstrap CIs once N≥30. Below `min_n_for_claim` the
report explicitly says "insufficient sample to claim out-performance." A pre-registration
entry exists. When N≥100 and skill>0 with CI excluding 0, **this is your headline result.**

---

## 8.5 — How to present it (RESULTS.md addition)
Add a "Forecasting" section to RESULTS.md with: the calibration plot, the Brier skill score
vs market with CI, the sample size and vertical, and an honest statement of where you do and
don't beat the market. A single well-calibrated curve that beats market Brier on 100+
neglected-market questions is the strongest "I out-predict" evidence you can hand a recruiter
— stronger than any return number, because it is hard to fake and easy to scrutinize.

### Build order
8.1 screener → 8.2 base rates → 8.3 one model → 8.4 ledger+scoring (the core) → 8.5 writeup.
Do **not** chase a second vertical until the first shows a positive, pre-registered Brier
skill score. One proven niche beats five unproven ones. The chosen first vertical is
**weather** — see Part III for the deep dive and its honest caveats.

---

# PART III — WEATHER VERTICAL DEEP DIVE (the chosen Phase-8 niche)

### Why weather — and the honest caveat
World-class probabilistic forecasts are **public and free** (NWS, ECMWF, GFS/GEFS
ensembles), and Kalshi weather markets settle on **objective NWS station data** — no oracle
ambiguity, no settlement-rule risk (the thing that kills your cross-venue sports arbs). That
is the appeal. **But the naive version is a documented loser:** a public 0-for-32 postmortem
lays out exactly why. Treat weather as a *hard forecasting problem with a free data edge*, not
as easy money. The path below is what makes it a credible vertical rather than a fee donation.

### The three structural traps (and the fix for each)
1. **Gaussian blindness.** Temperature forecast errors have **fat tails**: the "2σ event" a
   normal model says is 5% actually happens ~10–12%. A Gaussian model understates tail risk,
   so it mislabels 75–80% bets as "90% locks."
   **Fix:** build **empirical** error distributions from historical forecast-vs-actual data
   per city (Open-Meteo Historical Forecast API, coverage since ~2021). `p_you` comes from the
   empirical CDF of past errors, never from `norm.cdf`.

2. **The fee death zone.** Kalshi fees bite hardest at low prices: on a $0.05 contract a
   ~$0.01 fee is a 20% tax — you'd need the event to hit >83% just to break even before
   slippage.
   **Fix:** a hard **price floor** (never trade below ~$0.15) and a ceiling (~$0.85). **Verify
   Kalshi's exact weather fee schedule first** — it may differ from the proportional
   `0.07·P·(1−P)` formula in SPEC §3.1; put the *measured* fee model in `config.yaml`, then
   simulate the break-even win-rate at every price tier before any paper bet.

3. **You are exit liquidity.** Weather books are populated by arb bots reacting within seconds
   of each NWS model-cycle update. A 15–60 min polling loop trades on stale signals and feeds
   those bots.
   **Fix:** do **not** race them. Either (a) trade only the *slower* slice — multi-day
   horizons and **less-covered cities** the bots ignore — or (b) keep it measurement-only and
   score your forecast against the *closing* line (CLV), accepting you won't capture the fast
   move. Report capturability honestly.

### Where the edge actually is (synthesis)
Not the headline NY/Chicago near-term markets (bot-dominated, efficient). Look at
**less-covered cities**, **multi-day horizons** (thin books, ensembles still informative),
and **mid-price contracts ($0.15–0.85)**. Your edge is *better-calibrated tails from an
empirical error model* vs a crowd anchoring on the single deterministic forecast.

### Data sources (verify + fixture before coding — hard rule #5)
- **Open-Meteo Ensemble API** — free, no key, CC-BY. ECMWF IFS ENS, GFS ENS (GEFS, ~31–62
  members), ICON EPS, GEM EPS; up to 51 members / 35-day horizon. Member spread → empirical
  threshold-exceedance probability.
- **Open-Meteo Historical Forecast API** — reconstructed past forecasts (since ~2021) to build
  the empirical error distribution and backtest calibration.
- **NWS CLI / ASOS station data** — the official settlement source. Verify the exact station
  per Kalshi city (e.g. NYC = KNYC Central Park) and the LST day window.

### Build steps (maps onto Phase 8)
1. **8.3 model:** ingest the ensemble for the market's exact station + threshold + settlement
   day; convert member distribution → `p_you` via the **empirical** error model (per-city bias
   correction). Fixture first.
2. **Fee / break-even simulator:** before any bet, compute the required win-rate per price tier
   with the *verified* Kalshi fee; enforce the price floor/ceiling.
3. **8.4 ledger + scoring:** log `p_you` vs `p_market`, settle on the CLI, score Brier /
   log-loss **vs the market**, calibration curve, CLV — segmented by city and horizon (some
   will beat market, some won't; that split *is* the finding).
4. **Pre-register** (dated): "empirical-distribution weather forecasts beat market Brier on
   {less-covered cities, ≥2-day horizon} over N≥100 settled contracts." **Kill criteria (learn
   from the 0-32): kill at 10 consecutive losing paper trades, or Brier-skill ≤ 0 at N≥30 — do
   not wait for 32.**

### Config additions
```yaml
weather:
  enabled: true
  stations: { nyc: KNYC, chi: KORD, mia: KMIA, aus: KAUS, lax: KLAX, den: KDEN, phl: KPHL }
  ensemble_source: open_meteo_ensemble     # verify + fixture first
  members_min: 30
  error_model: empirical                   # NEVER gaussian
  history_since: "2021-01-01"
  price_floor: 0.15                         # fee death-zone guard
  price_ceiling: 0.85
  target_cities_first: [aus, den, phl]      # less-covered, less bot-dominated
  min_horizon_days: 1
  kill_after_consecutive_losses: 10
```

### Honest verdict
Weather is the most *accessible* forecasting edge (free, world-class data; objective
settlement) but **not the easiest to profit from** (fat tails, fees, fast bots). For your goal
it is ideal as a **calibration study**: even if you only beat market Brier on the slow,
neglected slice, that is a clean, citable "I out-forecast the market here — and here is exactly
where I don't." Build it as measurement, kill fast, and let the calibration curve, not P&L, be
the result.

### Sources (weather, for RESULTS.md)
- North Lake Labs "What I Learned Losing Money on Kalshi Weather Markets" (0-32 postmortem:
  Gaussian blindness, fee death zone, exit-liquidity).
- Kalshi Help Center — Weather Markets (CLI/ASOS settlement, stations).
- Open-Meteo Ensemble API + Historical Forecast API docs.

---

# UNIFIED MASTER SEQUENCE (how Phases 0–8 and Strategies S1–S9 interleave)

The doc has two numbering schemes (Phases = infrastructure/deliverables; S1–S9 = strategy
catalog, where **S3 = Phase 4**). Execute in this single order:

1. **Phase 0** repo hygiene.
2. **Phase 1** EV de-contamination (blocks everything downstream).
3. **Phase 2** category scorer → **Phase 3** Kelly column.
4. **S1** negative-risk arb (highest-value strategy add).
5. **S2** favorite-longshot fade (backtests on existing data).
6. **Phase 6** dashboard (parallelizable; existing data only).
7. **Phase 8 + Part III** forecasting/calibration on the weather vertical (your "out-predict"
   result).
8. **Phase 4 (=S3)** BTC same-oracle arb; **Phase 5 (=S8)** whale-copy debunk; **S4** news-lag;
   **S6** MM simulator — time-permitting experiments.
9. **Phase 7** writeup, ongoing throughout.

Minimum recruiter-grade artifact = steps 1–7. Steps 8 add novelty before sophomore recruiting.
