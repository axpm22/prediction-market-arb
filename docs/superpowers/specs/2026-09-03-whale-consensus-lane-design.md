# L14 Whale-Consensus Lane — Design

Date: 2026-09-03. Status: DRAFT for operator review. Paper only. Polymarket only.

## 1. The claim under test

Some Polymarket wallets are skilled. When several of them, independently and
with a demonstrated record in the relevant kind of market, take the same side
of a sports market before it starts, a follower who buys at the **post-print
touch** (best ask after the whale's prints, plus taker fee) earns a positive
settled return.

The lane exists to disprove this cheaply. "No variant clears the bar" is a
complete result. The V1 study (commit `f9ee969`, June 2026) killed a cruder
version: raw win-rate filters, a 100-trade floor, no shrinkage, and a replay
that granted the follower the whale's own fill. This design fixes each of
those and adds a signal-driven exit, a Polymarket-native settler, and
point-in-time scoring. It stays a REVIEW-lane measurement, never headline.

## 2. Decisions taken by the operator (2026-09-03)

| # | Decision |
|---|---|
| 1 | Paper only, pre-registered lane with a kill rule. |
| 2 | Polymarket only. Kalshi trades are anonymous. |
| 3 | Whales are self-discovered from prints in markets we track, plus the public leaderboard once verified. |
| 4 | Buckets are sport × market_type. Skill transfers partially across sports; never between sports and politics/crypto. |
| 5 | Scoring metric delegated: see §6. |
| 6 | No trade-count floor per whale. A confidence floor before we act. |
| 7 | Several point systems side by side. |
| 8 | Two stakes side by side per signal: flat $1 and confidence-weighted. |
| 9 | Cold start accepted; backfill from public per-wallet history where verified. |
| 10 | Kill/success criteria delegated: see §12. |
| 11 | Old infrastructure is rebuilt where it is in the way (settlement, stake, exits, dead config). |
| 12 | Bets can be left early when the consensus that opened them decays. |

## 3. Preconditions the operator must clear

- **The instrument is down.** The last watch poll in `data/arb.sqlite` is
  2026-08-09 and no supervisor process is running. The lane needs the watch
  lane's Polymarket quotes for the follower touch, the pregame gate, and
  markouts. Restart from a committed HEAD after a green `pytest` (rule 13).
- **ANJ fragility.** Polymarket was ISP-blocked in France 2026-07-16 to
  07-29. The collector writes to its own file and degrades to "record
  nothing, book nothing" on a block. Nothing in the lane assumes durability.
- **Terms of use.** Automated reads of `data-api.polymarket.com` are
  unauthenticated and reachable; whether they are ToS-permitted for this use
  is unresolved (the ToS page could not be fetched during research). Operator
  decision before the first poll; the collector polls conservatively and backs
  off on any 429 or challenge response.

## 4. Data sources and rule-3 verification gates

Every row below is UNVERIFIED until a dated line lands in
`data/verified_facts.md` with a recorded fixture. Task 1 of the plan does
nothing else. The lane is not built on any row that fails.

| Endpoint (all public GET) | Needed for | Must verify |
|---|---|---|
| `data-api/trades?market=<csv>&start=&end=&limit=&offset=&takerOnly=` | Print collector (batched fallback) | VERIFIED 2026-09-03 (Task 1): comma CSV `market=` batching works (3 markets, one call); `takerOnly=true` gives one row per fill, `false` every leg (2.2× rows); `limit` cap ≥ 10,000 on the global feed; `start`/`end` INCONCLUSIVE; `proxyWallet` present. Task 3 step 0 re-probes ordering, `offset`, and `start`/`end` |
| `data-api/trades?limit=10000` (no `market`) | Print collector (global, primary) | VERIFIED 2026-09-03: platform-wide feed, 10,000 rows per call against ≈1,400–2,000 rows/min. Probe 5 sent no `takerOnly`, so its effect on the global feed (and the "taker tape" reading) is unverified until Task 3 step 0 (i) |
| `data-api/v1/leaderboard?category=&timePeriod=&orderBy=&limit=&offset=` | Discovery only | Category values (is there a SPORTS category), fields, `offset` cap 1000 |
| `data-api/activity?user=&type=TRADE&limit=&offset=` | Cold-start backfill (full trade rows, so hygiene runs on them) | Fields, `offset` cap 5000. VERIFIED 2026-09-03 (Task 1 probe 8) |
| `data-api/closed-positions?user=&limit=50&offset=` | `avgPrice` cross-check only | VERIFIED 2026-09-03; losers-to-zero absent in the sample, so it is not a backfill source |
| `gamma-api/markets?condition_ids=<id>&condition_ids=<id>` (+ `closed=true` for resolved markets) | Resolution reader, bucket map for backfilled markets | Batching VERIFIED 2026-09-03 (fix pass) **only in the repeated-param form**; the comma CSV `condition_ids=<a>,<b>` returns nothing; `conditionId` singular is NOT a filter. Batch cap beyond 2 ids NOT PROBED. Resolved-market reachability and the resolved values of `closed`, `umaResolutionStatuses` (plural, JSON-encoded string), `outcomePrices` are re-probed at the start of Task 4 |
| `clob/books` (already verified) | Follower touch at signal and exit time | Nothing new |

Rate limits are undocumented; third-party estimates disagree by two orders
of magnitude. Config `whale.max_calls_per_min` (default 30) bounds the
collector; every non-200 is logged with its status and the poll backs off
`whale.backoff_secs` (default 300).

## 5. Architecture

Two write paths, never mixed (the `pm_tape.py` / `kalshi_history.py`
defence): the **collector and scorer** own `data/whale_tape.sqlite`; the
**lane** books through `arb.tracker.process_poll` into `data/arb.sqlite`
like every other booking lane. Rule 9 is structurally unreachable from the
collector.

| File | Responsibility | Lines |
|---|---|---|
| `arb/whale_tape.py` (new) | Poll prints for tracked markets, leaderboard snapshots, per-wallet backfill. I/O only. | ~220 |
| `arb/whale_resolve.py` (new) | Read Polymarket resolutions for markets with whale positions or WHALE bets; persist `resolutions`. Exposes `settle_auto_pm` used by `arb/settlement.py`. | ~150 |
| `arb/whale_score.py` (new) | Pure: prints → positions; positions + resolutions (as_of) → hierarchical scores; hygiene flags. No I/O, no clock. | ~250 |
| `arb/whale_signal.py` (new) | Pure: qualifying whales → points per side under V1/V2/V3 → fire/suppress with reasons; exit rule evaluation. | ~180 |
| `arb/lanes/whale.py` (new) | The loop: load fresh prints, score, evaluate signals, fetch books, write quotes rows, build candidates, book; evaluate exits on open WHALE bets. | ~250 |
| `arb/bench_whale.py` (new) | `LaneResult` wrapper for `arb bench` (L14). | ~120 |
| `arb/settlement.py` (modify) | Register `WHALE: resolution`; `open_resolution_bets` reads `RESOLUTION_SETTLED_KINDS`; `settle_auto_mlb` skips WHALE; exempt exits from the tripwire. | +40 |
| `arb/tracker.py`, `arb/commands.py`, `arb/invariants.py` (modify) | Replace the three hand-typed `('EV','NEGRISK','BTC')` tuples with `RESOLUTION_SETTLED_KINDS`; guard `settle_auto_pm` in `cmd_settle`. | +10 |
| `arb/reconcile.py` (modify) | Skip kind WHALE (single venue, single oracle; the resolution reader reconciles it). | +3 |
| `arb/dashboard.py`, `arb/regrade.py` (modify) | Unpack the three-part `by_class` key; percentile-parameterised `_bootstrap_mean_ci`. | +10 |
| `arb/tracker.py` (modify) | `stake_usd` from the candidate (default `Decimal(1)`); `variant` column pass-through. | +6 |
| `arb/models.py` (modify) | `KINDS += WHALE`; `RESOLUTION_SETTLED_KINDS += WHALE`. | +2 |
| `arb/eligibility.py` (modify) | `kind == "WHALE"` → `(False, "whale-signal", "review")`. | +2 |
| `arb/schema.py`, `arb/migrations.py` (modify) | Migration 7: `flat_bets.variant TEXT`, `flat_bets.signal_id INTEGER`. Add only. | +12 |
| `arb/metrics.py` (modify) | `settled` events with `source` starting `exit:` carry `pnl` in the payload; read it like any settled row. | +8 |
| `arb/report.py` (modify) | `by_class` key gains `variant`; a `whale` section. | +60 |
| `arb/heartbeat.py`, `arb/cli.py`, `arb/commands.py` (modify) | `whale` process, cadence branch, `POLL_KIND`, subcommands `whale --once`, `whale-backfill`, `whale-resolve`, `whale-score`. | +50 |
| `arb/config.py`, `config.yaml` (modify) | `whale:` block, schema, invariants. | +90 |
| `tests/test_whale_*.py` (new) | See plan. | |

Data flow per `whale --once` cycle (`whale.poll_secs`, default 60):

1. **Universe.** Polymarket sports markets from the latest watch poll with
   `start_utc` within `[now − whale.lookback_hours, now + whale.lookahead_hours]`
   (defaults 6, 48). Their `condition_id`, tokens, sport, market_type, line,
   outcome labels, and start come from `quotes`, read through a read-only
   connection (`replay.open_ro`) filtered by that poll's `poll_id` so the
   query is served by `idx_quotes_poll` and never scans the 37 GB table.
   Nothing is guessed.
2. **Collect.** `whale.collector_mode: global` (Task 1 fix pass: the global
   feed returns 10,000 rows per call against a platform rate of about
   1,400–2,000 rows/min): one `GET /trades?takerOnly=<whale.taker_only>&limit=<whale.page_size>`
   per cycle with no `market`, rows kept only when their `conditionId` is in
   the universe. Pages advance by `offset` only when a page is full and its
   oldest row is still newer than `last_seen − whale.overlap_secs` (120);
   they stop on a short page or on age, and never page past
   `whale.max_offset` (10,000: the feed returns 400 beyond it, observed
   2026-09-04); when the cap is hit with rows still newer than the bound,
   the collector records a `gap` (`meta.last_gap` = `[last_seen, oldest
   fetched]`, seconds and cycle) once, and advances the watermark to the
   NEWEST row fetched as on every other cycle (re-amended 2026-09-04, Task 8
   review: advancing only to the oldest row left the watermark pinned behind
   a fast feed and wrote a false 60 s gap every cycle), so a long outage
   costs one recorded gap, never a wedged collector. `start` is never relied
   on. The
   `batched` fallback polls the universe in groups of `whale.markets_per_call`
   using `market=<a>,<b>,<c>` (comma CSV, verified for `/trades` in Task 1
   probe 1; note Gamma's `condition_ids` is the opposite and needs the
   repeated-param form). Persist every kept row to `prints` with `observed_at`.
   Duplicates rejected by a unique index on
   `(tx_hash, proxy_wallet, asset, side, price, size, ts)`.
3. **Discover.** Any wallet whose cumulative pregame BUY notional in tracked
   markets exceeds `whale.discovery_min_notional_usd` (1,000) enters
   `wallets` with `source='tape'`. Leaderboard wallets enter with
   `source='leaderboard'` from a daily snapshot of every window in
   `whale.leaderboard.time_periods` (DAY, WEEK, MONTH, ALL; amended
   2026-09-04 from MONTH only) up to `whale.leaderboard.pages` (20, the
   documented offset cap 1000 / limit 50) each. Source never enters the
   score; membership in the latest snapshot is what V4 reads.
4. **Backfill.** For wallets not yet backfilled, FIFO by discovery time, up
   to `whale.backfill.wallets_per_cycle` (5): pull `/activity?type=TRADE`
   pages up to `whale.backfill.pages_per_wallet` (40 × `page_size` 50 =
   2,000 trades) into `prints` with `source='activity'`, deduplicated like
   live prints. Markets not in `quotes` are mapped to a bucket through Gamma
   by `condition_ids`; unmappable ones are stored with `bucket=NULL` and
   never scored. Their outcomes come from the same resolution reader as
   everything else (step 5). Backfill and the daily leaderboard snapshot run
   in their own supervisor process (`whale-backfill --once`, cadence
   `whale.backfill.cadence_secs` = 600, runtime cap 900 s), never inside the
   60 s cycle: at `max_calls_per_min` 30, five wallets × 40 pages is about
   seven minutes of calls (Task 3 acceptance measured 353 calls / 11 min).
5. **Resolve.** For every `condition_id` with an unresolved whale position or
   an open WHALE bet whose `start_utc` is more than `whale.resolve_after_hours`
   (4) in the past, read Gamma; persist `resolutions`. The 60 s cycle covers
   its own universe and open bets; the volume — every tape market with a
   whale position and no `resolutions` row, including backfilled ones —
   is swept by the `whale-backfill` process, FIFO by `start_utc`, bounded
   per cycle by `whale.backfill.resolve_per_cycle`, until
   `resolve_giveup_days` (final review 2026-09-04: without this sweep no
   wallet could ever qualify).
6. **Score.** `whale_score.scores(positions, resolutions, as_of=now, cfg)`.
7. **Signal.** For each market with prints since the last cycle, evaluate
   V1/V2/V3. Persist every evaluation to `signals`, fired or suppressed with
   a reason.
8. **Book.** For each fired (market, side, variant): fetch books, write
   `quotes` rows for both tokens (rule 7, and so markouts attach), build two
   candidates (flat, conf), `process_poll(...)`.
9. **Exit.** For each open WHALE bet, re-evaluate §9. On exit: fetch books,
   append the `settled` event with `source='exit:<reason>'`.

## 6. Whale positions and skill scoring

**Positions.** BUY prints by one wallet in one `(condition_id, outcome_index)`
within `whale.position_window_secs` (300) of each other form one position:
`avg_price = Σ price·size / Σ size`, `size = Σ size`; `entry_ts` = first BUY, `last_ts` = last BUY, and every gate (pregame, signal window, print age) reads `last_ts` (clarified 2026-09-03, Task 5).
SELL prints reduce the open size FIFO and are the input to the exit rule and
the market-maker filter; they never form a copy signal (Polymarket has no
native short). A position is **pregame** iff its last print precedes the
market's `start_utc`; only pregame positions are scored or signalled.

**Outcome per position.** Realized edge in probability points:
`e = payout − avg_price`, `payout ∈ {0, 1}` from `resolutions`. VOID markets
are excluded from numerator and denominator. When the whale sold part or
all of the position before resolution (SELL prints matched FIFO), the sold
shares realize their sell price instead of the payout:
`e = (Σ sold_size × sell_price + remaining_size × payout) / size − avg_price`
(pinned 2026-09-03). This is the whale's realized edge per share; a trader
who buys at 0.40, sells at 0.60, and then sees the team lose is scored
+0.20, not −0.40. This is the whale's own fill and
their own outcome, a fact, so the follower-fill fallacy does not arise here.
The lane's P&L (§8) is a different quantity and never uses the whale's price.

Why edge in points and not win rate: a wallet that buys favorites at 0.90
wins 90% of the time with zero skill. `e` subtracts the price. Its
expectation for a random taker is negative, about minus the half-spread and
fee; that is the base rate, measured not assumed.

Backfilled activity rows are full prints, so they build positions through
the same code path as live prints, pass the same hygiene filters (§13), and
resolve through the same reader. Scoring therefore starts on day one for
active wallets with no separate backfill logic. Backfilled prints carry
`source='activity'` and the report shows scores with and without them until
live-observed positions dominate. Backfill order is FIFO by discovery time,
so no second selection sits on top of discovery.

**Point-in-time.** `scores(..., as_of)` uses only positions whose
`resolved_at < as_of`. `as_of` is a required argument. A frozen-clock test
scores a fixture twice, once against the full fixture and once against the
fixture physically truncated to `resolved_at < T0`, and asserts identical
`Decimal` output.

**Hierarchy.** Two disjoint roots: `sports` and `nonsports`. Sports:
bucket (sport × market_type) → sport → root. Nonsports is a flat root and
is not scored in this version (recorded, not booked). No query joins across
roots; a structural test asserts `hierarchy_root` is in every aggregation.

**Model.** Normal–normal empirical Bayes with observation variance
`whale.score.obs_var` = 0.25 (`e` is bounded in `[−1, 1]` with sd ≈
√(p(1−p)) ≈ 0.5). Prior dispersions are named as skill spreads, not raw
pseudo-counts, so their meaning survives a change of `obs_var`:

| Level | Config | Default | Meaning |
|---|---|---|---|
| root | `whale.score.prior_sd.root` | 0.05 | spread of wallet skill around the base rate |
| sport | `whale.score.prior_sd.sport` | 0.03 | spread of a wallet's per-sport skill around its root skill |
| bucket | `whale.score.prior_sd.bucket` | 0.02 | spread of per-bucket skill around per-sport skill |

Top-down chain. At each level the prior is the parent's posterior mean with
total prior variance = that level's `prior_sd²` plus the parent's posterior
variance, which is the exact two-level Gaussian posterior, and the
effective pseudo-count is `k = obs_var / total_prior_var` (100 at the root
with no parent; smaller than 278 and 625 below because the parent's own
uncertainty widens the prior):

```
V_root    = prior_sd.root²
k_root    = obs_var / V_root
mu_root   = (n_root·ē_root + k_root·m0) / (n_root + k_root)
var_root  = obs_var / (n_root + k_root)

V_sport   = prior_sd.sport² + var_root
k_sport   = obs_var / V_sport
mu_sport  = (n_s·ē_s + k_sport·mu_root) / (n_s + k_sport)
var_sport = obs_var / (n_s + k_sport)

V_bucket  = prior_sd.bucket² + var_sport
k_bucket  = obs_var / V_bucket
mu_bucket = (n_b·ē_b + k_bucket·mu_sport) / (n_b + k_bucket)
var_bucket= obs_var / (n_b + k_bucket)

score_lb  = mu_bucket − z·sqrt(var_bucket)
```

At `n = 0` a level's variance is exactly `prior_sd² + var_parent`; as
`n → ∞` it is `obs_var / n`. The test in Task 5 pins both limits.

`m0` is the base rate: mean `e` over all scorable positions of all wallets in
the root over `whale.score.base_rate_lookback_days` (30), stored with its
`as_of` so scores are reproducible; below `whale.score.base_rate_min_n` (500)
the default `whale.score.base_rate_default` (−0.02) is used. Once at least
`whale.score.eb_min_wallets` (200) wallets have ≥ 20 positions, the three
`prior_sd` values are re-estimated monthly by method of moments
(`τ̂² = max(0, Var(ē_w) − mean(obs_var/n_w))`) and written as a dated
config amendment, never silently.

`z` is `whale.score.z` = 0.8416 (80%). A 90% column (1.2816) is reported
alongside and never acts.

**Worked numbers** (base rate −0.02; wallet at +0.08, i.e. 58% at price
0.50, all positions in one bucket):

| n | posterior mean | sd | lb80 | lb90 |
|---|---|---|---|---|
| 5 | −0.004 | 0.057 | −0.052 | −0.077 |
| 15 | +0.018 | 0.050 | −0.025 | −0.047 |
| 40 | +0.043 | 0.041 | +0.009 | −0.009 |
| 120 | +0.066 | 0.029 | +0.042 | +0.029 |
| 400 | +0.077 | 0.019 | +0.061 | +0.053 |

A wallet at +0.04 (54% at 0.50) clears zero at 80% around n = 110 and sits
at +0.020 by n = 300. This is the honest cost of "confidence floor, no trade
floor": skill of a few points takes a hundred or more resolved positions to
establish. Backfill is what makes that reachable.

**Transfer.** 200 MLB positions at +0.08 and 3 NBA positions at 0.00 give an
NBA bucket score of +0.044 ± 0.045, lb80 = +0.006: the wallet qualifies in
NBA on the strength of its baseball record, marginally, and is tagged
`transfer` on any NBA signal it joins. The same 3 NBA positions with no
history give lb80 = −0.067. That is the operator's "good at baseball,
decent at basketball" rule, and the model does it with one mechanism, not a
special case.

**Qualifying whale** for a market in bucket b: `score_lb(b) ≥
whale.min_score_lb` (0.00) and not hygiene-excluded.

## 7. Signal aggregation

Evaluated per market at cycle time T over qualifying whales with an open
pregame position (BUY minus SELL size > 0) whose last print is within
`whale.signal_window_secs` (7,200) of T and whose print notional is at least
`whale.min_print_usd` (200). **One wallet is one whale per side** (pinned
2026-09-03, Task 6 review): a wallet's several BUY windows on the same
`(market, side)` merge into one contributor with summed open size,
notional-weighted average price, and the newest `last_ts`; it is scored
once and carries one vote. The merge covers **all** of the wallet's open
windows on that side (its live exposure); the signal window gates
qualification through the newest window's `last_ts` only, and the exit
rule's `left` test compares like with like (pinned 2026-09-03, Task 6
re-review). Three pre-registered variants:

| Variant | points per whale on its side |
|---|---|
| V1 flat | 1 |
| V2 excess | `score_lb − min_score_lb` |
| V3 size | `(score_lb − min_score_lb) × log10(1 + open_notional / whale.size.unit_usd)`, `size.unit_usd` = 100; `open_notional = (size − sold_size) × avg_price`, the whale's live exposure on that side (pinned 2026-09-03, Task 6) |
| V4 ranked (added 2026-09-04, operator request) | 1 per qualifying whale that appears in the latest leaderboard snapshot (any window: DAY/WEEK/MONTH/ALL, category SPORTS), 0 otherwise. Same qualification bar as V1 (a whale must still clear `min_score_lb` on our own scores); rank is a filter, never a score input. `fire.min_points.V4` = 3, literal. Secondary, never promotable under this registration; its purpose is to test whether leaderboard rank adds information over our realized-edge scores — the literature's 12%-overlap finding says it should not |
| V6 dominance (added 2026-09-04, operator request) | `points_s = Σ_{w on s} q_w · N_w · R_w · F_w / max(V_24h, dominance.min_volume_usd)` with `q_w` the V2 excess clipped at zero, `N_w` the whale's live open notional on that side (SELLs already netted by `merge_windows`), `R_w` a half-life decay on print age inside the signal window, `F_w` a linear price-relative factor that goes to zero when the whale bought `fire.max_drift_points` below the current ask, and `V_24h` the venue's own 24-hour volume for the market (already read for the zombie guard; from Gamma `volume24hr` for a non-sports market). Units: probability points of skill per dollar of market flow. `fire.min_points.V6` = 0.0020, literal. Measured before registration on the 56 fired V1 signals (2026-09-04), using the tape's own 6-hour print volume as the denominator: the distribution of the skill-weighted net over window volume is p10 −0.0033, p25 +0.00006, p50 +0.00236, p75 +0.0138, p90 +0.0237, and **12 of the 56 fires had that quantity NEGATIVE** — a majority of whales on one side while the money sat on the other, which is precisely what V1–V5 cannot express. Secondary, never promotable under this registration. Implementation detail pinned 2026-09-04 (the formula names `fresh_avg_price`, which only an entry-tally whale has): a whale whose prints have aged out of `signal_window_secs` — one that appears in the holders recount of gate 1b, or in an exit — takes `avg_price`, its whole open exposure's VWAP, for `F_w`. Reading its missing fresh VWAP as 0 would zero every stale holder and quietly delete the standing opposition 1b exists to measure; the fallback is the reading that books fewer signals. Every context input is optional and a missing one is NO information, never zero: an unknown `V_24h` reads as the floor, an unknown ask leaves `F_w` at 1, and an unknown print age leaves `R_w` at 1 |

**Fire** when all hold for a side s:

1. `points_s / (points_s + points_other) ≥ whale.fire.ratio` (0.80).
1b. **Holders agreement (amended 2026-09-04).** The same tally recomputed
   over the whales still *holding* this market — `qualifying(...,
   fresh_only=False)`, i.e. every qualifying wallet's merged net pregame
   position whatever the age of the print that opened it — must give side s a
   share of at least `whale.fire.holders_ratio` (0.6667). Condition 1 asks
   whether the whales just moved; 1b asks whether the whales who are actually
   in this market agree. A market with no holders reading at all (nobody
   qualifying holds it) has nothing to disagree with and passes 1b.
   Suppression reason: `holders`, evaluated immediately after the count tests
   (`ratio`, then `min-points`/`uncalibrated`) so a variant still inside its
   calibration window keeps its own, more specific reason. The share is
   computed for every evaluation and persisted on the `signals` row as
   `holders_ratio`, whichever gate is reported. Registered because the exit's
   `flip` rule was moved onto the holders basis on 2026-09-04 (`a966232`)
   while entry stayed on the fresh basis: measured over the 56 fires to that
   date, 20 (36%) were already below `exit.flip_ratio` 0.50 on the holders
   basis at the moment of entry and would have been flipped out on the next
   cycle, paying the taker spread twice for nothing. Entry gate and §9's
   `flip` recount are ONE function (`whale_signal.holders_share`, imported by
   `arb/whale_exit.py`), so the two bases can never drift apart again.
2. `points_s ≥ whale.fire.min_points.<variant>`; V1 = 3 literally. V2 and V3
   are null for the first `whale.fire.calibration_days` (14) of `signals` (amended 2026-09-04: provisional calibration on the first day signals span ≥ 3 UTC days, re-run and replaced at ≥ 14 days — PREREGISTRATION.md row 2026-09-04 'provisional early calibration'; `calibration_days` is now 3)
   rows: they are evaluated and persisted but never fire. On day 14 the
   value is computed by one formula, written as a dated amendment, and
   frozen: take every whale that qualified in at least one evaluated signal
   during the window; each wallet is reduced to the median of its `score_lb −
   min_score_lb` over the window (one value per wallet); `q10` = the 10th
   percentile (nearest rank) of those values; `min_points.V2 = 3 × q10`;
   `min_points.V3 = 3 × q10 × log10(1 + median(open_notional) /
   size.unit_usd)` over the same wallets (pinned 2026-09-03, Task 6). Fewer than 10 such whales → the window extends 7 days at a time,
   reported each day. The lane cycle, which has the tape DB, enforces the
   "null only during calibration" rule: past the window with a null, it
   logs CRITICAL and books nothing for that variant. The config loader
   cannot see the tape, so this is not a loader invariant.
3. Two whales whose prints on this market fall within
   `whale.fire.sybil_gap_secs` (5) of each other count once.
4. Staleness: the current best ask on side s is at most
   `whale.fire.max_drift_points` (0.03) above the points-weighted VWAP of the
   contributing whales' **fresh** BUY prints (windows whose `last_ts` is
   inside `signal_window_secs`), not their whole-exposure average price: the
   gate asks whether we are chasing the prints that formed this signal
   (pinned 2026-09-03, Task 6 review #3). The merged contributor therefore
   carries both `avg_price` (all open windows) and `fresh_avg_price`.
5. Pregame with at least `whale.fire.min_secs_to_start` (600) remaining;
   depth at the ask ≥ `min_executable_usd`; the book is two-sided and not a
   zombie.

Every evaluation is persisted to `signals` with `fired`, `suppressed_reason`,
both point totals, the whale list, VWAP, touch, and depth. Suppressed
signals are the "left on the table" of this lane and are reported.

**Primary variant: V2.** V1 and V3 are secondary, reported for sign and
magnitude agreement. They are never promotable on their own under this
registration; nominating one would be a new, dated pre-registration with
its own replication month. Family control is the mandatory replication
month.

Every `signals` row records, per contributing whale, its own-bucket `n`
and whether it qualified on its own bucket (`native`) or only through
pooled sport/root evidence (`transfer`), so the report can split signals
by native versus transfer-only whales. **Definition (pinned 2026-09-03):**
`native` iff the whale's bucket-only score, computed with the chain fed
nothing but that bucket's own positions (`n_root = n_sport = n_bucket`,
same base rate and priors), also clears `min_score_lb`. No new config key. If the operator's transfer
assumption is wrong, that split shows it instead of diluting the aggregate.

## 8. Booking

Per fired (market, side, variant), two `flat_bets` rows, distinct
`fingerprint` (`sha1(base_fp|variant|sizing)`) and `variant` column values
`V2/flat`, `V2/conf` etc., sharing one `signal_id`:

- **flat**: `stake_usd = 1`.
- **conf**: `stake_usd = min_stake_usd + clip((S − min_score_lb) /
  cap_points, 0, 1) × (max_stake_usd − min_stake_usd)`, `S` = points-weighted
  mean `score_lb` of the whales on the side; `whale.size.cap_points` = 0.10,
  `max_stake_usd` = 1.00, `min_stake_usd` = 0.10. So $0.10 at the
  qualification bar, $1.00 at ten points above it. Bounded, monotone, no
  Kelly.

Each row has one leg: venue `polymarket`, the outcome token on side s,
`cost` = best ask at booking, `fee_cost` via `leg_fee_cost(cfg, "polymarket",
"sports", ask)`, `depth_usd`, `mid0`, `venue_market_id = condition_id:token`.
**Leg convention (pinned 2026-09-03, Task 4 review):** a WHALE leg always
BUYS one token, so `side = "YES"` and `outcome` = that token's own label
(`"Rays"`, `"Down"`, or literally `"No"` on a Yes/No market), exactly as the
watch lane's Polymarket sports `quotes` rows are written. `side = "NO"` is
the ledger's complement-inversion flag and is never used for WHALE legs;
settlement decides by token identity (`venue_market_id` vs the winning
`clobTokenIds` entry), and the label is only used to name the losing side.
`kind = WHALE`, `category = sports`, `event_key = pm:<event_slug>` always,
never the cross-venue matched key; `position_key` is the generic
`sport|event_key|outcome|market_type|line|YES` with a `|<variant>/<sizing>`
suffix, because each variant × sizing is its own paper position by design
and the tracker's position dedup would otherwise absorb a variant that
fires on a later cycle as a re-sighting of another (pinned 2026-09-04,
Task 8): `event_slug` from the print feed groups
every market of one game (moneyline and total share it) even when our
matcher has no cross-venue event, so `event_key` doubles as the cluster key
for every CI without a new column. The lane's own `quotes` rows carry the
same `event_key`, so markouts join on it; the watch lane's rows for the same
market keep their matched key and are not used for WHALE marks.

`classify()` returns `(False, "whale-signal", "review")` for kind WHALE
before any other check, so the lane is REVIEW by construction and never
touches the headline. The lane applies its own depth, pregame, and staleness
guards before booking and records rejects through `_lot` with reasons.

The tracker's confirmation sighting and `lifetime_closed` logic apply
unchanged with `poll_kind = 'whale'`. Markouts attach automatically because
the lane writes `quotes` rows under the same key tuple at every cycle for
markets with open WHALE bets.

## 9. Exits

One live exit policy, evaluated every cycle for every open, unsettled WHALE
bet while the market is pregame. Post-start, bets are held to resolution:
in-play prices are game state, not information about the whales, and the
instrument is pregame-only.

Exit when any of:

| Reason | Rule | Config |
|---|---|---|
| `flip` | Recomputed ratio for our side under the bet's own variant < `whale.exit.flip_ratio` | 0.50 |
| `left` | Whales that formed the signal have net SOLD ≥ `whale.exit.sold_fraction` of the size they held at booking | 0.50 |
| `left` (via positions) | The same test against what data-api `positions?user=` says those whales HOLD now | 0.50 |
| `confidence` | Points-weighted mean `score_lb` of the remaining whales on our side < `whale.exit.min_score_lb` (scores move as their other positions resolve); a whale whose points went negative since booking weighs zero, never negative (pinned 2026-09-03) | −0.01 |

**Amended 2026-09-04 (PREREGISTRATION row of that date), three sentences.**
(1) The `flip` recount runs over the whales still HOLDING — every wallet's
merged net position on this market across all open windows, scored and
pointed under the bet's own variant — not over the whales whose prints are
still inside `signal_window_secs`: on day one 12 of 14 flips fired with
`points_side` 0 and `points_total` 1–2 after 55–153 minutes because our own
whales' prints had aged out while one opposing fresh print remained, and
ageing out is not a change of mind (`arb/whale_merge.qualifying(...,
fresh_only=False)`).
(2) The print collector runs `takerOnly=false`, so both legs of every fill are
recorded and a whale exiting through a RESTING sell order appears as its own
SELL leg under its own wallet — invisible under `takerOnly=true` (live probe,
`data/verified_facts.md` 2026-09-04); the cost is ~2.8× the rows, and a maker
quoting both sides of a token now correctly trips the §13 wash filter.
(3) A SECOND `left` test reads each signal whale's CURRENT holding of the
bet's token from data-api `positions?user=<wallet>` — polled every
`whale.exit.holdings_poll_secs` per wallet, at most
`whale.exit.holdings_calls_per_cycle` wallets a cycle, stalest first, every
snapshot persisted to the tape's `holdings` table (rule 7) — and exits `left`
with `via: "positions"` when those holdings fall to ≤ `(1 − sold_fraction)` of
the size held at booking; the prints-based test keeps `via: "prints"` and
whichever fires first wins. A snapshot older than `holdings_poll_secs ×
holdings_max_age_polls` is no reading and that wallet falls back to its
prints-based size — exiting on a stale holding is the same mistake as (1). An
entirely EMPTY positions body is likewise read as no reading rather than a
verified zero: rule 3 never probed what an empty body means, and reading it as
"sold everything" would exit every bet behind that whale on one bad response,
while a whale that truly sold everything now prints its SELL leg under (2).
The report and `/api/whale` break `left` down by `via`, each with its own
exit-vs-hold counterfactual.

**Amended 2026-09-04 (holdings-paging fix).** A full `positions?user=` page
now pages by `offset` (verified live 2026-09-04) up to `holdings_max_pages`
or a short page, whichever comes first, so a token that only shows up past
page 1 is read at its real size rather than as a hard zero; a wallet still
full at `holdings_max_pages` is marked `partial`, and a row-level absence in
a partial snapshot is read as no reading (falls back to prints) rather than
a verified zero — presence is still trusted wherever it lands.

Exit price = best bid − taker fee, priced from a fresh `fetch_books`. No bid,
or depth at the bid below `min_executable_usd`: no exit, annotation
`exit-no-bid`, re-evaluated next cycle. A position that is still held (an
open WHALE bet exists for the same `position_key`) is never booked again
while it is open, whatever the tracker's lifetime bookkeeping says: the
lane drops such candidates as `position-held` (final review 2026-09-04).
An open WHALE bet for exit
purposes is one with no `settled`/`void` event; the tracker's
`lifetime_closed` means the flag left the feed, not that the position
closed, and never stops exit evaluation (pinned 2026-09-04); the dashboard's
open-positions view uses the same definition. The lane inserts the exit's
`settled` event directly through `store.insert`, never through
`settle_bet`: that function's future-start guard rejects any settlement
before the event starts, which is exactly when exits happen (Task 4
finding). The tripwire exemption by `source` prefix stays as a second
guard. A `confidence` exit records which
whale, which hierarchy level (its `n_bucket/n_sport/n_root` now vs at
booking), and the size of the drop that moved the score (amended
2026-09-04: the pure exit rule cannot name the resolved position; the
wallet and level are the audit trail), because pooling means a loss in another sport can lower a whale's
score on this market; that is the transfer model working as specified, and
it must be auditable rather than silent. The exit is one append-only
`bet_events` row: `kind='settled'`, `payload = {result: 'EXITED', source:
'exit:<reason>', exit_price, pnl}`. `open_resolution_bets` already excludes
bets with a `settled` row, so an exited bet is never re-settled. The
instant-settle tripwire exempts `source` beginning `exit:`.

**Hold counterfactual.** `whale_resolve` keeps resolving the market. The
report computes hold-to-resolution P&L for every exited bet read-time, so
"exit rule vs hold" is a measured comparison, pre-registered as secondary.
The §10.8 policies (`take_profit`, `trailing`) stay unimplemented and are
stated as such; the `exit:` config block is replaced by `whale.exit`.

## 10. Settlement rebuild

`settle_auto_pm(cfg, store, now)` in `arb/whale_resolve.py`, called from the
`settle` process after `settle_auto_mlb` and **wrapped so an exception is
logged and the cycle continues** into `reconcile()` and the invariant
`check()`: for every open resolution bet with a Polymarket leg (kind WHALE
now; EV bets with PM legs are a stated later extension), read Gamma by
`condition_ids` with `closed=true` (the only query verified to reach
resolved markets; Task 4 re-probes it first), and when `closed` is true and
`outcomePrices` names a winner (one outcome at 1, the other at 0, exact
values as recorded by Task 4's fixture), call `settle_bet(...,
source='pm-gamma:<condition_id>')`. `outcomePrices` at 0.5/0.5 or a void
entry in `umaResolutionStatuses` settles VOID.
Markets closed without a winner after `whale.resolve_giveup_days` (14) get
an annotation, never a guess. `settle_auto_mlb` skips kind WHALE (one
oracle per kind: the whale's outcome and the bet's settlement come from the
same Polymarket resolution). `reconcile()` also skips WHALE: single venue,
single oracle, and the resolution reader is its reconciliation.

Every place that spells out the resolution-settled kinds by hand today
(`tracker._active_bets`, `commands.cmd_status`, `invariants` check 3)
imports `RESOLUTION_SETTLED_KINDS` instead, so WHALE is treated as
resolution-settled everywhere at once. Without this, a settled or exited
WHALE bet stays "active" in the tracker and a re-fired signal on the same
market after a `left` exit is silently swallowed.

The resolution reader is shared with the scorer, so one verified field set
drives both whale outcomes and bet settlement, and a field change breaks
both loudly.

## 11. Ledger changes

- `flat_bets.stake_usd` is taken from the candidate (`cand.get("stake_usd",
  Decimal(1))`). Every existing lane is unchanged.
- Migration 7 adds nullable `variant` and `signal_id` to `flat_bets`. Only
  ADD COLUMN, consistent with every prior migration.
- `bet_events` vocabulary gains the `settled` payload `result='EXITED'`.
  No new event kind, so the dashboard and `tickets.py` keep working;
  `metrics.bet_pnl_optimistic` gains one early branch, before its
  WON/LOST/VOID guard, that returns `payload.pnl` when `source` begins
  `exit:`.
- `SCHEMA_VERSION` becomes 7.
- The rule-9 grep test runs unchanged over the new files.
- The daily report's `by_class` key becomes `(kind, lane, variant)`; the
  three sites that unpack it (`report.write_daily`, `report.print_daily`,
  `dashboard.state`) change in the same commit, and existing rows group
  exactly as before because their `variant` is NULL.

## 12. Reporting, kill, power

**Report.** `arb report --daily` groups by `(kind, lane, variant)`. A
`whale` section prints: wallets tracked / backfilled / hygiene-excluded;
qualifying whales by root and sport at lb80; signals evaluated / fired /
suppressed by reason; bets by variant; settled ROI per $ with an
`event_slug`-clustered bootstrap CI; win rate as a diagnostic; +2m and +10m
pregame markout coverage and mean drift; exits by reason with exit-vs-hold
P&L; base rate and its n. `arb bench` gains L14 with a `LaneResult` whose
primary is V2/flat settled ROI.

**Dashboard (operator request, 2026-09-03).** The existing read-only local
panel (`arb dashboard`, `dashboard.host:port`) gains: cumulative settled P&L
curves per `(kind, lane, variant)` over `dashboard.pnl_days`; an open
positions table with the current bid, unrealized P&L at bid minus fee, and
for WHALE rows the contributing whales and live exit-rule status; the
latest bench verdict per lane L1–L14 from a new append-only `bench_results`
table; and a whale panel (wallets, qualifying whales, signals, exits, base
rate, collector health, calibration status). GETs only, no chart library,
whale DB opened read-only. Plan Task 12.

**Primary metric.** Mean settled ROI per $ staked, V2/flat, clustered by
`event_key` (= `pm:<event_slug>` for every WHALE row), 90% CI.

**Every interval in this section is one-sided.** "Upper 90% bound" is the
90th percentile of the cluster-bootstrap distribution of the mean; "lower
80% bound" is its 20th percentile. `regrade._bootstrap_mean_ci` hardcodes
two-sided 95% percentiles, so Task 9 parameterises it with an explicit
percentile pair and the two-sided default stays for its existing callers.

**Named quantities.** `flagged_edge` of a bet = `S − m0` at signal time,
where `S` is the points-weighted mean `score_lb` of the contributing whales
and `m0` the base rate then in force; stored on the `signals` row and
copied to `flat_bets.edge_points`. Drift `D_h` = mark mid at horizon h minus
`mid0` on the leg (the existing `markouts` definition), pregame marks only.
"Significantly adverse" at +10m means the one-sided 90% upper bound of mean
`D_10m` is below zero.

**Powered early metric.** Mean +10m pregame markout of the follower's entry,
one-sided adverse. Per-bet sd ≈ 0.03 points, so at n = 100 the MDE is
about 0.8 points; at n = 300 about 0.45. ROI per $ has sd ≈ 1.0 for near-even
markets, so with a 1.5 design effect the MDE is 0.26 at n = 100, 0.15 at
n = 300, and 0.075 at n = 1,200. Signals skewed toward favorites have lower
per-bet variance, which makes these MDEs conservative, not wrong. ROI can kill an obviously bad signal early
and can only confirm a modest one at the EV lane's `target_n` scale.

**Kill rules (any fires → booking stops, collection continues):**

1. At n ≥ 100 signals (V2/flat): upper bound of the 90% clustered CI of
   mean ROI < 0.
2. Fast kill: mean `D_2m` (pregame marks) < −0.5 × mean `flagged_edge` over
   a rolling 30-day window with ≥ `kill.fast_kill_min_marked` (50) marked
   bets, on two consecutive *produced* daily reports. A day with no report
   (outage) pauses the streak; it does not reset it. "Produced" means the
   day has a line in the append-only `reports/daily/HASHES.log` chain that
   `write_daily` appends; the streak is derived read-time by re-evaluating
   the condition as of each produced day's end, never stored (pinned
   2026-09-04, Task 12 review). The same rule applies to
   the calibration window: outage days do not count toward its 14 days.
3. At n = 1,200 without the lower 80% bound above zero: NULL, kill.
4. Starvation: fewer than `whale.kill.min_signals_per_week_alarm` (10) fired
   signals per week after 4 weeks → the design is reported as starved and
   the operator decides.

**Success.** At n ≥ 100, lower 80% bound > 0 and +10m markout not
significantly adverse → continue into a second, non-overlapping calendar
month; both months must pass independently before any promotion claim. Even
then the lane stays REVIEW: a promotion claim is a report, not a lane change.

**Throughput.** `E[days to N] = N / (G · f_print · f_fire)` with G = markets
entering the pregame window per day (measured in week one; order of a few
hundred), `f_print` = share with a qualifying-whale print, `f_fire` = share
of those that fire. Dashboard: rolling 7-day signals/day by variant,
qualifying-whale count, and backfill progress.

### Trader profiles (read-time) — added 2026-09-04

Operator ask: *rank every trader to see where each one is good; success rate
above average based on risk; what markets they are good in.* `arb/whale_rank.py`
answers it from the positions, resolutions and base rates `whale_state` already
built — no new endpoint, no new collector, no second scoring pass.

**These are DIAGNOSTICS, not inputs.** Nothing under `whale.rank` reaches
qualification, points, sizing, exits or firing; V1–V4 and `min_score_lb` are
unchanged. `whale.rank.min_n` (5) is a *display* floor — a profile below it is
computed and returned, just not ranked. Hygiene-excluded wallets are ranked and
flagged, never hidden: "which of my whales is really a market maker" is exactly
what the table is for.

One row per (wallet, level, key), level ∈ root / sport / bucket, over every
position that `whale_score.resolved_outcomes` says is scorable as of `as_of`:

| Field | Definition |
|---|---|
| `n`, `wins`, `win_rate` | resolved positions; payout 1 on the side held; wins / n |
| `implied` | size-weighted mean `avg_price` — the break-even win rate the market quoted |
| `excess_win_rate` | `win_rate − implied`: success above average **given the risk taken** |
| `edge_mean`, `edge_sd` | mean and sample sd of realized edge in points (unshrunk; sd 0 at n = 1) |
| `roi_at_risk` | Σ e·size / Σ avg_price·size — dollar-weighted return on capital at risk |
| `t_stat` | `edge_mean / (edge_sd/√n)`, null at n < 2 or a flat sample |
| `lb80` | the shrunk lower bound: `whale_score.posterior` truncated to that level (at bucket level it IS `score_for(...).lb`, the number the fire rule reads) |
| `excess_vs_base` | `edge_mean − m0` for the row's root |
| `notional`, `last_resolved_at`, `native`, `hygiene_flag` | Σ notional; latest resolution; bucket rows are the wallet's own record, sport/root rows are aggregates; hygiene-excluded |

Surfaces: `arb whale-rank [--level|--wallet|--top|--min-n]`,
`/api/whale/traders?level=&top=&min_n=&wallet=` with a sortable Traders table on
the dashboard, and a "Top traders" + "where X is good" block in the daily
report's L14 section (deterministic: `as_of` is the report day's end).

## 13. Hygiene filters and threats

Filters, all pre-registered, applied at scoring and signalling:

| Flag | Rule | Config |
|---|---|---|
| `mm` | Wallet BUYs both outcomes of one market within `mm_window_secs` (600) on ≥ `mm_market_share` (0.30) of its markets, with mean print notional < `mm_max_avg_usd` (50) | exclude |
| `both_sides` | Simultaneous exposure on both outcomes beyond `both_sides_tolerance` (0.10 of the larger side) on that market | exclude the market's positions |
| `dust` | Print notional < `min_print_usd` (200) | print ignored |
| `wash` | Opposite prints by the same wallet within `wash_gap_secs` (2) at the same price | exclude |

Threats and their status:

- **Taking pays the spread.** This project has already measured that
  crossing the spread costs about 13× the edge on the one fully measured
  cover, and that posting is adversely selected (memory: neither side pays).
  This lane takes at the touch by design; the measured base rate `m0`
  absorbs the average cost of taking, and the follower touch plus fee
  charges the actual cost per bet. The lane can only win if whale consensus
  is worth more than that cost, which is the claim under test.
- **Taker-only tape.** The trades feed defaults to taker fills. Task 1
  decides the collector's `takerOnly` value and records it; if the feed
  cannot return maker fills, whale discovery sees only aggressive flow and
  misses patient limit-order whales. Stated as a discovery bias either way.
- **Whale price impact**: in the follower touch by construction.
- **Poll latency**: `observed_at − ts` is stored; a signal fires only if the
  newest contributing print is younger than `whale.max_print_age_secs`
  (300). The touch is fetched at signal time, never inferred, so the P&L
  basis is honest even when the signal is late.
- **In-play whales**: unmitigated blind spot, stated.
- **Off-venue hedgers**: unmitigated.
- **Sybils**: the 5-second gap heuristic only. Stated.
- **Leaderboard survivorship**: discovery only, weight zero.
- **Winner's curse from discovery**: the acting score is a continuously
  recomputed lower bound, and the replication month is mandatory. Any
  single-month positive is presumed inflated.
- **Backfill bias**: `/closed-positions` was found (Task 1, probe 10) to
  omit positions held to zero, so it is not used for scoring. `/activity`
  lists every trade regardless of outcome, and outcomes come from our own
  resolution reads, so backfill carries no winner-selection. The residual
  risk is `/activity`'s 5,000-row offset cap truncating very active wallets
  to their most recent trades; stated, and bounded by `pages_per_wallet`.
- **Regime change**: replication month; recency weighting deferred.

## 14. Config (`whale:` block, all new keys; strict loader schema updated)

```yaml
whale:
  enabled: true
  tape_db: data/whale_tape.sqlite
  poll_secs: 60
  lookback_hours: 6
  lookahead_hours: 48
  overlap_secs: 120
  collector_mode: global            # one platform-wide call per cycle; `batched` = per-market fallback
  markets_per_call: 20              # batched mode only; /trades takes comma CSV `market=` (verified)
  max_calls_per_min: 30
  backoff_secs: 300
  max_print_age_secs: 300
  min_print_usd: 200
  discovery_min_notional_usd: 1000
  backfill: { wallets_per_cycle: 3, pages_per_wallet: 40, page_size: 50,   # 5 -> 3 amended 2026-09-04: the child twice exceeded its 900 s cap
              cadence_secs: 600, resolve_per_cycle: 100, map_per_cycle: 100 }   # own supervisor process; order: leaderboard -> resolution sweep -> wallet pages -> Gamma mapping; resolve 40->100 and map_per_cycle added 2026-09-04 after the child hit its 900 s cap on unbounded mapping; map_per_cycle 40->200 amended 2026-09-04 (V4 review): worst case leaderboard 80 + sweep 50 + wallets 120 + map 100 Gamma calls ~= 11 min, under the 900s cap; map_per_cycle 200->100 amended 2026-09-04 (mapper fix pass 2): the mapper's Gamma query gained a second, closed=true pass for any id the default index misses (F0), which can double mapping's Gamma calls — at 200 that pushed the worst case to 450 calls ~= 900s, exactly this cap with no margin; 100 restores the same 350-call / ~700s worst case (2 passes x ceil(100/2) calls) with ~200s margin — amended 2026-09-04 (legacy re-map): GAMMA_IDS_PER_CALL verified live at 20, not 2 (data/verified_facts.md); shipped resolve_per_cycle and map_per_cycle are both 1000, ten times this block's 100, exactly offsetting the ten-times-larger Gamma batch — worst case is unchanged: sweep ceil(1000/20)=50 + mapping 2 x ceil(1000/20)=100, so leaderboard 80 + sweep 50 + wallets 120 + map 100 = 350 calls, ~700s of the 900s cap, same margin as before
  universe_poll_lookback: 5         # watch polls searched backwards for one with Polymarket rows (2026-09-04); older than data_stale_alarm_secs -> no universe
  page_size: 10000                  # trades rows per call (10,000 verified 2026-09-03); paging stops on age, never on `start`
  max_offset: 10000                 # the feed 400s past this (observed 2026-09-04); beyond it we record a gap and move on
  taker_only: false                 # Task 1 probe 3: `true` is one row per fill; `false` returns every leg — flipped true -> false 2026-09-04 (L14 amendment, item 2 of §9's amendment above): maker legs carry the maker's own proxyWallet and side (live probe, data/verified_facts.md), so `true` hides every whale that exits through a resting order; ~2.8x rows, so a 10,000-row page spans ~2.4 min at today's flow — still over the 60 s cycle, and offset paging to max_offset 10,000 is unchanged and verified
  leaderboard: { enabled: true, category: SPORTS, time_periods: [DAY, WEEK, MONTH, ALL], order_by: PNL, pages: 20 }   # amended 2026-09-04: all windows, full depth (80 calls/day)
  resolve_after_hours: 4
  resolve_giveup_days: 14
  position_window_secs: 300
  signal_window_secs: 7200
  min_score_lb: 0.0
  score:
    obs_var: 0.25
    prior_sd: { root: 0.05, sport: 0.03, bucket: 0.02 }
    z: 0.8416
    z_report: 1.2816
    base_rate_lookback_days: 30
    base_rate_min_n: 500
    base_rate_default: -0.02
    eb_min_wallets: 200
  hygiene: { mm_window_secs: 600, mm_market_share: 0.30, mm_max_avg_usd: 50,
             both_sides_tolerance: 0.10, wash_gap_secs: 2 }
  fire: { ratio: 0.80, holders_ratio: 0.6667,   # 1b, amended 2026-09-04: entry also needs this share of the whales still HOLDING, between exit.flip_ratio (0.50) and fire.ratio (0.80) so a fired signal is never already flip-eligible; invariant exit.flip_ratio < fire.holders_ratio <= fire.ratio
          min_points: { V1: 3, V2: null, V3: null, V4: 3, V6: 0.0020 },   # V6 (dominance, 2026-09-04) is a LITERAL like V1/V4, never null
          calibration_days: 3, calibration_min_whales: 10,   # calibration_days 14 -> 3 amended 2026-09-04 (provisional calibration)
          sybil_gap_secs: 5, max_drift_points: 0.03, min_secs_to_start: 600 }
  dominance: { half_life_secs: 1800, min_volume_usd: 5000 }   # V6, added 2026-09-04: R_w's half-life on print age, and the floor on the normalising 24h volume (unknown volume reads as this floor, never as zero)
  size: { unit_usd: 100, cap_points: 0.10, max_stake_usd: 1.00, min_stake_usd: 0.10 }
  exit: { flip_ratio: 0.50, sold_fraction: 0.50, min_score_lb: -0.01,
          holdings_poll_secs: 300, holdings_calls_per_cycle: 10,          # L14 amendment 2026-09-04, item 3 of §9's amendment: the authoritative holdings check. Per-wallet cadence and the per-cycle GET budget (10 of whale.max_calls_per_min 30, on the same shared bucket) — a roster of <=50 wallets is fully refreshed inside one cadence, a bigger one rotates stalest-first rather than starving
          holdings_page_size: 100, holdings_max_age_polls: 3 }            # `limit` on positions?user= (paging past one page NOT probed: a full page logs WARNING once and what came back is used), and how many cadences old a snapshot may be (300 x 3 = 900 s) before that wallet falls back to its prints-based size
  primary_variant: V2
  kill: { decision_n: 100, null_n: 1200, fast_kill_min_marked: 50,
          min_signals_per_week_alarm: 10 }
```

**Amended 2026-09-04 (holdings-paging fix):** a fifth `exit` key,
`holdings_max_pages: 4`, bounds offset-paging past the first
`positions?user=` page to 2,000 positions or a short page; each extra page
spends one call of the same `holdings_calls_per_cycle` budget above, and a
wallet still full at the cap is marked `partial` in the tape (see §9).

Loader: the strict schema gains an optional-number leaf type
(`int | Decimal | None`) used only for `fire.min_points.V2` and `V3`, since
the existing `NUM` leaf rejects `null`. Invariants: `fire.ratio > 0.5`;
`exit.flip_ratio < fire.ratio`;
`exit.flip_ratio < fire.holders_ratio ≤ fire.ratio` (added 2026-09-04 with
gate 1b); `size.min_stake_usd ≤ size.max_stake_usd`;
`poll_secs ≥ 30`; `fire.min_points.V2/V3` may be null only while `signals`
has fewer than 14 days of rows (a startup warning, a hard error after).

## 15. Documentation amendments

- `STRATEGIES.md` §3: new `### L14 (new 2026-09-03). Whale consensus — the
  V1 debunk re-tested with post-print pricing, shrinkage, and exits`, citing
  L11's scrap reasons and what changed.
- `PREREGISTRATION.md` amendment log: hypothesis, primary metric, variants,
  kill rules, exit policy, replication requirement, dated before the first
  `whale` poll.
- `data/verified_facts.md`: one dated row per endpoint and field in §4.
- `SPEC.md` §5 "deliberately absent" note gains a pointer: the whale tracker
  is rebuilt as L14 under the §3 debunk's corrections.
- `config.yaml`: the dead `tape_check` and `exit` blocks are removed and
  their intent recorded in KNOWN_ISSUES.md, replaced by `whale`.

## 16. Out of scope, stated

Kalshi (anonymous). Nonsports scoring and booking (recorded only). Live
order placement (rule 1, forever). Kelly sizing. §10.8 take-profit and
trailing policies. Sub-minute book snapshots. Polynode paid tiers. A global
trades feed unless Task 1 verifies it.
