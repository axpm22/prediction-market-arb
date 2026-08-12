---
title: "ArbitrageModel — Complete Rebuild Specification (v3)"
subtitle: "Kalshi / Polymarket / SX Bet paper-trading arbitrage scanner with flat-stake measurement ledger"
date: "July 6, 2026"
geometry: margin=2.2cm
fontsize: 10pt
colorlinks: true
toc: true
toc-depth: 2
numbersections: true
---

\newpage

# Executive summary

This document is a complete, self-contained specification for rebuilding the ArbitrageModel project from scratch. It is written so that a Claude Code instance running Opus 4.8 (or a weaker model) can implement the entire system phase by phase, with every formula, endpoint, schema, threshold, test vector, and acceptance gate stated explicitly. It also folds in every shortcoming discovered during the first two builds (V1, June 2026; V2 hardening, July 2026) as design requirements, so the rebuilt system starts where the old one ended rather than repeating its mistakes.

**What the system is.** A Python CLI that watches the same event priced on more than one of Kalshi, Polymarket, and SX Bet; flags three opportunity classes (true arbitrage, divergence, +EV vs. de-vigged sportsbook consensus); and maintains an honest, append-only paper ledger answering one question: *"How much would I have made by today if I'd bet $1 on every flag?"* It is a **measurement instrument**, not a trading bot. It never places orders. Its most valuable outputs so far have been honest nulls.

**What the first builds established (the priors a rebuild inherits):**

- Riskless cross-venue sports arbitrage is a **mirage at retail paper scale**: 44 settled arbs totaled +$0.13 (~$0.003/bet); ~88% of flagged edges on the fast venue pair vanish on a 10-second re-quote; confirmed-fill P&L is indistinguishable from zero.
- The de-vigged sportsbook consensus is a **worse forecaster than the venues' own mids** (Brier 0.238 vs. 0.221/0.223; ECE 0.30 vs. ~0.07–0.11). The EV lane's sign was inverted; EV bet creation is frozen by a pre-registered decision rule.
- The instrument itself **works**: 617k quotes/day persisted, append-only ledger discipline caught and voided 100% of one day's fake P&L, activity gating kept API costs near zero, and pre-registration made the nulls citable.

**Why rebuild anyway.** The measurement harness is the asset. It cheaply disproves edges, and the classes that remain plausible (single-venue negRisk baskets with corrected fees, same-oracle BTC windows, maker-side counterfactuals, ladder-aware totals) all need exactly this instrument to be tested honestly. A rebuild with the fixes baked in produces clean evidence from day one instead of after three incident recoveries.

**How to use this document.** Read §4 (hard rules) and §16 (build phases) first. Implement phases in order; each has acceptance checks that must pass before the next phase starts. Every number lives in `config.yaml` (§15). Every claim about a venue API was verified live in June–July 2026; if an endpoint 404s, stop and report — do not invent alternatives.

# Strategy

## The three opportunity classes

Everything is priced in a single unit: **effective cost per $1 of payout** — the real executable order-book ask plus venue fees, expressed as an implied probability in `Decimal`.

**TRUE ARB.** Outcome-covering positions across venues whose combined effective cost is under $1 per $1 of guaranteed payout. If Kalshi sells YES at an effective 0.47 and Polymarket sells NO at an effective 0.49, buying both costs 0.96 and pays exactly 1.00 whichever way the event resolves: a locked 4.17% return. The catch — and the project's central finding — is that the two quotes are rarely live simultaneously, depth is thin, and the two venues may settle the "same" event differently (extra innings, postponements, divergent oracles). Cross-venue arbs are therefore **REVIEW-only by default**, forever, until a venue pair earns whitelisting through the settlement-consistency gate (§18).

**DIVERGENCE.** A cross-venue spread on the same outcome that exceeds a threshold without locking profit. Logged with full quote data as future stat-arb fuel; never bet. (Note: the V2 audit queued divergence *bet-row* removal — ~1,600 no-op opportunity rows/day of ledger noise. In the rebuild, record divergences as opportunities/quotes only, never as ledger rows. See §10.1.)

**+EV.** Fee-adjusted venue cost below the de-vigged median of ≥3 sportsbooks ("consensus"). Every positive-EV quote is recorded regardless of size (the display threshold only gates what prints), which builds the data to tune thresholds later. **Critical inherited verdict:** on 68–877 settled archive samples, the consensus was a worse forecaster than venue mids and the venue side won only 47.9% of EV disagreements. The rebuild ships with `ev_enabled: false` and a frozen re-open trigger (§18.3). EV machinery is still built — it is the measurement instrument — but no EV dollar is paper-bet until the trigger fires.

**Secondary classes** (each single-venue or single-oracle, i.e. structurally safer than cross-venue arb):

- **NEGRISK** — intra-Polymarket exhaustive-outcome baskets where the sum of YES asks (after fees) < 1. Single venue, single oracle, no settlement mismatch: the cleanest arb construct in the system. V1 found all baskets at 1.005–1.011 (honest null), but that was measured with a wrong (double-charging) fee curve; the corrected quadratic fee lowers the bar, so the exhaustive re-scan is a priority experiment.
- **BTC window arb** — Kalshi vs. Polymarket short-horizon BTC price windows where both venues reference effectively the same oracle. Requires exact window end-time match (never interpolate) and a settlement path (V1 gap E3, fixed in §12).
- **PARLAY relative value** — Fréchet-bound violations and independence-baseline divergence on multi-leg markets. EV-signals only; a parlay can never be locked risk-free. Dormant until the core is green.
- **Non-sports (politics/econ/weather)** — fully separate category with manual-first matching and an AI-matcher (Haiku) second lane that is always REVIEW, always segregated, and never eligible for a trusted path without a human map entry.

## Why measurement-first is the strategy

The project's economics are asymmetric: a false "edge exists" costs real money if ever acted on; a true "no edge" costs electricity. So the design optimizes for cheap, honest disproof:

1. **Flat $1 per flag** is the headline, because sizing models multiply whatever bias exists in detection. Kelly/weighted numbers are parallel projections only.
2. **Optimistic vs. confirmed fills.** Every bet records the price at first sighting (optimistic) and at the second consecutive sighting (confirmed). The gap *is* the slippage estimate. If confirmed ≈ 0 while optimistic looks great, the strategy does not survive reality. (V1's confirmed column suffered survivorship bias — bets never seen twice were dropped rather than counted as misses — which is why the rebuild also adds markouts, §10.5.)
3. **Pre-registration.** Every hypothesis, gate, and n-threshold is frozen in `PREREGISTRATION.md` *before* its data accumulates. Changing a rule after data arrives is a dated, versioned amendment — never a silent edit. This is what makes a null citable.
4. **Append-only ledger.** Corrections are annotation rows or new rows, never UPDATEs. When bugs manufactured fake P&L (twice), the annotation mechanism voided it without destroying the evidence of what happened.
5. **Populations never pool.** The two arb venue-pairs (kalshi+polymarket ~88% vanish rate; polymarket+sx ~5% vanish rate — the latter an artifact of SX being slow, not of real edge) are pre-registered separately and never share a win rate, a gate, or a headline.

## Where edge might actually live (ranked by structural safety)

1. **NegRisk exhaustive baskets** (single venue, single oracle) with corrected quadratic fees and full pagination.
2. **Same-oracle BTC windows** (cross-venue but no settlement-rule risk if both oracles verifiably agree).
3. **Ladder-aware totals arb** — Kalshi quotes an exhaustive half-point totals ladder (2.5→12.5) that provably contains Polymarket/SX main lines in ~100% of multi-venue MLB totals events; detection must pair every strike, not one main line.
4. **Maker-side counterfactual** — both Kalshi and Polymarket rebate makers; re-price every flag at maker economics and log the flip fraction as an upper bound (measurement only; no fill-probability pretense).
5. **Venue-leads-books indicator** — the inverted EV signal, repurposed: venues beating the consensus is itself information.

Everything else — cross-venue moneyline arb as a P&L engine, whale copying, blind de-vig EV — is measured, was found wanting, and stays measured rather than believed.

# What worked and what didn't

This section is the distilled empirical record. A rebuilding model should treat every "didn't work" as a constraint to design around, not a bug to re-discover.

## What worked

| Component | Evidence |
|---|---|
| Quote persistence at scale | ~617,000 quotes/day on the fresh DB; ~2.95M in the V1 archive; WAL mode; poll-atomic writes; zero data loss attributable to the store |
| Append-only ledger + annotations | Caught and voided 100% of the 2026-07-02 incident's fake P&L (315 interleave duplicates, 48 rebook-loop rows, 20 in-play voids) without destroying evidence |
| Venue adapters, fixtures-first | All three venues parse recorded fixtures; SX maker-side inversion unit-tested; UFC tag migration caught by live verification |
| Activity gating | Dormant sports cost zero Odds API credits; monthly budget stayed under 500 with headroom (492 remaining at audit) |
| Flash verification + hot watchers | Measured arb lifetime directly: ~88% vanish on ~10s re-quote (kalshi+polymarket); lifetimes right-censoring found and fixed (raise cap to 900s, tag `censored`) |
| Pre-registration discipline | The EV freeze, the fee re-freeze, and the Stage-α rules all executed exactly as pre-committed — no post-hoc story-fitting |
| AI matcher negative-verdict caching | 8 verdicts, 0 positive, cached append-only at market level; budget guard held |
| Quality/quarantine layer | Collapsed the largest recorded EV "edge" from ~54,500% (empty-book artifact) to 18.2% clean — labels, never deletes |
| MLB Stats API as settlement oracle | Free, keyless, unlimited history; rescued bets outside the Odds API 3-day scores window |
| Supervisor + heartbeats (Phase D) | Structural fix for the "old process kept betting stale code" failure class; children one-shot, double-run refusal |

## What didn't work

| Finding | Numbers | Lesson baked into this spec |
|---|---|---|
| Cross-venue arb as P&L | 44 settled arbs = +$0.13 total; 88% vanish on re-quote (K+P pair) | Cross-venue is REVIEW-only forever by default; whitelist gate needs 100 reconciled positions at ≥99% settlement consistency (§18.2) |
| De-vigged EV consensus | Archive clean MLB EV: −$24.07 over 627 rows; consensus Brier 0.238 vs venue mids 0.221/0.223; EV-subset win rate 0.479 | Ship with `ev_enabled: false`; frozen re-open trigger; consensus kept as a venue-leads-books indicator (§18.3) |
| Confirmed-fill column as slippage proxy | Archive: −$19.13 optimistic vs +$40.64 confirmed — a survivorship artifact (never-seen-twice bets dropped), not slippage-beating | Confirmed=NULL counts as *missed*; markouts (+30s/+2m/+10m/+30m) added as the real adverse-selection measure (§10.5) |
| Settlement enforcement of REVIEW | 314/314 settled arbs were forbidden REVIEW rows booked into P&L (V1 bug A1) | One eligibility predicate consulted by *every* money-touching module (§12) |
| Reconciliation | 0 rows ever; only a Kalshi resolver existed while 100% of arbs had a Polymarket/SX leg | Per-venue resolvers required in Phase 5; P&L labeled `provisional` until reconciled (§11.2) |
| Capacity/sizing numbers | "Summed profit $72,209" was fantasy: 0 captured ladders, 4.5x row duplication, 99% REVIEW rows included | Capacity reports `INSUFFICIENT_DEPTH_DATA` until ≥50% of feasible arbs carry real multi-level ladders; dedup by position_key; trusted-only sums (§13.4) |
| Timing honesty | Watch cadence ~140s actual vs 60s configured; 85/314 settled arbs were placed *after* the event; Kalshi start-time fallback was `close_time` (days late) | Start-time truth hierarchy, in-play guards at detection AND settle, cadence printed in the daily report, instant-settle tripwire (§11.3, §13.1, §10.4) |
| Poll interleaving | Multiple loop kinds sharing absent-close logic created 315 duplicate bets in one day | Poll-kind ownership: only the poll kind that created a bet can close it (§10.3) |
| The dirty-tree failure class | Twice, a fix sat uncommitted while the old process kept betting | Supervisor + heartbeats + a hard rule: nothing restarts until pytest is green on a clean committed tree (§13.2) |
| Machine-sleep outages | Jul 4–6: three near-zero-data days; heartbeats stayed fresh while DNS failed | `caffeinate -i` / launchd KeepAlive in the documented start command; stale-data alarm (`data_stale_alarm_secs`) wired into status and daily report (§13.3) |
| In-place label mutations | A V1-era backfill script contained the tree's only `UPDATE flat_bets` and could NULL-clobber quarantine flags | Grep-test in CI: no `UPDATE flat_bets` / `DELETE FROM flat_bets` string may exist under `arb/` (§10.2) |
| Fee-model drift | Polymarket fee modeled linear while the venue had gone quadratic (double-charged mids by ~0.75pts — larger than most arb edges); negRisk used the Kalshi curve on Polymarket legs | Single `leg_fee_cost` implementation with per-venue `FEE_BASIS`; fees are dated facts with `source_url` + `confirmed_on` + staleness badge (§6.5) |
| Whale copying | Adverse selection; win rates above ~72% were zombie-order inflation | Class dropped from the rebuild (keep the debunk in the record) |
| Divergence as ledger rows | 177 zero-stake rows + ~1,600 no-op opportunities per day of pure noise | Divergences persist as quotes/opportunities only, never ledger rows |
| AI-matcher ground truth | Recall unmeasurable: zero confirmed-equivalent non-sports pairs ever existed | Ground-truth set is a prerequisite deliverable before any Haiku budget expansion (§18.5) |

## The one-paragraph verdict

The instrument works; the naive edges don't. A rebuild's purpose is not to find the arb the first build missed — it is to run a cleaner instrument over the classes that remain structurally plausible, produce trustworthy nulls or positives at pre-registered sample sizes, and keep costing nothing but electricity while doing so.



# Hard rules (never violate)

These rules bind every phase, every module, every prompt. They are restated here from the original CLAUDE.md, amended with the lessons of §3. A rebuilding model should copy this section into the new repo's `CLAUDE.md` verbatim.

1. **Paper trading only.** Never call authenticated or order-placement endpoints. Only public, unauthenticated GETs (plus The Odds API key as a query param for market data).
2. **Never spend Odds API credits in tests or loops.** The free tier is 500 credits/month. All tests run against recorded fixtures in `tests/fixtures/`. Live calls happen only in `scan`/`settle` commands. Log the `x-requests-remaining` response header on every live call; warn below 100, abort the scan below 50.
3. **Do not invent API endpoints or fields.** Use only the endpoints listed in §6 (verified working June–July 2026). If one returns 404/4xx, stop and report — do not guess alternatives.
4. **No magic numbers.** Every threshold, fee rate, bankroll figure, and schedule time lives in `config.yaml`.
5. **Money math uses `Decimal`, never float**, in pricing, fees, stakes, and the ledger. Floats are permitted only for display and for statistical root-finding (de-vig power/Shin solvers) whose results are immediately re-quantized to 6-decimal `Decimal`.
6. **Cross-venue opportunities are REVIEW-only by default.** Displayed but never auto-"filled" into the clean lane unless the venue-pair + market type is whitelisted in `config.yaml` after the §18.2 gate. Settlement rules differing between venues is the main source of fake arbs.
7. **Persist every quote** observed during a scan to SQLite, even when no opportunity is found. This is the backtest data.
8. **The math test vectors (§7) are non-negotiable.** Implement them exactly and make them pass before writing any venue adapter that uses the math.
9. **`flat_bets` is append-only.** Never UPDATE, DELETE, or retro-edit rows — *including label/flag columns* (the V1 backfill script that mutated only `quality_flag` was still a critical violation, because it could un-quarantine rows). Corrections are annotation rows in an immutable `annotations` table or new bet rows. Daily reports are always recomputed from the raw log. A CI grep-test asserts no `UPDATE flat_bets` / `DELETE FROM flat_bets` string exists under `arb/`.
10. **SX Bet quotes the maker's side.** Taker cost = `1 - percentageOdds/1e20`. Unit-test this from a recorded fixture before the SX adapter touches the pipeline; inverting it silently flips every SX price.
11. **Non-sports matching is manual-first, Haiku-second.** AI-matched events are always `status=REVIEW`, tagged `ai_matched`, displayed and paper-tracked in a segregated lane, never in the headline, and never eligible for the trusted path without a human `nonsports_map.yaml` entry. Verdicts cache append-only with a per-day budget; a settlement mismatch writes a negative verdict and permanently bans the pair.
12. **One eligibility predicate.** A single module (`arb/eligibility.py`) decides `(eligible, reason, lane)` for every bet-shaped object; detection, tracker, settle, reconcile, capacity, exposure, and report all import it. A grep-test proves the logic exists in exactly one place. (New in v3 — the V1 dual-track foot-gun produced 314/314 forbidden settled arbs.)
13. **Nothing restarts on a dirty tree.** Loops run only under the supervisor, from a committed HEAD, after a green `pytest`. A fix that is "in the tree" but uncommitted does not exist.

# Project layout

Clean sectioning, one concern per directory, modules under ~300 lines (split when larger). The `experimental/` quarantine of V1 grew to 30+ modules; v3 promotes the proven ones into first-class packages and drops the debunked ones.

```text
ArbitrageModel/
├── CLAUDE.md                  # hard rules + workflow (copy §4 verbatim)
├── SPEC.md                    # this document, as the in-repo source of truth
├── PREREGISTRATION.md         # frozen hypotheses, gates, amendment log (§18)
├── README.md                  # setup, free API key, reading the daily report (write LAST)
├── requirements.txt           # pinned: requests, rich, rapidfuzz, schedule,
│                              #   python-dotenv, pyyaml, pytest, anthropic (AI matcher only)
├── config.yaml                # every threshold in the system (§15)
├── .env.example               # ODDS_API_KEY=..., ANTHROPIC_API_KEY=...
├── .gitignore                 # .env, data/*.sqlite*, logs/, reports/, .locks/
├── arb/
│   ├── __init__.py  __main__.py  cli.py  config.py
│   ├── models.py              # Event / Quote / Opportunity dataclasses (§7.1)
│   ├── store.py  schema.py  migrations.py     # SQLite WAL layer (§9)
│   ├── eligibility.py         # THE predicate: (eligible, reason, lane) (§12)
│   ├── venues/
│   │   ├── base.py            # adapter interface: fetch_markets() -> list[Quote]
│   │   ├── kalshi.py  polymarket.py  sx.py    # the three venues (§6.1–6.3)
│   │   ├── oddsapi.py         # consensus + scores (§6.4)
│   │   ├── mlbstats.py        # free settlement oracle + start-time truth (§11)
│   │   └── fees.py            # single leg_fee_cost() + FEE_BASIS map (§6.5)
│   ├── matching.py            # event keys, aliases, doubleheader clustering (§8)
│   ├── pricing.py             # de-vig, EV, arb math — the §7 vectors live here
│   ├── arbitrage.py           # detection: true arb, divergence, guards (§8.4)
│   ├── negrisk.py             # intra-Polymarket basket arb
│   ├── tracker.py             # flat-stake append-only ledger (§10) — the heart
│   ├── settlement.py          # settle + tripwire + timing guards (§11)
│   ├── reconcile.py           # per-venue resolution cross-check (§11.2)
│   ├── settlement_matrix.py   # per-sport scenario matrix, trusted gate (§11.4)
│   ├── markout.py             # +30s/+2m/+10m/+30m marks per booked leg (§10.5)
│   ├── invariants.py          # check-invariants command (§10.4)
│   ├── heartbeat.py           # supervisor, heartbeats, stale-data alarm (§13.2)
│   ├── report.py              # terminal tables + daily CSV/MD writer (§14.2)
│   ├── metrics.py             # ONE place computing every displayed number (§14.1)
│   ├── activity.py            # sport-dormancy gate (zero credits when out of season)
│   ├── exposure.py  capacity.py  sizing.py    # projections, never headline (§13.4)
│   ├── nonsports/
│   │   ├── matcher.py         # manual map + fuzzy candidates
│   │   ├── ai_matcher.py      # Haiku verdicts, cache, budget (§8.5)
│   │   └── strike_match.py    # deterministic strike alignment for series
│   └── lanes/                 # optional loops, each supervisor-owned
│       ├── verify.py          # flash re-quote of fresh arbs (10s)
│       ├── hot.py             # 1s-resolution lifetime watcher per fresh arb
│       ├── btc.py             # same-oracle BTC window arb (with settle path)
│       └── political.py       # intra-market YES+NO<1 (single venue, trusted)
├── data/                      # arb.sqlite, aliases.json, kalshi_series.json,
│                              # nonsports_map.yaml, settlement_matrix.yaml
├── reports/daily/             # auto-written YYYY-MM-DD.csv + .md
├── logs/                      # scan.log, unmatched.log, nonsports_candidates.log,
│                              # supervisor.log
├── prompts/                   # numbered work-order queue (the V2 pattern; keep it)
└── tests/
    ├── fixtures/              # one recorded real response per venue per market type
    ├── test_pricing.py  test_arbitrage.py  test_matching.py
    ├── test_tracker.py  test_settlement.py  test_eligibility.py
    ├── test_fees.py  test_negrisk.py  test_invariants.py
    └── test_hygiene.py        # the grep-tests (no UPDATE flat_bets; one predicate)
```

Deliberately **absent** from v3 (debunked or never-launched in V1/V2 — do not rebuild): `paper.py`/`positions`/`ledger` bankroll auto-fill executor (superseded by `flat_bets`), the whale tracker, the manual `forecasts` ledger, and divergence ledger rows. The record of *why* lives in §3.

# Data sources

All verified live June–July 2026. All keyless except The Odds API. Every adapter is fixtures-first: record one real response per venue per market type into `tests/fixtures/` before writing the parser, and test against the fixture forever after.

## Kalshi

- Base `https://api.elections.kalshi.com/trade-api/v2`, no auth for GETs. Docs: `https://docs.kalshi.com`.
- `GET /markets?series_ticker={S}&status=open&limit=200` (cursor pagination). Discover sports series once via `GET /series?category=Sports` and cache to `data/kalshi_series.json` (winner *and* totals series; NBA totals observed as `KXNBATOTAL-...`). Non-sports series come only from `data/nonsports_map.yaml`.
- Verified fields: `ticker, title, yes_sub_title, close_time, status, yes_ask_dollars, yes_bid_dollars, no_ask_dollars, no_bid_dollars, liquidity_dollars, market_type:"binary"`. Decimal-dollar strings parse straight to `Decimal`.
- **Empty-book trap:** ask 1.0000 + bid 0.0000 + liquidity 0 is an empty book, not a quote. Drop it.
- Orderbook: `GET /markets/{ticker}/orderbook` — use for the `min_executable_usd` depth check and for multi-level ladder capture (§13.4). Kalshi's book size units are under-documented: document the conversion you verify against a live book, and unit-test depth from a fixture.
- **Start time:** parse from the ticker's embedded time. **Never fall back to `close_time`** — it is days after the game and corrupted event keys, in-play guards, and settlement bucketing in V1 (bug D1). A quote whose start can't be parsed is `start-unknown` and non-actionable.
- Kalshi quotes totals as an exhaustive half-point strike ladder (2.5→12.5, 11–25 lines per game). Persist every strike; detection pairs each strike against other venues' main lines (§8.3).

## Polymarket

- Gamma discovery: `GET https://gamma-api.polymarket.com/events?tag_slug={mlb|nba|soccer|tennis|ufc}&closed=false&limit=100`. Markets carry `question, outcomes, outcomePrices, clobTokenIds, endDate, liquidity, gameStartTime`.
- **MMA keys on `tag_slug=ufc`, not `mma`** (the `mma` tag went futures-only mid-2026). Canonical sport string stays `mma` everywhere else. UFC titles carry a `"(Weight, Card)"` suffix — strip before name parsing. In-scope: `moneyline` → WINNER, `totals` (round totals) → TOTAL; ignore method-of-victory / go-the-distance markets.
- **Executable prices come from the CLOB only:** `GET https://clob.polymarket.com/book?token_id={id}`. Gamma `outcomePrices` are stale mids — never use them for detection. Fetch the full per-token book (not just top-of-book) so arb legs carry real multi-level ladders (V1 bug B4: every Polymarket leg was permanently single-level, capping the whole capacity model).
- **Depth trap:** never let event-level `liquidity` stand in for one outcome's depth. Unknown depth is `None`, and `None` means non-actionable, not infinite.
- **Tennis start times from Gamma are nominal** (follow-on court scheduling, hours late). Mark polymarket+tennis starts unreliable; an event whose only start source is unreliable is start-unknown → non-actionable.
- Free keyless WebSocket book feed exists; optional Phase-8 upgrade for markouts and ladder capture.

## SX Bet

- Base `https://api.sx.bet`, no key. Docs: `https://api.docs.sx.bet`.
- `GET /sports` (Basketball=1, Baseball=3, Soccer=5, Tennis=6, MMA=7), `GET /leagues/active?sportId={id}`, `GET /markets/active?leagueId={id}&onlyMainLine=true` (paginate `nextKey`). Market `type`: **226 = moneyline, 28 = totals** (in scope); 342 = spread (ignore). Fields: `marketHash, teamOneName/teamTwoName, outcomeOneName/outcomeTwoName, line, gameTime, status, outcomeVoidName`.
- Order book: `GET /orders?marketHashes={h}`. Per order: `percentageOdds` = the **maker's** implied probability x 1e20 (string), `isMakerBettingOutcomeOne`, sizes in USDC x 1e6.
- **Taker math (rule 10):** you take the opposite side. Cost per $1 payout of outcome one = `1 - percentageOdds/1e20` over orders where `isMakerBettingOutcomeOne == false`; best price = max `percentageOdds`. Depth at a level is the *taker* dollar notional: `maker_stake * c / (1 - c)`. Unit-test both from a fixture before the adapter ships.
- SX void outcomes (`NO_CONTEST`, `NO_GAME_OR_EVEN`) differ from Kalshi/Polymarket binary rules → SX legs start REVIEW until whitelisted per sport. SX legs cannot be flash re-quoted from stored rows (outcome side is not re-derivable) — mark them `unverifiable` in the verify lane rather than silently skipping.
- SX quotes are slow/unrepriced; the polymarket+sx pair's low vanish rate is an artifact of that, not durable edge. Price SX honestly (win fee, thin depth) before believing anything on that pair.

## The Odds API (consensus + scores; optional, credit-metered)

- `GET https://api.the-odds-api.com/v4/sports/{key}/odds/?apiKey={K}&regions=us&markets=h2h,totals&oddsFormat=decimal`. Cost: 1 credit per market per region — so h2h+totals = 2 credits/sport/scan. `GET /v4/sports/` is free (discover soccer/tennis/MMA keys there).
- Scores for settlement: `GET /v4/sports/{key}/scores/?daysFrom=3` (3 = the API max lookback; 3 credits/sport). MLB Stats API (`statsapi.mlb.com`, free, keyless, unlimited history) is the primary MLB settlement oracle and backfill rescue; the Odds API covers the rest.
- Tennis/MMA venues quote WINNER only → fetch h2h only for those keys (don't waste a totals credit that can never join a venue quote).
- Sport-key aliasing: the Odds API exposes `soccer_fifa_world_cup` while venues key events as `soccer`; map keys to canonical prefixes in config so consensus rows join venue quotes.
- Log `x-requests-remaining` on every call; warn < 100, abort < 50. `consensus_enabled: false` disables everything.

## Fee models (single implementation, dated facts)

All fees route through one function, `leg_fee_cost(venue, category, price, contracts)`, driven by a `FEE_BASIS` map. Fees are **dated facts**: each venue's config block carries `source_url` and `confirmed_on`; entries older than `fee_stale_days` (30) get a load-time warning and a stale-fee badge in reports (never a silent halt). V1 lost ~0.75pts of pricing accuracy — more than the median arb edge — to silent fee drift.

| Venue | Model (per $1-payout contract) | Rates (confirmed 2026-07-02) |
|---|---|---|
| Kalshi | taker `ceil_to_cent(0.07 * C * P * (1-P))` (money vector: 10 contracts @ P=0.40 → ceil(0.168) = $0.17) | general 0.07; index markets 0.035 (out of scope); schedule effective 2026-06-29 |
| Polymarket | **quadratic per category**: `fee = C * feeRate * p * (1-p)`; makers 0 | sports 0.03, crypto 0.07, finance/politics/tech 0.04, econ/culture/weather/other 0.05, geopolitics 0; effective 2026-03-30 |
| SX | singles 0% taker+maker; **5% on winning parlays only** (model in the parlay lane) | help.sx.bet fee page |

Historical stored quotes always keep their as-observed `fee_cost`; a fee-model change is a versioned amendment affecting newly fetched quotes only. NegRisk uses the same `leg_fee_cost` (V1's inline second fee implementation used the wrong curve and manufactured fake basket arbs — bug C0). Add an execution-side `taker_price_buffer` (default $0.005/contract) on top of fees so displayed edges survive latency/queue risk.

# Canonical model and math

## Data model (`models.py`)

Everything in **cost per $1 payout** (= implied probability), `Decimal` quantized to 6 places.

- `Event(event_key, category, sport, home, away, start_utc, start_grade)` — `event_key = sport:date:sorted(norm(home), norm(away))`; non-sports events key from their `nonsports_map.yaml` id. `start_grade ∈ {verified, nominal, absent}` per the truth hierarchy (§11.3).
- `Quote(event_key, venue, book, market_type{WINNER|TOTAL}, line|None, outcome_label, raw_price, cost, fee_cost, depth_usd, ladder_json|None, live, fetched_at)` — `outcome_label` ∈ normalized team / `DRAW` / `OVER` / `UNDER`; effective cost = `cost + fee_cost`.
- `Opportunity(kind, category, event, legs, edge, stakes, status{OK|REVIEW}, notes, fingerprint, position_key)`:
  - `fingerprint = sha1(event_key | kind | sorted((venue, book, market_type, line, outcome) per leg))` — prices excluded, so a drifting price is the same opportunity.
  - `position_key = (sport, event_key, normalized_outcome, market_type, line)` — the *economic* position, independent of venue. One opinion re-quoted on another venue pair is the same position, not a new bet (V1 bug C2: 661 rows collapsed to 148 fingerprints and even fewer positions; every aggregate was inflated 4.5x).

## Mandatory test vectors (exact, `Decimal`; implement in Phase 2, before any adapter)

**De-vig (proportional, per book):** `p_i = (1/o_i) / sum_j(1/o_j)`.
Odds (1.91, 1.91) → (0.5, 0.5). Odds (1.50, 2.86, 6.00) → normalizer k = 1.183 → approximately (0.5635, 0.2956, 0.1409), summing to 1.

**De-vig methods:** implement `proportional`, `power`, and `shin`; config-selected. The archive bake-off (n=30 settled MLB): power Brier 0.22041 < shin 0.22099 < proportional 0.22217 → **default `power`**. Store per-book de-vigged probabilities so flags can be re-graded under another method without re-running.

**Consensus:** median de-vigged p across ≥ `min_books_for_consensus` (3) books; totals threshold 2 (line fragmentation would otherwise kill totals consensus). Optional outlier drop: exclude a book whose de-vigged prob is > `consensus_outlier_points` from the cross-book median.

**EV:** `c = cost + fee_cost`; `EV = (p_hat - c) / c`.
Vector: p_hat = 0.55, Kalshi ask 0.50, fee = 0.07 x 0.5 x 0.5 = 0.0175 → c = 0.5175 → **EV = 0.062802**.

**Kalshi fee (money):** 10 contracts @ P = 0.40 → ceil_to_cent(0.07 x 10 x 0.40 x 0.60 = 0.168) = **$0.17**.

**Polymarket quadratic fee:** at p = 0.50, sports rate 0.03 → fee per contract = 0.03 x 0.5 x 0.5 = **$0.0075** (the linear model's $0.015 here was the double-charge bug).

**True arb:** best effective cost per outcome `c_i`; `S = sum(c_i) < 1` → `ROI = 1/S - 1`; `stake_i = B * c_i / S`.
Vector: decimal odds 2.10 & 2.05 → S = 0.963995 → **ROI = 0.037349**; B = $100 → stakes $49.40 / $50.60, payout $103.73–103.74.
No-arb vector: 1.95 / 1.95 → S = 1.025641 (must NOT flag).

**NegRisk fee vector:** a basket that is exactly break-even under the correct quadratic fee must NOT flag as an arb (regression against bug C0).

**Divergence:** `spread = mid_A - mid_B` per outcome; DIVERGENCE if |spread| ≥ threshold and no arb survives asks + fees. Persist all spreads (quotes only — no ledger rows).

**Parlay (Phase 7):** legs p = (0.60, 0.55) → Fréchet bounds [max(0, 0.60+0.55-1), min(0.60, 0.55)] = [0.15, 0.55]; independence fair = 0.33. Ask 0.12 → class-1 flag (below lower bound); ask 0.25 → class-2 flag (vs. fair, threshold 0.05).

**Tracker vector:** see §10.7.

Totals use identical math with OVER/UNDER as the two outcomes.


# Matching

## Names and aliases

Normalize (lowercase, strip punctuation and city qualifiers) into `data/aliases.json`, seeded with all MLB/NBA teams plus short forms. **Alias tables are per sport** — "Athletics"/"Rangers" collide globally. Tennis/MMA use surname + event date, and EV bets on surname-matched sports require an explicit per-player alias entry (fuzzy matches may record divergences but never EV bets — this killed V1's fake tennis edges).

## Event identity

Same sport + same unordered team pair + start within ±6h → same `event_key`. Exact alias match → auto. `rapidfuzz.token_set_ratio ≥ 92` → match, but forced `REVIEW`/`fuzzy-match`. Unmatched → `logs/unmatched.log`, never forced. Two sanity gates run before any pairing: venues must agree on start time within `max_start_drift_mins` (30), and the same outcome must not be priced more than `match_suspect_prob_diff` (0.15) apart — a 20-point gap is a matching error, not an arb; suspect groups can log divergences but never ARB flags.

**Doubleheader clustering (fix for V1 bug D2):** the date-only event key means two MLB games between the same teams on one day collide. Within a matched market group, cluster legs by start time (gap > `doubleheader_cluster_tol_secs`, default 7200s, = different game) before pairing outcomes, so game-1 and game-2 quotes never combine into a fake arb. The start-drift gate stays as a backstop.

## Totals: exact line, every strike

Totals match only on the **exact same line** (8.5 ↔ 8.5; never interpolate; 8.5 ≠ 9.0 — a push changes the contract). **Ladder-aware requirement (Stage-α finding):** Kalshi quotes an exhaustive strike ladder that contains Polymarket/SX main lines in ~100% of multi-venue MLB totals events (21/21 live, 270/275 archive). Detection must therefore key outcomes by `(market_type, line, outcome)` and pair **every** Kalshi strike against other venues' lines, not a single main line. Totals are ~50% of live quote inventory; a main-line-only matcher silently discards half the surface. Integer lines can PUSH — flag them, and optionally scale edge by (1 − push probability) from a Poisson run-total model (config `push_pricing`).

## Detection guards (all at detection time, not just settle time)

- **In-play/final guard:** drop any leg whose `start_utc <= now` (route to a live lane if one exists); a resolver saying the event is final records a `FINAL_EVENT_SKIPPED` diagnostic, never an arb. (V1 booked arbs on finished games.)
- **Start-unknown guard:** a leg with `start_grade = absent` is never actionable.
- **Simultaneity:** legs fetched more than `max_actionable_skew_secs` (10) apart were never live together → REVIEW with a skew note. Additionally decay the edge continuously: `adj_edge = edge_points - skew_secs * line_velocity_points_per_sec[sport]`; REVIEW if `adj_edge <= min_adj_edge_points`. The old binary 60s window is only the outer recording bound.
- **Edge-sanity cap:** a computed edge above `ev.max_edge` (0.15) or `edge_suspect_above` (0.25) is a data error (stale/one-sided book, mis-joined consensus) — record the quote, book no bet, count it in "left on the table" with reason `edge-implausible`. The largest V1 raw "edge" was ~54,500%; the largest clean one was 18.2%.
- **Depth floor:** every leg needs `depth_usd >= min_executable_usd` (50) to be clean; unknown depth routes to the micro lane (EV-only, larger edge floor, stake capped by visible depth) or is non-actionable.
- **Rejected-arb audit trail:** every rejection writes a diagnostic row with its reason (V1 silently `continue`d, losing the record of what was rejected and why).

## Non-sports matching

Manual-first: the scanner writes fuzzy candidates (title similarity ≥ 85) to `logs/nonsports_candidates.log`; a human promotes pairs into `data/nonsports_map.yaml`. Human entries always win and are the only path to any trusted lane. Second lane, when `ai_matcher.enabled`: Haiku (`claude-haiku-4-5`) judges pair equivalence from both venues' resolution rules (truncated to `max_rules_chars`), min confidence 0.85, budget `max_new_pairs_per_day`, verdicts cached append-only at market level with a rules-hash (`rules_sha`) so an edited rulebook expires its cached verdict. Multi-market series (weather/macro strikes) pass through a deterministic strike matcher first — exact comparator/threshold/unit alignment — so only strike-aligned pairs reach Haiku. SX pairs are never AI-matched (no rules text). AI-matched flags: always REVIEW, tagged `ai_matched`, paper-tracked flat-$1 in a segregated lane; an inconsistent cross-venue settlement flags `settlement_mismatch`, writes a negative verdict, and permanently bans the pair. **Prerequisite before any budget expansion:** a hand-labeled ground-truth set plus a stage-1 recall harness — V2 discovered recall had never been measurable because no confirmed-equivalent pair ever existed.

# Storage (SQLite, WAL)

Stdlib `sqlite3`, no ORM. WAL mode. All money columns TEXT holding `Decimal` strings (never REAL; only pure time measurements like `leg_skew_secs` may be REAL). Schema versioned via a tiny `migrations.py` that only ever adds.

```sql
quotes(id, poll_id, fetched_at, event_key, category, sport, venue, book,
       market_type, line, outcome, raw_price, cost, fee_cost, depth_usd,
       ladder_json, live)                -- every quote, every poll, forever
polls(id, started_at, kind, n_quotes, n_opps)   -- kind: watch|scan|break|verify|...
opportunities(id, poll_id, fingerprint, position_key, kind, category, event_key,
       edge, status, legs_json, notes)
flat_bets(id, created_at, poll_id, poll_kind, fingerprint, position_key, kind,
       category, sport, venue_pair, event_key, event_start_utc, start_grade,
       legs_json, stake_usd, edge_points, trusted, lane, quality_flag,
       consensus_age_secs, leg_skew_secs, status, result, settled_at,
       pnl_optimistic, pnl_confirmed, pnl_markout, reappearance_of, notes)
       -- APPEND-ONLY (rule 9)
annotations(id, created_at, bet_id, kind, detail)      -- append-only corrections
consensus(event_key, outcome, p_hat, method, n_books, dispersion, computed_at)
markouts(id, bet_id, leg_index, horizon_secs, mark_cost, marked_at)
heartbeats(process, beat_at, head_sha, pid)
verdicts(id, created_at, pair_key, verdict, confidence, rules_sha, model, cost_usd)
left_on_table(id, poll_id, fingerprint, reason, edge_points)   -- filtered flags
```

Storage rules: every table an INSERT-only or INSERT+terminal-row pattern; `arb report` recomputes any day idempotently from raw rows, never from stored aggregates; pre-cutover databases are analysis-only oracles and are **never blended into post-cutover P&L**.

# The flat-stake tracker — THE deliverable

Answers, per day: *"if I'd put $1 on every flag, where would I be?"* All P&L net of fees and spread crossing.

## Bet creation

First poll where a fingerprint appears → one row per class:

- `ARB`: $1 total split across legs by the §7 stake formula; `locked_pnl` known at entry.
- `EV`: $1 on the single leg; resolves at event settlement; recorded for every EV > 0 with its edge bucket. (Creation frozen while `ev_enabled: false`; quotes and would-be flags still recorded to `left_on_table`.)
- `NEGRISK`: $1 across the basket.
- Divergences: **no ledger row** (v3 change) — persisted as opportunities/quotes only.

Classes are independent: one event can hold an ARB and an EV bet simultaneously, each in its own lane. Before creating a row, check `position_key`: an active bet on the same economic position absorbs the sighting as an improved quote (note on the existing row's annotations), not a new bet.

## Lifetime, dedup, and poll ownership

A fingerprint is active while observed on consecutive polls **of the poll kind that created it**. Absent for ≥1 completed poll of that kind → lifetime closed. Reappearance later → a new row with `reappearance_of` linking the old one. **Poll-kind ownership is mandatory:** V2's worst single bug (315 duplicate bets in a day) came from six different loop kinds sharing absent-close logic — a bet created by `watch` must be closable only by `watch` polls. An observability guard logs any fingerprint with > `max_reappearances` (10) rows in 24h.

## Dual fill columns

- `pnl_optimistic`: filled at first-observation effective prices.
- `pnl_confirmed`: filled at the second consecutive observation's prices; never observed twice → confirmed = NULL **and counted as "missed"** in every aggregate (the V1 survivorship artifact came from silently dropping these).

## Terminal discipline, tripwire, invariants

- SETTLED is terminal — but only once the event has genuinely ended (start in the past by `terminal_mute_grace_mins`). A SETTLED row for a still-future event is premature; a re-sighting re-opens via the normal reappearance path.
- **Instant-settle tripwire:** a bet with `settled_at - placed_at < settle_tripwire_secs` (one poll interval) had a knowable outcome at placement — never a forecast. The row still writes (append-only) but is auto-annotated `settle-tripwire`, excluded from every P&L cell, and its fingerprint is blacklisted from re-booking for the event's lifetime. Any future instant-settle row without an annotation is a sev-1.
- **`arb check-invariants`**, run at the end of every settle cycle and in CI: (1) no bet settled before placement; (2) no bet placed after its event's final; (3) every SETTLED row's event is in the past; (4) no fingerprint exceeds the reappearance bound un-annotated; (5) no P&L cell includes an annotated-excluded row. Silent when clean; CRITICAL log + annotation when not.
- **Hygiene grep-tests in CI:** no `UPDATE flat_bets`/`DELETE FROM flat_bets` anywhere under `arb/`; `eligibility` logic in exactly one module.

## Markouts (the real slippage/adverse-selection measure)

For every booked leg, persist the executable cost at +30s, +2m, +10m, +30m from the quotes the loops already sample, into `markouts`. Derive `pnl_markout` (entry at the +30s mark). **Decision rule (frozen):** if the +2m markout eats more than half a class's average flagged edge, that class's headline switches to `pnl_markout` and any go-live math prices the measured markout as a per-bet cost. Markout ≈ 0 would be the single most valuable positive fact this project could produce. A flicker/zombie guard excludes marks from suspended or empty books.

## Trust split and headline definition

Every bet carries `trusted` (venue-pair whitelisted) and `lane ∈ {clean, review, invalid}` from the eligibility predicate. **Headline daily number = trusted + sports + pregame + clean, only.** Non-sports, `live=true`, AI-matched, micro, and review rows are each tracked in their own always-visible sections, never blended.

## Tracker test vector (Phase 6 acceptance)

Simulated fixture: poll1 flags ARB (S=0.96) + EV (edge 3%, ask 0.50); poll2 ARB persists @0.97, EV gone; poll3 EV reappears @0.49. Expected: ARB one bet, optimistic pnl = 1/0.96 − 1 ≈ $0.041667, confirmed = 1/0.97 − 1 ≈ $0.030928; EV two rows (second has `reappearance_of` set), first has confirmed = NULL (a "missed"); event settles YES → EV row 1 optimistic pnl = (1 − 0.5175)/0.5175, all asserted to the cent with Kalshi fees applied.

# Settlement and reconciliation

## Settling

ARB settles at entry (locked P&L) — *after* passing the eligibility predicate and timing guards. EV/NEGRISK settle at event resolution: MLB via the MLB Stats API (primary; free, unlimited history), other sports via Odds API scores (`daysFrom=3`), non-sports manually via `arb settle --manual EVENT RESULT`. P&L attributes to the EST day the bet was placed, so past daily rows get restated as pending bets settle — restated days are marked `*` in reports.

**Settle-eligibility (fix for V1 bug A1 — the 314/314 forbidden-arbs bug):** the settle query selects open bets through the same eligibility predicate as everything else. REVIEW bets still settle — into the review lane, `result='RESOLVED_REVIEW'`, never touching clean/headline P&L. A regression test settles a fixture with one OK and one REVIEW arb and asserts only the OK row lands actionable.

**Timing guards (fix for V1 bug A3 — 85/314 arbs placed after the event):** at settle, compare `placed_at` to the event start/final: placed after → append `INVALID_AFTER_FINAL`, excluded from clean. Defense in depth with the detection-time guard (§8.4). A future-start guard refuses to settle an event that hasn't started.

## Reconciliation (per-venue resolvers)

Settlement by scores is provisional until each leg's **venue** confirms resolution. Implement one resolver per venue, composed by `RESOLVERS[venue]`, each returning `WON|LOST|VOID|None`:

- Kalshi: market result via the public market endpoint.
- Polymarket: conditionId / UMA resolution via public Gamma/CLOB reads.
- SX: `/markets` settled status + the leg's outcome side.

Reconcile runs automatically after every settle cycle. Only mark a bet unresolved if *its* venue's resolver returns None (V1 skipped a whole bet when any leg was unresolved, and only a Kalshi resolver existed — reconciliation was a structural no-op for 100% of a book whose every bet had a non-Kalshi leg). Until reconciled, realized P&L is labeled `provisional` in every surface. Corrections append rows; a reconciled mismatch on a trusted pair reverts the pair to REVIEW immediately.

## Start-time truth hierarchy

`start_grade = verified` (MLB Stats schedule, which outranks all venue times) > `nominal` (venue-listed) > `absent`. A bet created from a `nominal` start where a verified source exists is a bug; `absent` is never actionable. This plus the no-`close_time`-fallback rule kills the whole family of V1 timing corruptions.

## Settlement matrix (the gate to any trusted path)

`data/settlement_matrix.yaml`: per sport x scenario (tennis retirement; MLB extra innings, suspension, doubleheaders; soccer extra time/penalties/abandonment; MMA no-contest and card changes — the MMA parity check found a real Kalshi-vs-Polymarket divergence on cancellation handling), each cell GREEN/AMBER/RED with rulebook-section citations. The trusted gate requires every cell for a sport GREEN before the venue pair can be whitelisted for it; a reconcile retro-check flips cells from lived settlements. No vibes-based whitelisting.

# Eligibility: one predicate, one lane definition

`arb/eligibility.py` exposes a single function:

```python
def classify(bet_or_opp, config) -> tuple[bool, str, str]:
    """Returns (eligible, reason, lane) with lane in {'clean','review','invalid'}."""
```

It encodes, in one place: trusted venue-pair membership; settlement-matrix GREEN requirement; pregame vs. in-play; start-grade; depth floor; skew/adjusted-edge; quality flags; category segregation (sports vs. non-sports vs. AI-matched vs. micro vs. live). Detection, tracker, settle, reconcile, capacity, exposure, and report all import it — none reimplements any part of it. Frozen tests: a REVIEW arb can never reach clean/headline P&L (fixture test), and a grep-test proves the logic exists in exactly one module. This single change retires V1's three parallel notions of "clean" (trusted flag / quality_flag / status), which were the root cause of both incidents' guard gaps.

The BTC lane's V1 gap belongs here too: **no opportunity class may book bets without a registered settlement path.** A `kind` without a settler is a config error at startup, not a permanent-OPEN surprise later.


# Operational discipline (practicality)

## Cadences and loops

Two cadences. Primary `watch`: every `watch_interval_secs` (60s) across all three venues (keyless, unlimited). Secondary `scan` (4x/day at `scan_times`, only if `consensus_enabled`): refreshes sportsbook consensus. `watch` never calls The Odds API; EV math uses stored consensus, annotated `stale-consensus` beyond `consensus_max_age_hours` (8h; pregame flags quarantined beyond 3h — a pregame line vs. an 8-hour-old consensus is "venue vs. stale line"). If `scan_breaks: true`, re-scan during scheduled pauses (NBA halftime ~48min after start, soccer ~45min; configurable); break-window quotes get `live=true` and never blend into headline numbers.

**Cadence honesty:** the daily report prints planned vs. actual poll interval (mean and p95). V1's loop believed it ran at 60s while actually running at ~140s; the p95 cycle time in the report is the loop's real spec, and every timing claim keys off it.

## Supervisor and heartbeats

All loops run as subprocesses of one `arb supervisor`:

- Children are **one-shot** (`watch --once`); the supervisor relaunches each on exit. A second supervisor copy refuses to double-run (lock dir `.locks/`).
- Every child writes a heartbeat row `(process, beat_at, head_sha, pid)` at cycle end. The `head_sha` column is the structural fix for the dirty-tree failure class: any process beating from a sha ≠ committed HEAD is visible instantly.
- Heartbeat staleness alone must NOT trigger a restart (a healthy multi-minute live cycle beats only at the end — V2 learned this by killing healthy children in a loop for three days). Force-restart only a child alive past `max_child_runtime_secs` (600s).
- `data/stop-all` (a flag file) halts every loop cleanly; only the documented restart procedure removes it.

## Host survival (the Jul 4–6 outage lesson)

The machine sleeping is a proven zero-data failure mode that heartbeats do not catch (the supervisor kept beating while DNS failed). Requirements:

- The documented start command wraps the supervisor in `caffeinate -i` (macOS) or the platform equivalent; better, install a launchd/systemd unit with `KeepAlive`.
- **Stale-data alarm:** `arb status` and the daily report both alarm when the newest persisted quote is older than `data_stale_alarm_secs` (900). An invisible outage is the failure; the alarm makes it loud.
- `status.sh` / `arb status` must model the supervisor's process semantics: fresh beat OR live child pid → ALIVE; stale beat + live supervisor lock → STALE; DEAD only with no supervisor. (Keying status on child pids reads a healthy one-shot loop as DEAD.)

## Sizing, capacity, exposure (projections only)

Flat-$1 is the only headline. Everything else is a parallel projection and must say so:

- **Capacity** reports `INSUFFICIENT_DEPTH_DATA` until ≥ `min_ladder_fraction` (0.5) of feasible arbs carry real multi-level ladders. Top-of-book-only capacity numbers are fiction (V1's "$72,209 summed profit" stacked zero ladders, 4.5x row duplication, and 99% REVIEW rows). Aggregate by `position_key`, filter to trusted, display the captured-ladder fraction, and haircut displayed depth by `fillable_fraction` for sizing.
- **Exposure** runs inside the scan: refuse to promote a new actionable flag when total or per-venue committed stake would exceed caps; charge capital to the later venue's settlement (start + sport duration + venue payout lag), not to event start; tag locks below `capital_charge_annual` (5%/yr pro-rated) as `uneconomic`.
- **Kelly** (quarter-Kelly, hard-capped at 5% of bankroll per bet) and depth-aware ARB scaling are projection columns gated behind their own pre-registered enablement rules (§18.4).

# UX

## CLI

`python -m arb <command>` with `rich` tables. Commands:

| Command | Purpose |
|---|---|
| `watch [--once] [--dry-run]` | primary loop: fetch, match, detect, track, persist |
| `scan` | Odds API consensus refresh (no-op if disabled) |
| `settle [--manual EVENT RESULT]` | resolve pending bets; auto-runs reconcile + check-invariants |
| `report [--daily DATE]` | recompute any day idempotently from `flat_bets` |
| `status` | process states, data freshness alarm, credits, P&L snapshot |
| `check-invariants` | the five ledger invariants; silent when clean |
| `regrade [--db PATH]` | re-score consensus vs. venue mids on any archive (read-only) |
| `supervisor` | own all loops; the only sanctioned way to run them |
| `negrisk-scan`, `verify`, `hot`, `btc`, `political`, `nonsports` | optional lanes, supervisor-owned |

Design rules learned the hard way: every command is safe to run twice (idempotent or refuses); every command prints which DB and HEAD sha it ran against; destructive-looking words never appear (there is nothing destructive to do); one `metrics.py` computes every number so the terminal, the daily file, and any dashboard can never disagree (V1 had three surfaces computing P&L independently — they diverged).

## The daily report (the product)

Auto-written at 23:59 America/New_York as `reports/daily/YYYY-MM-DD.csv` (one row per bet) + `.md` (summary) + terminal print. Contents:

- Per class x fill-assumption (optimistic / confirmed / markout) x trust lane: bets, $ staked, settled P&L, pending, win rate, ROI.
- Breakdowns by venue-pair (never pooled), sport, category (sports / non-sports / AI-matched separate), edge bucket (0–1%, 1–2%, 2–5%, >5% — the threshold-tuning table).
- Biggest win / biggest loss / largest unrealized pending.
- **Left on the table:** count + summed edge of flags killed by each filter, by reason.
- Flagged edge vs. realized edge per class (the honesty metric).
- Cadence line: planned vs. actual interval, p95 cycle time.
- Premise monitor: rolling consensus-vs-venue-mid Brier (the EV re-open trigger's input).
- Data-freshness alarm state; stale-fee badges; restated-day markers (`*`); cumulative since inception under both fill assumptions.

## Status and control

`arb status` (and thin `status.sh` wrapper): supervisor/child states via heartbeats + lock, newest-quote age with alarm, Odds API credits remaining, today's flags by lane, open bet counts, last settle/reconcile result. Optional read-only local dashboard (single HTML page reading `metrics.py` output). Keep the control surface minimal: flag files (`data/stop-all`, `data/stop-<lane>`) for halts; config edits + supervisor restart for everything else. Web server, if kept, swallows client-disconnect tracebacks (log one line — traceback greps are the outage forensics tool and must stay clean).

# Config reference (`config.yaml` — complete, annotated)

Every value the system reads. Defaults are the V2-hardened values; comments are binding documentation.

```yaml
timezone: America/New_York          # all daily boundaries EST/EDT
bankroll_usd: 1000
max_stake_pct: 0.10
ev_threshold: 0.02                  # display gate only; tracker records all EV>0
divergence_threshold: 0.03
arb_stale_warn: 0.04                # ROI above this = probably stale/mismatch; flag don't celebrate
max_leg_skew_secs: 60               # outer recording bound (actionable bound is execution.*)
max_start_drift_mins: 30            # venues disagreeing on start = match suspect
match_suspect_prob_diff: 0.15       # same outcome priced >15pts apart = matching error
capital_charge_annual: 0.05         # locks below T-bill-ish carry are 'uneconomic'
sport_typical_duration_hours: { baseball_mlb: 3.5, basketball_nba: 2.5,
                                soccer: 2.0, tennis: 2.5, mma: 1.0 }
venue_settlement_lag_hours: { kalshi: 24, polymarket: 48, sx: 24 }  # verify empirically
min_books_for_consensus: 3
min_books_for_consensus_totals: 2   # totals are line-fragmented
devig_method: power                 # empirical winner (Brier bake-off, archive n=30)
sharp_books: [lowvig, betonlineag, betus]
weight_sharp_books: false           # measurement-parallel until it beats plain median on CLV
min_liquidity_usd: 500
min_executable_usd: 50
micro:                              # thin-book EV lane, segregated
  enabled: true
  min_depth_usd: 5
  max_stake_usd: 25
  e_min: 0.08                       # edge floor at full depth...
  e_min_thin: 0.15                  # ...rising linearly as depth thins
  assume_thin_venues: [sx]
  max_event_exposure_usd: 50
  max_venue_exposure_usd: 100
max_days_to_settlement: 14
nonsports_max_days: 7
sports: [baseball_mlb, basketball_nba, soccer, tennis, mma]   # no NFL
unreliable_start_venues: { polymarket: [tennis] }
activity_lookahead_hours: 48        # sport ACTIVE iff a venue lists a fixture in window
consensus_sports: [baseball_mlb]    # add tournament keys only while they run
odds_api_sport_aliases: { soccer_fifa_world_cup: soccer,
                          tennis_atp_wimbledon: tennis,
                          tennis_wta_wimbledon: tennis,
                          mma_mixed_martial_arts: mma }
odds_api_markets: { default: "h2h,totals",
                    tennis_atp_wimbledon: h2h, tennis_wta_wimbledon: h2h,
                    mma_mixed_martial_arts: h2h }
scan_breaks: true
break_windows: { basketball_nba: [48, 15], soccer: [45, 15] }
watch_interval_secs: 60
consensus_enabled: true
ev_enabled: false                   # FROZEN by Phase A verdict; see PREREGISTRATION
consensus_max_age_hours: 8
consensus_pregame_max_age_hours: 3
ev:
  e_min: 0.05
  kelly_fraction: 0.25
  edge_suspect_above: 0.25          # above = quarantine as data error
  max_edge: 0.15                    # above = record quote, book nothing
  require_exact_match: [tennis, mma, golf]
  enforce_depth_floor: true
  reject_one_sided_books: true
scan_times: ["06:15", "10:00", "15:00", "19:00"]
daily_report_time: "23:59"
settle_time: "09:30"
settle_days_from: 3                 # Odds API scores max lookback
settle_tripwire_secs: 60            # instant-settle = knowable at placement
tracker:
  terminal_mute_grace_mins: 180
  max_reappearances: 10             # observability bound, logs only
mlbstats_enabled: true
auto_fill: false                    # permanently false; flat_bets is the executor
trusted_pairs: []                   # empty until the SS18.2 gate clears — weeks by construction
fuzzy_match_threshold: 92
fee_stale_days: 30                  # fees are dated facts; stale = badge, never halt
kalshi:      { fee_rate: 0.07, source_url: "https://kalshi.com/docs/kalshi-fee-schedule.pdf",
               confirmed_on: 2026-07-02 }
polymarket:  { sports_fee_rate: 0.03, crypto_fee_rate: 0.07, politics_fee_rate: 0.04,
               econ_weather_fee_rate: 0.05, geopolitics_fee_rate: 0.0, fee_rate: 0.05,
               source_url: "https://docs.polymarket.com/trading/fees",
               confirmed_on: 2026-07-02 }   # QUADRATIC per category; makers 0
sx:          { fee_rate: 0.0, win_fee_rate: 0.0,   # singles free; 5% winning PARLAYS only
               source_url: "https://help.sx.bet/en/articles/2798017-sx-bet-fees",
               confirmed_on: 2026-07-02 }
execution:
  taker_price_buffer: 0.005         # half-cent per contract vs latency/queue risk
  slippage_model: flat              # 'ladder' is a versioned change to every cost vector
  fillable_fraction: 1.0            # flip toward 0.5 once live fills calibrate it
  arb_size_by_ladder: false         # SS18.4 gate
  arb_size_safety_frac: 0.5
  min_ladder_fraction: 0.5          # capacity says UNKNOWN below this
  max_actionable_skew_secs: 10
  min_adj_edge_points: 0.002
  line_velocity_points_per_sec: { default: 0.001, baseball_mlb: 0.0008,
                                  basketball_nba: 0.0015 }
  doubleheader_cluster_tol_secs: 7200
odds_api: { region: us, warn_below_credits: 100, abort_below_credits: 50 }
negrisk:
  enabled: true
  min_edge: 0.005
  require_exhaustive_exclusive: true
  max_outcomes: 30
  scan_limit: 200
  scan_pages: 3                     # paginate; V1's page cap hid most of the surface
  min_executable_usd: 5             # single-venue: thin baskets worth measuring
btc_arb:
  enabled: false                    # enable only WITH its settlement path + oracle-agreement study
  window_match_secs: 5
  max_window_minutes: 15
  trusted: false
parlay: { enabled: false, divergence_threshold: 0.05, min_liquidity_usd: 500,
          sx_win_fee_rate: 0.05 }
ai_matcher:
  enabled: false                    # enable after ground-truth set exists (SS18.5)
  model: claude-haiku-4-5
  min_confidence: 0.85
  max_new_pairs_per_day: 100
  max_rules_chars: 8000
  paper_fill: true
  paper_stake_usd: 1
nonsports:
  watch_interval_secs: 300
  candidate_threshold: 85
  kalshi_categories: ["Politics", "Economics", "Climate and Weather"]
  polymarket_tags: [politics, economy, weather]
verify: { poll_secs: 10, max_bet_age_secs: 900 }
hot:    { poll_secs: 2, check_interval_secs: 1.0, watch_duration_secs: 900,
          dead_checks: 3, max_watchers: 3 }   # 900s: 120s right-censored every lifetime
scoring:
  weights: { roi: 0.40, trend: 0.25, sample: 0.20, win_rate: 0.15 }
  n_cap: 100
  score_min_n: 30
  block_below: 30
  recent_window: 10
  calibration_gate: true            # ECE gate blocks a segment even if ROI passes
  ece_max: 0.10
sizing: { kelly_fraction: 0.25, max_kelly_fraction_of_bankroll: 0.05 }
validation:
  ev_filter: { rule_id: H-EVFILTER, kind: EV, min_cost: 0.40, max_cost: 0.75,
               max_flagged_edge: 0.02, target_n: 190, halfway_n: 95,
               kill_consecutive_losses: 10 }
supervisor:
  processes: { watch: true }        # add lanes deliberately, one at a time
  commands:  { watch: "watch --once" }
  max_child_runtime_secs: 600
data_stale_alarm_secs: 900
```

# Build phases (for a Claude Code executor)

Implement strictly in order. Each phase ends with `pytest` green plus the named acceptance command, whose output must be shown. Do not start a phase until the previous one's acceptance passes. Commit per phase (or per finding within a phase) with evidence pasted into the commit message. Modules stay under ~300 lines. Python 3.11+; dependencies pinned in `requirements.txt`: `requests, rich, rapidfuzz, schedule, python-dotenv, pyyaml, pytest, anthropic` (AI matcher only — never for market data); stdlib `sqlite3`. `ODDS_API_KEY` from `.env` (git-ignored; ship `.env.example`).

| Phase | Build | Acceptance |
|---|---|---|
| 1. Skeleton | `config.py` (typed loader, no getattr-with-default fallbacks duplicating loader defaults), `models.py`, `schema.py`/`store.py`/`migrations.py`, CLI stub, hygiene grep-tests | `arb report` prints an empty frame; DB file created with full schema; `pytest` green |
| 2. Math | `pricing.py` + `venues/fees.py`: every §7 vector exact in `Decimal`; de-vig x3 methods; fee models incl. Polymarket quadratic + negRisk regression vector | all vectors pass to the cent/6th decimal |
| 3. Venue adapters | fixtures first (record one real response per venue per market type), then parsers; SX inversion + depth tests; Kalshi empty-book + no-close_time-fallback tests; Polymarket CLOB-only + full-book ladder; `left_on_table` plumbing | adapters parse fixtures; one live `watch --once --dry-run` prints per-venue quote counts |
| 4. Matching | aliases, event keys, doubleheader clustering, ladder-aware totals pairing, detection guards, non-sports manual map | fixture: same MLB game on 3 venues → one event_key; totals strike-ladder pairing test; doubleheader fixture never cross-pairs |
| 5. Detection + eligibility | `arbitrage.py`, `negrisk.py`, `eligibility.py` (with T-ELIG fixture tests + grep-test), display | live `watch --once` end-to-end; quotes persisted; a REVIEW arb provably cannot reach the clean lane |
| 6. Tracker + settle + report | `tracker.py` (§10 vector), `settlement.py` + tripwire + timing guards, `reconcile.py` with all three resolvers, `invariants.py`, `metrics.py`, daily writer | §10.6 vector passes; simulated 3-poll fixture produces the exact CSV; `check-invariants` silent; run `watch` 30 min live then `report --daily` shows real flags |
| 7. Ops | `heartbeat.py` supervisor, stale-data alarm, `status`, caffeinate/launchd docs, markouts persist hook | kill-test: stop flag halts everything; sleep-test documented; markouts table accumulates rows within one watch session |
| 8. Lanes (each gated) | verify, hot, political, non-sports manual; then per pre-registered "real if": negRisk exhaustive re-scan, BTC (with settle path), AI matcher (after ground truth), maker shadow, parlays | each lane's own acceptance in its prompt; supervisor owns every loop |
| 9. README | setup, free Odds API key, reading the daily report | written last, from the working system |

**Working method per fix/feature (binding):** reproduce first (quote the file:line or query output) → write the failing test → minimal implementation → full `pytest -q` → `arb report --daily` unchanged except where the change explicitly says otherwise → commit with evidence.

# Shortcomings ledger — every known defect and its fix

Consolidated register. "Baked in" = the fix is a requirement elsewhere in this spec (section cited).

| # | Shortcoming (evidence) | Fix (where baked in) |
|---|---|---|
| A1 | Settle ignored REVIEW; 314/314 settled arbs were forbidden rows | One eligibility predicate + review settle lane (§11.1, §12) |
| A2 | Reconcile never ran; Kalshi-only resolver = structural no-op | Per-venue resolvers, auto-run, `provisional` labels (§11.2) |
| A3 | 85/314 arbs placed after the event still settled | Timing guards at detection AND settle (§8.4, §11.1) |
| B1 | Zero ladders → capacity couldn't model slippage; always "depth-limited" | `INSUFFICIENT_DEPTH_DATA` until ladder fraction ≥ 0.5 (§13.4) |
| B2 | "$72k profit" = no slippage x 4.5x dup x REVIEW included | position_key dedup + trusted filter + ladder-fraction display (§7.1, §13.4) |
| B3 | depth_usd on three different bases, trusted 100% fillable | normalized taker-notional definition + per-venue unit tests + `fillable_fraction` (§6, §13.4) |
| B4 | Polymarket legs permanently single-level | full CLOB `/book` ladders (§6.2) |
| C0 | NegRisk used Kalshi's fee curve on Polymarket legs → fake baskets | single `leg_fee_cost` + break-even regression vector (§6.5, §7.2) |
| C1 | 60s leg skew counted as "simultaneous" | 10s actionable bound + velocity-decay haircut (§8.4) |
| C2 | No position key; one opinion = many bets | `position_key` on every opportunity/bet + dedup-by-position (§7.1, §10.1) |
| C3 | No portfolio constraint bound detection | exposure runs inside the scan (§13.4) |
| C4 | Capital charged to event start, not settlement | later-venue settlement horizon (§13.4) |
| C5 | No started/final guard at detection | in-play/final/start-unknown guards (§8.4) |
| D1 | Kalshi start fell back to close_time (days late) | never fall back; start-unknown = non-actionable (§6.1) |
| D2 | Doubleheaders collapsed to one event_key → fake arbs | start-time clustering inside market groups (§8.2) |
| D3 | SX priced at zero friction | honest SX pricing; parlay-only 5% modeled in its lane (§6.3, §6.5) |
| D4 | Depth-unknown arbs counted "feasible" | feasible requires real depth basis; unknown routes to micro or non-actionable (§8.4) |
| E1 | Cent-rounding could break the lock, row still recorded | negative-floor arbs flagged and excluded from locked-P&L cells (§7.2 stake math + eligibility) |
| E2 | Rejections were silent | diagnostic rows for every rejection (§8.4) |
| E3 | BTC bets had no settler → permanent OPENs | no class without a registered settlement path (§12) |
| I1 | Poll interleaving created 315 duplicate bets/day | poll-kind ownership of bet lifecycles (§10.2) |
| I2 | Settled-rebook loop re-booked terminal fingerprints | terminal-with-grace + tripwire + blacklist (§10.4) |
| I3 | Fixes sat uncommitted while old process bet (twice) | supervisor + head_sha heartbeats + rule 13 (§13.2) |
| I4 | Host sleep = 3 zero-data days, invisible | caffeinate/launchd + stale-data alarm (§13.3) |
| I5 | `UPDATE flat_bets` survived in a legacy backfill; could NULL-clobber quarantine flags | delete-on-sight + CI grep-test; rule 9 has no labels exemption (§4.9, §10.4) |
| I6 | status.sh keyed on dead process names / child pids | status models supervisor semantics (§13.3) |
| I7 | Three surfaces computed P&L independently and diverged | one `metrics.py` (§14.1) |
| I8 | Confirmed-fill survivorship (+$40 "confirmed" on a −$19 book) | NULL-confirmed counts as missed; markouts are the real measure (§10.3, §10.5) |
| I9 | Config defaults duplicated in three places, drifting silently | typed loader is the only default site (§16 Phase 1) |
| I10 | Web log tracebacks on client disconnect polluted forensics | swallow disconnects, one-line log (§14.3) |
| I11 | Divergence rows: ~1,600 no-ops + 177 zero-stake ledger rows/day | divergences never create ledger rows (§10.1) |
| I12 | AI-matcher recall unmeasurable (zero ground truth ever) | ground-truth set is a prerequisite deliverable (§8.5, §18.5) |
| I13 | Fee facts undated; drifted from published schedules | `source_url` + `confirmed_on` + staleness badge (§6.5) |
| I14 | Hot-watcher 120s cap right-censored every arb lifetime | 900s cap + `censored` tag (§15 `hot`) |
| I15 | Tournament/consensus keys mismatched venue prefixes | `odds_api_sport_aliases` (§6.4) |

# Pre-registration (freeze before any data)

Copy this section into `PREREGISTRATION.md` before the first live poll. Changing any rule after data collection starts is a dated amendment, never an edit. Nulls are publishable results.

## Populations

The two arb venue-pairs (kalshi+polymarket, polymarket+sx) are different populations, pre-registered separately, never sharing a win rate, gate, or headline.

## Arb whitelisting gate (the only path to headline cross-venue P&L)

A venue-pair + market type enters `trusted_pairs` only after: ≥ 99% consistent settlement over ≥ 100 reconciled, position-deduped, clean-lane positions for that pair, **and** a human has read both venues' rulebooks for that market type, **and** the settlement matrix section is fully GREEN. By construction this is weeks away from any fresh start; nothing shortcuts it. A reconciled mismatch reverts the pair immediately.

## EV lane (frozen OFF, with a re-open trigger)

Inherited verdict: consensus is a worse forecaster than venue mids (Brier 0.238 vs 0.221/0.223; consensus ECE 0.30) and the EV-subset venue side won 420/877 = 0.479. Therefore `ev_enabled: false`. **Re-open trigger:** a re-run of `arb regrade` where consensus beats venue mids on Brier AND EV-subset win rate > 0.5 with the CI excluding 0.5. If re-opened: go-live for a segment additionally requires mean CLV > 0 (one-sided 95% CI excluding 0, CLV measured against the no-vig closing mid, fees excluded from the metric) over ≥ 190 settled MLB EV bets, AND segment ECE ≤ 0.10 with positive Brier skill. Kill rule: 10 consecutive losing forward-test bets halts the test. ROI alone never promotes anything.

## Capacity / weighted-ARB enablement

`arb_size_by_ladder` flips true only after (1) ≥ 50% of feasible arbs carry real multi-level ladders and (2) the capacity report shows genuine edge-limited (not depth-fantasy) arbs. Until both: capacity says UNKNOWN, no weighted headline exists.

## Class-specific "real if" clauses

- **NEGRISK:** real if corrected-fee basket edge > `negrisk.min_edge` appears at all in the exhaustive scan; claim requires 30 settled baskets. (Prior: all archived baskets 1.005–1.011; corrected fees shift ≤ ~0.01 — fees were not the binding constraint.)
- **BTC windows:** real only if the two oracles print the same binary result over ≥ N pre-set windows before `trusted` is even discussed; whitelisting still needs the §18.2 gate.
- **AI matcher:** no Haiku budget expansion until a hand-labeled ground-truth set exists and stage-1 recall ≥ 80%; 50–80% = tune first; segregated paper lane must show settlement consistency at the arb-settle bar before any human-map promotion shortcut.
- **Maker shadow lane:** output is only "share of flags that flip +EV as maker" — an upper bound, never promoted on the flip fraction alone.
- **Ladder-aware totals:** first deliverable is verifying strike-level pairing fires at all (0 ARB may be the fees/edge null, not a matching gap).
- **Minimum samples:** 100 reconciled positions per arb pair; 190 settled EV bets; 200 settled quotes per calibration segment; 30 settled negRisk baskets; segment scoring blocked below n=30. Halfway checkpoints monitor, never promote.

# Known traps (print this list above the desk)

1. ROI > ~4% means a stale quote, suspended market, or settlement mismatch — flag, don't celebrate (`arb_stale_warn`).
2. Gamma `outcomePrices` are not executable; CLOB only.
3. Kalshi ask 1.0000 / bid 0.0000 / liquidity 0 = empty book.
4. SX `percentageOdds` is the maker's side — forgetting `1 - p` inverts every price.
5. Totals: 8.5 ≠ 9.0; exact line only; but pair *every* Kalshi strike.
6. Break-window (`live=true`) quotes go stale in seconds; never blend into headline numbers.
7. The optimistic−confirmed gap is only a persistence proxy; markouts are the adverse-selection measure; if confirmed ≈ 0 while optimistic looks great, the strategy doesn't survive reality.
8. Alias per sport; non-sports never auto-matched into the trusted path.
9. A fix that isn't committed doesn't exist; a loop not under the supervisor isn't running.
10. Every "huge edge" is an input bug until proven otherwise — the largest V1 raw edge was 54,500%, and it was an empty book.

# Practicality: costs, effort, expectations

**Money.** $0 venue-side (all keyless public reads). The Odds API free tier (500 credits/mo) suffices with activity gating: MLB-only steady state ≈ 8–24 credits/day including settles; tournament windows front-load more. Haiku matcher: cents/day under the verdict cache and budget cap. Hardware: any always-on machine; the DB grows ~0.5–1 GB/month at full quote persistence (keep everything; storage is the cheapest input to honesty).

**Build effort.** Phases 1–6 are the core instrument (~6–9k lines with tests, based on V1/V2 actuals of ~9k lines in `arb/` plus ~100 test files); an Opus-class executor lands them in ordered sessions, one phase per session, committing per phase. Phase 7 ops and Phase 8 lanes are incremental.

**What to expect from the data.** Weeks one–two: plumbing checks, zero clean settled evidence (that's correct — the measurement clock starts at the first supervised restart on a green tree). The first real deliverables are the daily report's honesty metrics (flagged vs. realized edge, cadence, left-on-table) and the negRisk/ladder-totals verdicts at their pre-registered n. If everything nulls out, the system has done its job at the cost of electricity — and the maker-side, markout-near-zero, or venue-leads-books findings would each still be independently valuable.

**The one rule that outranks the rest.** This instrument's value is that its numbers can be believed. Any time saved by skipping a gate, blending a lane, or patching a row in place is repaid with the entire evidentiary record. Slow is honest, and honest was the point.
