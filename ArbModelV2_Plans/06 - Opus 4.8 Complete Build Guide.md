---
title: Opus 4.8 — complete from-scratch build guide (model + UX/UI)
type: guide
project: ArbitrageModel
model: opus-4-8
status: stable
tags: [arbmodel, v2, build-guide, opus48, ui]
created: 2026-07-01
---

# Opus 4.8 — Complete Build Guide: ArbModel V2 from scratch

> [!abstract] Mission
> You are Opus 4.8 and you own the **entire** build: architecture, math, code, tests, UX/UI, and verification. Build **ArbModel V2** — a paper-only, append-only measurement harness that flags and honestly grades cross-venue prediction-market edge (Kalshi / Polymarket / SX Bet vs sportsbook consensus). V1 exists and produced 3 weeks of data; V2 is a rebuild that fixes every catalogued defect **by construction**. This guide is self-contained; where it cites an ID (A1, EV#3, R1, K10…) the full context is in [[02 - Weaknesses and Lessons]], [[00 - How It Works and What The Data Says]], and [[04 - Code Audit Findings (New)]].

**The one organizing principle:** V1's math was honest but the paths that *booked money* ignored detection's safeguards (314/314 settled arbs were forbidden REVIEW arbs). V2 defines each rule **once, in one module, consumed everywhere**: one eligibility predicate, one leg-cost function, one lifecycle definition, one P&L function. Any second implementation of any of these is a rejected diff.

---

## 1. Hard rules (violating any one fails the build)

1. **Paper only.** No order-placement or account endpoint, ever. Public market-data reads; the only credentials are `ODDS_API_KEY` (query param) and — if you enable them — read-only WS feed keys, each an explicit config opt-in (`feeds.<venue>.allow_readonly_key: false` default).
2. **Never spend Odds API credits in tests or loops.** Tests run on `tests/fixtures/` only. Log `x-requests-remaining` on every live call; abort the scan below `odds_api.abort_below_credits` (50).
3. **No invented endpoints or fields.** Only §5's listed endpoints. On 404/4xx: stop and report.
4. **No magic numbers.** Every threshold, fee, buffer, cadence, and weight is a `config.yaml` key. (V1 regressions to catch in review: hardcoded CLV depth floor, report bucket edges.)
5. **Money math is `Decimal` end-to-end.** Floats only for display and inside de-vig root-finding (results re-quantized `q6`). **All stored P&L is 6 dp (`q6`), never cent-rounded** — K10: `money()` rounding erased 154/315 settled arbs ($0.00 booked on sub-0.5% ROIs). Cents are a rendering concern.
6. **Cross-venue is REVIEW-only** unless `venue_pair` (optionally `venue_pair:sport`) is whitelisted in `config.trusted_pairs` (ships empty — whitelisting is the user's policy call).
7. **Persist every quote** observed, opportunity or not.
8. **`flat_bets` is append-only.** No UPDATE/DELETE; corrections are new rows; excluded bets go through the immutable `annotations` table.
9. **Fixtures first.** Record one real response per venue per market type before any parser is trusted; the SX maker-inversion test (`taker = 1 − percentageOdds/1e20`) must pass from a fixture before SX touches the pipeline.
10. **V1's archive is read-only oracle data**: `data/archive-pre-v2/arb.sqlite` (2.16 GB, 11,806 bets, 2.94 M quotes). Open with `file:...?mode=ro`. Use it to fit/choose (de-vig method, push table, calibration) — never write to it.

## 2. Stack and layout

Python 3.11+. Pinned deps: `requests`, `rich`, `rapidfuzz`, `schedule`, `python-dotenv`, `pyyaml`, `pytest`, `websockets` (Polymarket WS only). Stdlib `sqlite3` (WAL), stdlib `http.server` for the cockpit. `.env` (git-ignored) holds `ODDS_API_KEY`; ship `.env.example`. Modules stay under ~300 lines.

```
arb/
  core/            pricing.py  eligibility.py  lifecycle.py  positions.py  pnl.py
  venues/          kalshi.py  polymarket.py  sx.py  oddsapi.py  mlbstats.py  ws_polymarket.py  legcost.py
  engine/          matching.py  detect_arb.py  detect_negrisk.py  detect_divergence.py  detect_ev.py
                   verify.py  tracker.py  settle.py  reconcile.py  exposure.py  capacity.py
  stats/           devig.py  consensus.py  calibration.py  push.py  clv.py  scoring.py
  io/              config.py  store.py  schema.py  report.py  daily.py
  ui/              webserver.py  state.py  static/cockpit.html
  cli.py  __main__.py
tests/  tests/fixtures/  config.yaml  status.sh
```

## 3. The four singletons (define once, import everywhere)

**3.1 `core/eligibility.py` — THE predicate (F2).**
```python
def eligible(bet) -> bool:
    return ((bet.trusted or bet.venue_pair in cfg.trusted_pairs
             or f"{bet.venue_pair}:{bet.sport}" in cfg.trusted_pairs)
            and bet.start_utc is not None                # D1/K9: never invented
            and bet.placed_at < bet.start_utc            # A3/C5
            and bet.depth_basis_real)                    # B3/D4: no event-liquidity fallback
```
Called — as this object, never re-implemented — in: detection, fill, verify eligibility, settle, reconcile, capacity, exposure, and every report/UI "clean" aggregate. VERIFY step greps for it (§10).

**3.2 `venues/legcost.py` — THE leg-cost function (fixes K1/K2 by construction).**
```python
def leg_effective_cost(venue, raw_price, ladder, *, cfg) -> Decimal
```
Fee bases: Kalshi `rate·p·(1−p)` quadratic (`kalshi.fee_rate: 0.07`); Polymarket linear `rate·p` (`polymarket.sports_fee_rate: 0.03`); SX linear + winning-leg term `win_fee_rate·(1−p)` (`sx.win_fee_rate`, R4/D3); plus the slippage buffer (`execution.slippage_model: flat|ladder`, `taker_price_buffer: 0.005`, `slippage_ref_stake_usd: 50`). **Adapters (entry), the flash verifier (re-quote), and settlement all call this one function.** Parity test per venue: verifier cost at price p == adapter `effective_cost` at price p, from the same fixture. (V1 died here: verifier priced Kalshi linear — 0.6–3.4 pts of phantom cost, 24/161 VANISHED were fee artifacts.)

**3.3 `core/lifecycle.py`** — ACTIVE={OPEN,CONFIRMED}, TERMINAL={CLOSED,SETTLED,EXITED}; annotation rows (VERIFIED/VANISHED/UNVERIFIABLE/LIFETIME/RECONCILED/CORRECTED) are never lifecycle transitions. One indexed "latest lifecycle row per fingerprint" query.

**3.4 `core/pnl.py`** — `sized_pnl(effective_cost, result, stake) -> Decimal(q6)`: WON `stake·(1−c)/c`, LOST `−stake`, VOID `0`; ARB lock `stake·(1/S−1)`. **q6, not cents (K10).**

## 4. Data model (SQLite, WAL; `Decimal` stored as TEXT)

**`quotes`** — `id, poll_id, fetched_at` (**per-market read time, K11** — never one batch stamp), `event_key, sport, venue, book, market_type, line, outcome, raw_price, cost, fee_cost, best_bid` (**populated by every adapter, K19** — Polymarket `/book` bids, Kalshi both paths incl. TOTAL, SX opposing makers), `depth_usd, ladder_json, start_utc` (**K18**), `live` (**derived: `fetched_at >= start_utc`, K18** — never hardcoded), `source_updated_at`.

**`flat_bets`** — `id, fingerprint, position_key, venue_pair, reappearance_of, opened_poll_id, category, sport, kind` (ARB|EV|DIVERGENCE|NEGRISK|PARLAY|CLOSE), `trusted, live, edge` (ratio, display-only), `edge_points` (**populated on every kind incl. ARB, R6/K4**), `adj_edge_points` (**post skew-haircut, a real column — K4/K5**), `guaranteed_floor, stake_usd, legs_json, optimistic_cost, confirmed_cost, confirmed_source` ('flash-verify'|'second-sighting', R1), `requote_sum, requote_lag_secs, seq_worst_edge` (**columns, not note strings — K5**), `pnl_optimistic, pnl_confirmed, pnl_kelly_optimistic, pnl_kelly_confirmed` (all q6), `kelly_f_star, kelly_stake_usd, status, result, provisional` (0/1 until venue-reconciled), `placed_at, settled_at, quality_flag, validation_rule, validation_tag, notes` (**human prose only**).

`position_key = sport|event_key|market_type|line|sorted(outcome labels)` — every aggregate dedups on it first (C2/B2: V1's 1,280 arb rows = 149 positions).

**`polls`** — `id, kind, started_at, finished_at, n_quotes, n_opps, credits_remaining, venue_health_json` (**K3**: per venue `{ok: bool, quotes: n, error: str|null}`).

Plus: `consensus` (with `consensus_history` retained per E6: append, don't upsert-destroy), `suspect_matches`, `annotations` (immutable exclusions), `segment_scores`. Indexes: `flat_bets(fingerprint, status)`, `flat_bets(position_key)`, `quotes(event_key, venue, book, outcome, fetched_at)`.

## 5. Venue adapters (precise)

| Venue | Endpoints (REST) | Musts |
|---|---|---|
| Kalshi | `api.elections.kalshi.com/trade-api/v2` `/markets?series_ticker=…` (paginated, 200/page), `/markets/{t}/orderbook` | **Price from the orderbook read itself** (`ask = 1 − best opposing bid`), stamped with that read's time — never the stale `/markets` snapshot price (K11). Quadratic fee. Ticker time (`26JUN101940…`) is US-Eastern wall clock; unparseable → `start-unknown`, non-actionable, **never** `close_time` fallback (D1). Rate-limit 0.15 s + 429 backoff. Empty book = ask 1.0/bid 0.0/liq 0. `best_bid` on WINNER **and** TOTAL paths (K19). |
| Polymarket | `gamma-api.polymarket.com/events?tag_slug=…` (**paginate past 100, K16**), `clob.polymarket.com/book?token_id=…` (pool of 6) | Executable prices from CLOB `/book` only — Gamma `bestAsk/outcomePrices` are stale mids, banned (this ban is venue-wide: it killed the BTC lane, K20). Capture the full ask ladder (B4) **and best bid** (K19). Linear fee. No liquidity fallback for depth — unknown depth is `None` (B3/K17). |
| SX Bet | `api.sx.bet` `/leagues/active`, `/markets/active?onlyMainLine=true` (paginated), `/orders` (batches of 20) | Maker-side inversion `taker_cost = 1 − percentageOdds/1e20`; taker $-notional `= maker_stake·c/(1−c)`. Winning-leg fee in `legcost` (R4). `settlement-rule-review` note on every leg (void rules differ). Store `ref` (outcome side 1|2) for re-quote + resolution. UA header required (403 otherwise). |
| Odds API | `api.the-odds-api.com/v4/sports/{sport}/odds` (`h2h,totals`, decimal), `/scores?daysFrom=3` | Consensus + EV settlement only, never an arb leg. Credit tracking per rule 2. Keep `last_update` as `source_updated_at` (C16). Plausibility gate: decimal odds ∈ [1.002, 500]. |
| MLB Stats | `statsapi.mlb.com/api/v1/schedule` | Keyless finals for MLB settlement/backfill/push-table fitting. |

**WebSocket (R8, build after REST works):** `ws_polymarket.py` — `wss://ws-subscriptions-clob.polymarket.com/ws/market`, subscribe by `token_id`, keyless; maintain a live local book (ping 5 s / pong ≤10 s, auto-resubscribe on drop; REST is the fallback and stays authoritative for discovery/settlement). Kalshi WS (signed headers) and SX realtime (token via API key) ship **behind the per-venue `allow_readonly_key` opt-ins, default off** — document that this relaxes "unauthenticated" but never touches an order path.

## 6. Engine rules (precise)

**Activity gating** (`engine/activity.py`, shipped in V1 2026-07-02): sports gated on venue-derived activity — a sport is active iff a free venue lists a fixture starting within `activity_lookahead_hours: 48`; dormant sports cost zero Odds API credits and zero absent-closes (their fingerprints are exempt like a failed venue). No hardcoded season calendars.

**Matching** (`engine/matching.py`): per-sport alias table (`data/aliases.json`), names lowercased/punct-stripped/`fc|sc|cf|the` dropped; `event_key = sport:date(America/New_York):team1:team2` (sorted). **Cluster legs by start time ≤ `matching.doubleheader_cluster_tol_secs` (7200) before pairing** (D2); untimed legs never actionable. Gates: start-drift > 30 min → suspend; same-outcome gap > 15 pts → `suspect_matches` row (never silent-drop, E2).

**Arb detection** (`detect_arb.py`): group by (event_key, market_type, line) → per-cluster best ask per outcome; soccer WINNER needs 3 outcomes; `S = Σ leg_effective_cost`; arb iff `S < 1`; `ROI = 1/S − 1`; `edge_points = 1 − S`. Skew haircut (C1): `adj_edge_points = edge_points − skew_secs·line_velocity[sport]`; **stored as a column and consumed by sizing/ranking** (K4) — sizing runs *after* the haircut; skew > `max_actionable_skew_secs` (10) → REVIEW. Stakes proportional to leg cost, cent-rounded per leg, report the true `guaranteed_floor` (negative floor → REVIEW `rounding-broke-lock`). ROI > `stale_warn` → REVIEW. Capital charge to the **slowest venue's settlement** (start + `sport_duration_hours` + `venue_settlement_lag_hours`, C4). Never fabricate `Event.start = now` (K9).

**Neg-risk** (`detect_negrisk.py`): one Polymarket negRisk event; prices from CLOB books; **linear fee both sides** (C0); convert `Σasks+fees<1`, overshoot `Σbids−fees>1`; basket capped at min leg depth.

**Flash verifier** (`engine/verify.py`): re-quote every leg of each fresh OPEN arb ≤ `verify.max_bet_age_secs` via **`leg_effective_cost`** (K1/K2 parity). Writes `requote_sum`, `requote_lag_secs`, `seq_worst_edge` (worst over held-leg orderings) as **columns**, appends VERIFIED/VANISHED/UNVERIFIABLE, and **on VERIFIED writes `confirmed_cost = requote_sum` with `confirmed_source='flash-verify'` (R1)** — this is what gave V1 5/1,280 arb confirms. Fixture tests must exercise the *live* fetchers' pricing (V1's mocks hid K1).

**Tracker** (`engine/tracker.py`): $1 flat per new position; dual fill (optimistic first sighting; confirmed via R1 for arbs, second consecutive sighting for EV); dedup by `position_key`; reappearance links. **Venue-outage guard (K3):** before closing a fingerprint as absent, check the poll's `venue_health_json` — a fingerprint with a leg on a failed/zero-quote venue is exempt that poll (and/or require `tracker.absent_polls_to_close: 2`). V1 booked 240 spurious CLOSEs across 43 dead-venue polls. In-play/missing-start rejections as diagnostic rows.

**Settle / reconcile** (`settle.py`, `reconcile.py`): Odds API scores + MLB backfill; TOTAL: `total == line → VOID`; soccer draw is settleable. A3 guard checks **all** legs' starts, at settle too. A1: untrusted settles carry `quality_flag='REVIEW'`, never the clean lane. Resolver map `{kalshi, polymarket, sx}` (Kalshi `result` yes/no; Polymarket closed + `outcomePrices` snap, 0.5/0.5 → VOID; SX `outcome` 1/2/3 — fail closed until the field is live-verified once). Realized P&L `provisional=1` until RECONCILED/CORRECTED child exists; reconcile runs automatically after settle.

**EV path** (`stats/`, `detect_ev.py`) — **measurement only; auto-betting stays disabled** until the pre-registered gate clears:
- De-vig: proportional, power, Shin implemented; **default chosen by Brier/log-loss fit on the V1 archive's settled raw quotes** (Phase 0 deliverable — freeze the winner in `config.devig_method` with the fit script committed).
- Consensus: modal complete outcome set (C14); weighted median (sharp books ×`sharp_weight`); renormalize the median vector to 1; per-outcome SE with effective-sample-size correction `n_eff = n/(1+(n−1)·ρ)` (`book_correlation`); totals get a lower book gate.
- `edge_points = p̂ − c` is primary; ratio display-only (EV#4). Gate on `edge_points/SE ≥ ev.min_edge_se_ratio` (EV#3).
- **Edge inversion (R5):** V1 measured win-rate falling with apparent edge (51% at <2% → 31% at 5–10%). Hard-gate `edge_points > ev.max_credible_edge` into quarantine; never rank/size ascending-by-edge. Scope segments to `ev.enabled_segments` (MLB) — Polymarket was the only positive venue.
- Calibration: isotonic on settled **raw** quotes from the archive (EV#5), per-segment ECE with `ece_min_n: 200` floor; **ECE computed on stored raw `p_hat` (its own column), never reconstructed post-haircut (K14)**.
- Push: `stats/push.py` fits an **empirical** integer-total push table from archive settled totals; Poisson(λ=line) only as documented fallback (it overstates MLB push ~13% vs 8–9%, K14).
- Staleness: continuous exponential decay on `source_updated_at` age (EV#7); shrinkage config split into **two keys** — `shrink_prior_sd` and `shrink_prior_pseudocount`, error if both set (K7).
- **CLV** (`stats/clv.py`): default gate = no-vig **true mid** on both ends: `mid = (best_bid+ask)/2` per outcome (bids exist now, K19), renormalized two-sided, keyed by the flag's own `(market_type, line)` (**totals included — V1 silently dropped 39% of flags, K12**), close = last quote with **`fetched_at <= start_utc`** (**8% of V1 "closes" were in-play, worst 288 min — K18**). Fee-loaded variant exists behind a flag, labeled BIASED. Bootstrap CI, fixed seed.

**Exposure / capacity**: per-venue committed = **that venue's leg stake only** (K6, not the full bet), duration-weighted $-days to *settlement* (C4); caps bind inside the scan (C3). Capacity from captured ladders (breakeven/profit-max/limited-by); **returns `UNKNOWN` while `n_with_ladder = 0`** (F6) and weighted-ARB sizing stays behind `arb_size_by_ladder: false` until real multi-level ladders exist and show edge-limited arbs.

## 7. config.yaml (top-level keys — every one used, none hardcoded)

`bankroll`, `max_stake_pct`, `trusted_pairs: []`, `devig_method`, `min_books_for_consensus: 3`, `min_books_for_consensus_totals`, `sharp_books: [lowvig, betonlineag, betus]`, `sharp_weight`, `book_correlation`, `consensus_outlier_points`, `shrink_prior_sd`, `shrink_prior_pseudocount`, `ev: {enabled: false, enabled_segments: [baseball_mlb], min_edge_se_ratio, max_credible_edge, staleness_half_life_secs, push_pricing: true}`, `kalshi: {fee_rate: 0.07}`, `polymarket: {sports_fee_rate: 0.03, min_liquidity_usd}`, `sx: {fee_rate, win_fee_rate}`, `oddsapi: {region, abort_below_credits: 50, warn_below_credits, settle_days_from: 3}`, `execution: {taker_price_buffer: 0.005, slippage_model: flat, slippage_ref_stake_usd: 50, line_velocity_points_per_sec: {default, baseball_mlb}, max_leg_skew_secs, max_actionable_skew_secs: 10, min_adj_edge_points}`, `matching: {fuzzy_match_threshold, max_start_drift_mins: 30, match_suspect_prob_diff: 0.15, doubleheader_cluster_tol_secs: 7200}`, `tracker: {absent_polls_to_close: 2, terminal_mute_grace_mins: 180}`, `verify: {max_bet_age_secs}`, `capital: {charge_annual, sport_duration_hours, venue_settlement_lag_hours, per_venue_caps}`, `quality: {min_executable_usd, edge_suspect_above}`, `scoring: {weights, n_cap, score_min_n, block_below, tiers, walk_forward_holdout, calibration_gate, ece_max, ece_min_n: 200}`, `feeds: {polymarket_ws: true, kalshi: {allow_readonly_key: false}, sx: {allow_readonly_key: false}}`, `clv: {min_close_depth_usd, resamples: 10000}`, `arb_size_by_ladder: false`, `size_safety_frac: 0.5`, `fillable_fraction`, `schedule: {watch_interval_secs, settle_times}`.

## 8. UX/UI — the four surfaces (precise spec)

All read the same append-only ledger; all are **read-only on the data plane**; none can place a trade. Every displayed dollar is labeled with its cell in the **honest-variant matrix**: *fill* (optimistic|confirmed) × *population* (raw|clean) × *sizing* (flat-$1|quarter-Kelly). **Default headline everywhere: optimistic / clean / flat-$1** (R2 — confirmed-only is survivorship-biased; show confirmed only as a paired delta with the label "paired Δ, survivorship-safe").

**8.1 `status.sh`** — bash + `sqlite3`, one shot: process health from pidfiles/log mtimes (flag duplicate or missing workers — duplicates double-count bets); per-class counts (ARB split OPEN/VERIFIED/VANISHED/UNVERIFIABLE + hot lifetimes); per-venue last-poll health (K3 surfacing); CLV verdict line; DB size + credits remaining. Optional baseline-id arg windows a test run.

**8.2 `arb report` / `arb report --daily [date]`** — `rich` tables recomputed idempotently from `flat_bets`. Daily writes `reports/daily/YYYY-MM-DD.{csv,md}`: CSV one row per bet (id, fingerprint, position_key, venue_pair, kind, trusted, edge_points, adj_edge_points, costs, all four P&L columns, result, provisional, quality_flag, validation, verdicts); MD sections: Headline → Class × Fill × Trust → **By Venue Pair (mandatory in every arb view — R3/F3: kalshi+poly ~88% vanish vs poly+sx ~5% are different populations, never pooled)** → EV edge-points buckets (bucket edges from config) → Validation forward-test → Kelly (flagged) → Extremes → Flagged vs Realized. P&L attribution day = `placed_at` in America/New_York.

**8.3 Live cockpit — `python -m arb ui` → `http://127.0.0.1:8787`** (stdlib `http.server`, bind localhost only, zero new deps, no secrets in any payload):
- `GET /` serves `ui/static/cockpit.html` — **one self-contained file**, Chart.js from cdnjs only, auto-refresh via `fetch('/api/state')` every 10 s.
- `GET /api/state` JSON:
```
{ generated_at, process_health: [{name, alive, last_beat, duplicate}],
  venue_health: [{venue, ok, quotes, error, at}],                    // K3
  credits_remaining,
  headline: {basis: "optimistic/clean/flat", pnl, n, staked, provisional_arbs},
  classes: [{kind, venue_pair, n_positions, n_rows, staked, pnl_optimistic,
             pnl_confirmed_paired_delta, win_rate, roi, pending, sparkline: [..]}],
  equity:       {optimistic_raw: [...], optimistic_clean: [...],
                 confirmed_raw: [...], confirmed_clean: [...]},      // arrays of [date, cum_pnl]
  kelly_equity: { same four },  drawdowns: {...},
  optimistic_confirmed_gap: [...],
  ev: {edge_bucket_table, clv: {basis: "no-vig true mid, pregame-bounded",
       n, mean, ci95, verdict}},                                     // K12/K13/K18: never the fee-loaded metric
  capacity: {status: "UNKNOWN"|..., n_with_ladder},
  recent_bets: [last 50, with venue_pair + quality_flag],
  logs: {watch: tail, settle: tail}, toggles: {strategies, versions} }
```
- **Rendering hierarchy (top→bottom):** hero total (headline basis, labeled) → per-class cards, **ARB card split by venue pair** (R3), each card: $ + ROI% + win rate + n(positions, not rows) + sparkline, never blended across kinds → version A/B panels → equity-over-time chart with per-type/per-version/per-matrix-cell toggles → recent bets + log tails.
- **Guardrails baked into the UI (R7/K13):** "Kelly + confirmed" never renders as a standalone headline — always badged `⚠ survivorship × weighting` (V1: −$78 honest vs +$489 in that cell); any A/B panel whose B-column is all-NULL is hidden, not shown empty; CLV widget calls the no-vig gate only; `provisional` arb dollars are visually hatched until reconciled; every panel footer names its matrix cell.
- Toggle POSTs write to `config.local.yaml` only (strategy on/off) — the single allowed write, never to the ledger.

**8.4 Static export — retired (R7).** No `dashboard.html`/`dashboard.json` snapshot files; V1's went 10 days stale and got read as current. A shareable snapshot is `GET /api/state` saved by the user, timestamped in the payload.

## 9. Testing (vectors are law)

Fixtures-first per rule 9. Pytest suite must include, at minimum:
- **Math vectors to the cent/6dp**: proportional + power + Shin de-vig on a 3-book fixture; weighted median; consensus SE (with ρ); `edge_points`; Kalshi quadratic fee; linear fee; SX inversion + win fee from a recorded fixture; true-arb S/ROI/stakes/guaranteed_floor; ladder walk + depth-scaled buffer; push table lookup; staleness decay; kelly `(p−a)/(1−a)`; capital charge. Reproduce V1's SPEC §8 vectors where unchanged.
- **Parity tests (new, non-negotiable):** per venue, `verify` re-quote pricing == adapter entry pricing at the same raw price (kills K1/K2 forever); a `live` fetcher exercised against a fixture HTTP layer, not a mock lambda.
- **Predicate tests:** OK+REVIEW fixture → only OK reaches clean P&L; `placed_at ≥ start` → INVALID_IN_PLAY at detect *and* settle; Polymarket-leg and SX-leg bets reconcile; 5-rows/1-position aggregate reports n=1.
- **K-regressions:** sub-cent arb P&L survives storage (q6, K10); dead-venue poll closes nothing it shouldn't (K3); CLV close query never returns `fetched_at > start_utc` (K18); a TOTAL flag is CLV-gradeable (K12); `best_bid` non-NULL for all three venues' fixtures (K19); doubleheader fixture yields two clusters (D2); Kalshi `start-unknown` never actionable (D1).

## 10. Build order and acceptance

| Phase | Deliverable | Acceptance |
|---|---|---|
| 0 | Fit scripts on the V1 archive: de-vig choice (Brier), empirical push table, isotonic calibration params → frozen into config + committed | fit script reruns reproducibly; chosen method documented |
| 1 | Package, config loader, schema, store, empty `arb report` | `pytest` green; `arb report` prints empty frame |
| 2 | `core/` + `stats/devig,push` — all math vectors | every §9 vector green |
| 3 | Adapters + `legcost` + parity tests (fixtures) | fixtures parse; `watch --once --dry-run` shows per-venue counts + health; credits logged |
| 4 | Matching | 3-venue same-game fixture → one key; doubleheader → two clusters |
| 5 | Detection + verifier + quality | `watch --once` end-to-end; quotes persisted; REVIEW never in clean lane; VERIFIED writes `confirmed_cost` |
| 6 | Tracker, settle, reconcile, daily report | tracker vector to 6dp; 3-poll fixture → correct CSV; `report --daily` idempotent; predicate tests pass |
| 7 | EV path + CLV (measure-only) | flags carry `p_hat`, edge_points, SE, calibration status; CLV grades totals; nothing auto-bets |
| 8 | Exposure, capacity, cockpit UI | capacity honest-UNKNOWN; `/api/state` schema exact; UI guardrails render; `status.sh` runs |

**Final verification (do this yourself, adversarially):** grep every money-booking and money-displaying path for `eligibility.eligible` and `legcost.leg_effective_cost` — any local reimplementation fails the build. Diff a day of V2 output against the same day recomputed from raw rows (idempotency). Run the K-regression suite. Confirm no float ever reaches a money column (`SELECT typeof()` spot checks). Confirm the UI never renders an unlabeled dollar.

## 11. Kickoff prompt (copy-paste)

> *"You are Opus 4.8. Build ArbModel V2 from scratch following `ArbModelV2_Plans/06 - Opus 4.8 Complete Build Guide.md` exactly — phases 0→8 in order, acceptance before advancing. The four singletons (eligibility, legcost, lifecycle, pnl) are defined once and imported everywhere; parity and K-regression tests are mandatory; all stored P&L is q6; the cockpit implements §8 precisely including the honest-variant labels and guardrails. The V1 archive at `data/archive-pre-v2/arb.sqlite` is read-only oracle data for Phase 0 fits. Paper only, public reads only, no Odds API credits in tests. When anything is ambiguous, stop and ask — do not guess."*

Related: [[00 - How It Works and What The Data Says]] · [[02 - Weaknesses and Lessons]] · [[03 - V2 Build Instructions]] · [[04 - Code Audit Findings (New)]] · [[05 - Cowork Audit and Research Prompt]]
