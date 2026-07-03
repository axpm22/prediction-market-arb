# SPEC v2 — Kalshi / Polymarket / SX Bet arbitrage scanner with flat-stake tracking (paper only)

## 1. Goal

A Python CLI that watches the **same event** priced on more than one of **Kalshi, Polymarket, and SX Bet**, flags opportunities, and — the core deliverable — maintains an honest, append-only record answering: **"how much would I have made by today if I'd bet $1 on every flag?"**

Opportunity classes:

- **TRUE ARB**: outcome-covering positions across venues whose combined cost (all fees + spread crossing included) < $1 per $1 guaranteed payout.
- **DIVERGENCE**: cross-venue spread on the same outcome ≥ `divergence_threshold` that doesn't lock profit. Logged with full quote data (future stat-arb fuel), **not** bet by the tracker yet.
- **+EV** (only when `consensus_enabled`): fee-adjusted cost below the de-vigged sportsbook consensus. *Every* positive-EV quote is recorded in the tracker regardless of size — the display threshold (`ev_threshold`) only gates what's printed. This builds the data to tune the threshold later.

**Markets in scope (v1):** game/match **winner** (moneyline) and **totals (over/under)**. Totals match across venues only on the **exact same line** (8.5 ↔ 8.5; never interpolate). Spreads and player props: later. Parlays: Phase 7 only.

**Sports:** MLB, NBA, soccer, tennis, MMA. No NFL.

**Non-sports events** (politics, econ, crypto): included, but as a fully **separate category** — own report section, own ledger rows (`category = nonsports`), and only events closing within `nonsports_max_days` (default 7). Matching for non-sports is manual-first: the scanner writes fuzzy-matched candidates to `logs/nonsports_candidates.log`; a human promotes them into `data/nonsports_map.yaml`. **Amendment (prompts/nonsports_ai_matcher.md + STRIKE_AWARE_NONSPORTS_MATCHER_PROMPT.md):** with `ai_matcher.enabled`, Haiku compares both venues' resolution rules and may match a pair automatically. Single-market series use the one-shot path; multi-market weather/macro series go through a deterministic strike matcher (exact comparator/threshold/unit or range alignment) so only strike-aligned pairs reach Haiku. Such events are tagged `ai_matched`, always REVIEW, **displayed AND paper-tracked in a segregated lane** (`ai_matcher.paper_fill`, flat $1) that is never in the headline and never trusted — the tracker measures Haiku's error rate. An AI pair whose venues settle inconsistently is flagged `settlement_mismatch`, banned via a negative verdict, and never re-bet. Human map entries always win and remain the only path to the real-money/trusted lane. SX pairs are never AI-matched (no rules text).

**Timing:** pregame always. Additionally, if `scan_breaks: true`, re-scan events during scheduled pauses (NBA/soccer halftime windows estimated from start time; configurable offsets). Quotes captured in-break get `live=true` and are reported separately — never mixed into headline tracker numbers.

**Two cadences.** Primary loop `watch`: every `watch_interval_secs` (60 s) across Kalshi ↔ Polymarket ↔ SX Bet (all keyless, unlimited). Secondary `scan` (3×/day, only if `consensus_enabled`): refreshes sportsbook consensus from The Odds API. `watch` never calls The Odds API; EV computed from stored consensus, annotated `stale-consensus` beyond `consensus_max_age_hours`.

**Selection per cycle:** (1) pull open winner+totals markets for configured sports (+ mapped non-sports) from all three venues; (2) intersect via matching (§5); (3) filter: liquidity ≥ `min_liquidity_usd` on every leg, settlement ≤ `max_days_to_settlement`, depth at best ask ≥ `min_executable_usd`; (4) classify; (5) rank display by **edge per day to settlement**. Filtered-out opportunities are still counted (§9, "left on table").

## 2. Project layout

```
ArbitrageProject/
├── CLAUDE.md  ├── SPEC.md  ├── README.md  ├── requirements.txt
├── config.yaml  ├── .env.example
├── arb/
│   ├── __init__.py  ├── __main__.py  ├── cli.py  ├── config.py
│   ├── models.py    ├── store.py
│   ├── venues/ (base.py, kalshi.py, polymarket.py, sx.py, oddsapi.py)
│   ├── matching.py  ├── pricing.py  ├── arbitrage.py
│   ├── tracker.py   # flat-stake ledger (§9) — the heart of the project
│   ├── paper.py     # bankroll-based paper positions (secondary to tracker)
│   └── report.py    # terminal tables + daily file writer
├── data/ (arb.sqlite, aliases.json, kalshi_series.json, nonsports_map.yaml)
├── reports/daily/   # auto-written YYYY-MM-DD.csv + .md
├── logs/ (scan.log, unmatched.log, nonsports_candidates.log)
└── tests/ (fixtures/, test_pricing.py, test_arbitrage.py, test_matching.py, test_tracker.py)
```

## 3. Data sources (all verified live, June 2026)

### 3.1 Kalshi (primary)
- Base `https://api.elections.kalshi.com/trade-api/v2`, no auth for GETs. Docs: https://docs.kalshi.com
- `GET /markets?series_ticker={S}&status=open&limit=200` (cursor pagination). Discover sports series once via `GET /series?category=Sports` → cache `data/kalshi_series.json` (winner *and* totals series, e.g. NBA totals observed as `KXNBATOTAL-…-212`). Non-sports series only via `data/nonsports_map.yaml`.
- Fields (verified): `ticker,title,yes_sub_title,close_time,status,yes_ask_dollars,yes_bid_dollars,no_ask_dollars,no_bid_dollars,liquidity_dollars,market_type:"binary"`. Decimal-dollar strings → `Decimal`. Ask 1.0000 + bid 0.0000 + liquidity 0 = empty book, not a quote.
- Orderbook: `GET /markets/{ticker}/orderbook` (use for `min_executable_usd` depth check).
- Fee: `ceil_to_cent(fee_rate × contracts × P × (1−P))`, `kalshi.fee_rate` default 0.07 — confirm against the published fee schedule in Phase 3.

### 3.2 Polymarket (primary)
- Gamma discovery: `GET https://gamma-api.polymarket.com/events?tag_slug={mlb|nba|soccer|tennis|ufc}&closed=false&limit=100`. Markets carry `question, outcomes, outcomePrices, clobTokenIds, endDate, liquidity, gameStartTime`.
  - The `mma` sport keys on Gamma `tag_slug=ufc` (NOT `mma`): the `mma` tag went futures-only (season/champion markets, no `gameStartTime`) ~mid-2026, while the actual fight cards live under `ufc` — verified against live Gamma 2026-07-02. The canonical sport string stays `mma` everywhere else (event_key prefix, `quotes.sport`, config, Odds API alias). UFC titles carry a `"(Weight, Card)"` suffix that is stripped before name parsing; in-scope market types are `moneyline`→WINNER and `totals` (round totals)→TOTAL, while `ufc_method_of_victory` / `ufc_go_the_distance` are ignored (out of v1 scope).
- Executable prices from CLOB only: `GET https://clob.polymarket.com/book?token_id={id}` (Gamma `outcomePrices` are stale mids — never use for detection).
- `polymarket.fee_rate` default 0.0 (config key kept).
- **Amendment 2026-07-02 (prompt 05):** Polymarket's taker fee is **quadratic per category**,
  `fee = C·feeRate·p·(1−p)` (docs.polymarket.com/trading/fees, effective 2026-03-30) — feeRate
  sports 0.03 / crypto 0.07 / finance·politics·tech·mentions 0.04 / econ·culture·weather·other 0.05
  / geopolitics 0; makers 0. This **supersedes** any earlier "linear Polymarket fee" rule (the
  linear model was correct for the pre-2026-03-30 schedule; it double-charged mid-price legs).
  Priced on the single `FEE_BASIS["polymarket"] = "quadratic"` (`arb/venues/slippage.py`), used by
  every adapter, the flash verifier, and negrisk. Per-category rates in `config.yaml`; stored
  historical quotes keep their as-observed `fee_cost` (versioned change).

### 3.3 SX Bet (primary)
- Base `https://api.sx.bet`, no key. Docs: https://api.docs.sx.bet
- `GET /sports` (Basketball=1, Baseball=3, Soccer=5, Tennis=6, MMA=7), `GET /leagues/active?sportId={id}`, `GET /markets/active?leagueId={id}&onlyMainLine=true` (paginate `nextKey`). Market `type`: **226 = moneyline, 28 = totals** (both in scope); 342 = spread (ignore v1). Fields: `marketHash, teamOneName/teamTwoName, outcomeOneName/outcomeTwoName, line, gameTime, status, outcomeVoidName`.
- Order book: `GET /orders?marketHashes={h}`. Per order: `percentageOdds` = **maker's** implied probability ×10²⁰ (string), `isMakerBettingOutcomeOne`, sizes in USDC ×10⁶.
  **Taker math:** you take the opposite side. Cost per $1 payout of outcome one = `1 − pct/1e20` over orders with `isMakerBettingOutcomeOne == false`, best = max pct. Unit-test from fixture before use — inverting this silently flips every SX price.
- `sx.fee_rate: null` until read from docs in Phase 3; adapter refuses to run while null.
- SX void outcomes (`NO_CONTEST`/`NO_GAME_OR_EVEN`) differ from Kalshi/Polymarket binaries → SX legs start `REVIEW` until whitelisted per sport.

### 3.4 The Odds API (confirmation only, optional)
- `GET https://api.the-odds-api.com/v4/sports/{key}/odds/?apiKey={K}&regions=us&markets=h2h,totals&oddsFormat=decimal` (2 markets × 1 region = 2 credits/sport/scan; ≤5 sports × 3 scans/day ≈ 450/mo < 500 — if tight, drop totals from consensus). `GET /v4/sports/` is free; discover soccer/tennis/MMA keys there. Log `x-requests-remaining`; warn <100, abort <50. Disable everything via `consensus_enabled: false`.

## 4. Canonical model (`models.py`)

Everything in **cost per $1 payout** (= implied probability), `Decimal` only.
- `Event(event_key, category, sport, home, away, start_utc)`; `event_key = sport:date:sorted(norm(home),norm(away))`; non-sports: `event_key` from `nonsports_map.yaml` id.
- `Quote(event_key, venue, book, market_type{WINNER|TOTAL}, line|None, outcome_label, raw_price, cost, fee_cost, depth_usd, live, fetched_at)`. `outcome_label` ∈ normalized team / `DRAW` / `OVER` / `UNDER`. Effective cost = `cost + fee_cost`.
- `Opportunity(kind, category, event, legs, edge, stakes, status{OK|REVIEW}, notes, fingerprint)`. `fingerprint = sha1(event_key | kind | sorted(venue,book,market_type,line,outcome) per leg)` — **prices excluded**, so a drifting price is the same opportunity.

## 5. Matching (`matching.py`)

1. Normalize names (lowercase, strip punctuation/city qualifiers) → `data/aliases.json` (seed all MLB/NBA teams + short forms; tennis/MMA use surname + event date). Alias per sport — "Athletics"/"Rangers" collide globally.
2. Same sport + same unordered pair + start within ±6 h → same `event_key`. Totals additionally require **exact line equality**.
3. Exact alias match → auto. `rapidfuzz.token_set_ratio ≥ 92` → match but `REVIEW`/`fuzzy-match`. Unmatched → `logs/unmatched.log`, never forced.
4. Non-sports: via `data/nonsports_map.yaml` (manual, always wins) or — amendment, when `ai_matcher.enabled` — a cached Haiku verdict on both venues' resolution rules (§1). Candidates by title similarity ≥ 85 in both paths.
5. Cross-venue legs ⇒ `REVIEW` unless venue-pair+sport in `trusted_pairs`.

## 6. Storage (SQLite, WAL)

- `quotes(id, poll_id, fetched_at, event_key, category, sport, venue, book, market_type, line, outcome, raw_price, cost, fee_cost, depth_usd, live)` — **every quote, every poll, forever** (user decision: keep all).
- `polls(id, started_at, kind{watch|scan|break}, n_quotes, n_opps)`
- `opportunities(id, poll_id, fingerprint, kind, category, event_key, edge, status, legs_json, notes)`
- `flat_bets` (§9) — append-only.
- `consensus(event_key, outcome, p_hat, computed_at)`
- `positions/ledger` — bankroll paper trading as before (secondary).

## 7. `config.yaml` defaults

```yaml
timezone: America/New_York   # all "daily" boundaries in EST/EDT
bankroll_usd: 1000
max_stake_pct: 0.10
ev_threshold: 0.02           # display gate only; tracker records all EV > 0
divergence_threshold: 0.03
arb_stale_warn: 0.04
min_books_for_consensus: 3
min_liquidity_usd: 500
min_executable_usd: 50
max_days_to_settlement: 14
nonsports_max_days: 7
sports: [baseball_mlb, basketball_nba, soccer, tennis, mma]
scan_breaks: true
break_windows: { basketball_nba: [48, 15], soccer: [45, 15] }  # [minutes after start, window length]
watch_interval_secs: 60
consensus_enabled: true
consensus_max_age_hours: 8
scan_times: ["10:00", "15:00", "19:00"]
auto_fill: false
trusted_pairs: []            # add "kalshi+polymarket:baseball_mlb" after reading both rulebooks
kalshi: { fee_rate: 0.07 }
polymarket: { fee_rate: 0.0 }
sx: { fee_rate: null }
odds_api: { region: us, abort_below_credits: 50 }
parlay: { enabled: false, divergence_threshold: 0.05 }
```

## 8. Math — mandatory test vectors (exact, `Decimal`)

- **De-vig** (per book): `p̂ᵢ = (1/oᵢ)/Σ(1/oⱼ)`. Odds (1.91,1.91) → (0.5,0.5). Odds (1.50,2.86,6.00) → k=1.183 → ≈(0.5635,0.2956,0.1409), Σ=1.
- **Consensus**: median de-vigged p̂ across ≥`min_books_for_consensus` books.
- **EV**: `c = cost + fee_cost`; `EV = (p̂−c)/c`. Vector: p̂=0.55, Kalshi ask 0.50, fee 0.07×0.5×0.5=0.0175 → c=0.5175 → EV=**0.062802**.
- **Kalshi fee (money)**: 10 contracts @ P=0.40 → ceil(0.168)=**$0.17**.
- **True arb**: best effective cost per outcome cᵢ, S=Σcᵢ<1 → ROI=1/S−1; stakeᵢ=B·cᵢ/S. Vector: odds 2.10 & 2.05 → S=0.963995 → ROI=**0.037349**; B=$100 → $49.40/$50.60, payout $103.73–103.74. No-arb vector: 1.95/1.95 → S=1.025641.
- **Divergence**: `spread = mid_A − mid_B` per outcome; DIVERGENCE if |spread| ≥ threshold and no arb survives asks+fees. Persist all spreads.
- Totals use identical math; OVER/UNDER are the two outcomes.

## 9. Flat-stake tracker (`tracker.py`) — THE deliverable

Answers, per day: *"if I'd put $1 on every flag, where would I be?"* Append-only `flat_bets` table; **never** retro-edit or delete rows. All P&L **net of fees and spread crossing** ("price of trading" — user requirement).

**Bet creation.** First poll where a fingerprint appears → one row per class ledger:
- `ARB`: $1 *total* split across legs by §8 stakes; `locked_pnl` known at entry.
- `EV`: $1 on the single leg; resolves at event settlement. Recorded for **every** EV > 0 with its edge bucket.
- `DIVERGENCE`: recorded with full quotes, `stake = 0` for now (stat-arb phase activates it).
Classes are fully independent: one event can hold an ARB and an EV bet simultaneously, each in its own ledger.

**Lifetime / dedup (user rule).** A fingerprint is *active* while observed on consecutive polls. Absent for ≥1 completed poll → lifetime closed. If it reappears later → **new bet** (new row, `reappearance_of` linking to the old one).

**Dual fill columns (track both, user choice on Q3):**
- `pnl_optimistic`: filled at first-observation effective prices.
- `pnl_confirmed`: filled at the **second consecutive** observation's prices; if never observed twice, confirmed-fill = NULL (counted as "missed"). This proxies being slower than the market; the gap between columns measures how much of the edge is mirage.

**Trust split (safest interpretation of Q17).** Every bet carries `trusted` (venue-pair whitelisted) or `review`. Headline daily number = **trusted + sports + pregame only**. Full breakdown always shown alongside. Non-sports and `live=true` bets are tracked in separate sections, never in the headline.

**Settlement.** ARB: settled at entry (locked). EV: outcome via scores (Odds API daily for sports; non-sports resolved manually via `arb settle --manual`). **P&L attributes to the EST day the bet was placed** (user choice), so past daily rows get restated as pending bets settle — the report marks restated days with `*`.

**Daily report (auto).** Daemon fires at 23:59 `America/New_York`: writes `reports/daily/YYYY-MM-DD.csv` (one row per bet) and `YYYY-MM-DD.md` (summary), plus prints to terminal. Contents — all of (user: "all of them"):
- Per class × fill-assumption × trust: bets placed, $ staked, settled P&L, pending count, win rate, ROI.
- Breakdown by venue-pair, by sport, by category (sports / non-sports separate), by edge bucket (EV: 0–1%, 1–2%, 2–5%, >5% — this is the threshold-tuning table).
- Biggest win / biggest loss / largest unrealized pending.
- **Left on the table**: count + summed edge of flags killed by liquidity/depth/horizon filters.
- Flagged edge vs realized edge per class (the honesty metric).
- Cumulative since inception, both fill assumptions.
`arb report --daily [date]` recomputes any day from `flat_bets` (idempotent — derived from the append-only log, never from stored aggregates).

**Tracker test vector (`test_tracker.py`).** Simulated fixture: poll1 flags ARB (S=0.96) + EV (edge 3%, ask 0.50); poll2 ARB persists @0.97, EV gone; poll3 EV reappears @0.49. Expect: ARB one bet, optimistic pnl = 1/0.96−1 ≈ $0.041667, confirmed = 1/0.97−1 ≈ $0.030928; EV two rows (reappearance), first has confirmed=NULL; event settles YES → EV row1 optimistic pnl = (1−0.5175)/0.5175 … all asserted to the cent with Kalshi fees applied.

## 10. CLI & phases

Commands: `watch` (primary loop incl. tracker), `scan` (consensus; no-op if disabled), `report [--daily DATE]`, `positions`, `settle [--manual EVENT RESULT]`, `daemon` (watch + scheduled scans + 23:59 EST daily writer + break windows).

Phases (each: `pytest` green + acceptance):
1. Skeleton (config/models/store) → `arb report` prints empty frame, DB created.
2. Math (§8 vectors exact).
3. Venue adapters, fixtures-first (record one real response per venue per market type) → adapters parse fixtures; one live `watch --once --dry-run` shows quote counts.
4. Matching (fixture: same MLB game on 3 venues → one event_key; totals line-match test).
5. Detection + display → live `watch --once` end-to-end, quotes persisted.
6. **Tracker + daily report** (§9 vector passes; simulated 3-poll fixture produces correct CSV) → run `watch` 30 min live, then `report --daily` shows real flags.
7. Parlays (§11) — only after 1–6.

README last: setup, free Odds API key, reading the daily report.

## 11. Phase 7 — Parlay / multi-leg relative value

Kalshi "combos" (MVE markets in normal `/markets` feed; `mve_selected_legs` references leg tickers — verified), Polymarket CAOC parlays (CFTC-listed May 2026), SX Daily Parlays (sportId 25). A parlay pays $1 iff all legs win; **no static replication** → EV signals only, always `REVIEW`, never auto-filled, tracked in their own ledger when enabled.
1. **Fréchet violations (model-free):** legs p₁…pₙ from same venue's individual markets: `max(0, Σpᵢ−(n−1)) ≤ P(all) ≤ min(pᵢ)`. Ask below lower bound = underpriced under any correlation; bid above upper = overpriced. Report at any size.
2. **Independence baseline:** cross-game `fair ≈ Πpᵢ`; flag vs `parlay.divergence_threshold`. Same-game legs: positive correlation ⇒ fair > product; annotate direction only.
Vector: legs (0.60,0.55) → bounds [0.15,0.55], fair 0.33; ask 0.12 → class 1; ask 0.25 → class 2.
Liquidity: observed zero-liquidity Kalshi MVE books; apply `min_liquidity_usd` strictly.

## 12. Known traps

- ROI > ~4% ⇒ stale quote, suspended market, or settlement mismatch (extra innings/OT, postponement voids, different oracles). Flag, don't celebrate.
- Gamma `outcomePrices` aren't executable; CLOB only.
- Kalshi ask 1.0000/bid 0.0000/liquidity 0 = empty book.
- SX `percentageOdds` is the maker's side — forgetting `1−p` inverts every price.
- Totals: 8.5 ≠ 9.0; exact line match only.
- Break-window (`live=true`) quotes go stale in seconds; never blend into headline tracker numbers.
- The optimistic−confirmed P&L gap *is* the slippage estimate; if confirmed ≈ 0 while optimistic looks great, the strategy doesn't survive reality.
- Alias by sport; non-sports never auto-matched.
