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

### Steps

3.1 **Kelly for a $1-payout binary.** For a contract bought at effective cost `c` with model
probability `p̂` (consensus for EV bets), net odds `b = (1 − c) / c`, `q = 1 − p̂`:
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
- win rate in `[wr_floor, wr_ceiling]` (default 0.60–0.72; above 0.72 is usually
  zombie-order illusion — treat as suspect),
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
   mutually exclusive** from the event metadata — if you cannot prove exhaustive+exclusive,
   it is REVIEW, never auto-filled (hard rule #6 logic applies intra-venue too).
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

### Config additions
```yaml
flb:
  enabled: true
  min_samples_per_band: 200
  flb_min_edge: 0.02          # realized−implied required to bet a band
  bands: [0.0,0.05,0.1,0.2,0.3,0.4,0.5,0.6,0.7,0.8,0.9,0.95,1.0]
  per_category: true          # fit the curve separately per category
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
