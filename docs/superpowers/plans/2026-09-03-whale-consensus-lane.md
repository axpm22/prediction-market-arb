# L14 Whale-Consensus Lane — Implementation Plan

> **Task 1 decisions (2026-09-03):** Rule-3 probes run against live Polymarket
> `data-api`/`gamma-api` endpoints (10 probes, `scripts/probe_whale_endpoints.py`,
> results dated 2026-09-03 in `data/verified_facts.md`). Decisions:
> (a) **collector mode = `batched`** — **[corrected in the 2026-09-03 fix
> pass]** probe 5's global feed exists, but "caps at 1000 rows/call" was
> unsupported and is now disproven: a follow-up `limit=10000` call on the
> same global feed returned exactly 10000 rows, not 1000. The true ceiling is
> ≥10000 and still not located. The decision is unchanged because it never
> rested on the raw cap — it rests on implied throughput, which stayed below
> the 3,000 rows/min gate on both runs (≈2,000 then ≈1,400 rows/min).
> (b) **backfill disabled** — probe 7's `closed-positions` fields do include
> entry price (`avgPrice`) and `outcome`, but probe 10 found 0 losers-to-zero
> in 30 closed positions for the probed wallet (all 30 had `avgPrice > 0` and
> none had `curPrice == 0`), so the "shows losers-to-zero" half of the gate is
> not met. **[2026-09-03 fix pass]** Cross-referenced against `/activity` for
> the same wallet: 67 markets were bought, only 30 (all fully-redeemed
> winners, `curPrice==1`, `realizedPnl>0`) appear in `closed-positions` — 37
> traded markets are simply absent, not present as losers or zeros. Decision
> stands (disabled); the gap this leaves is wider than "no losers observed"
> suggested — most traded history isn't in this endpoint at all. Small
> sample (one wallet); re-check before permanently ruling this out.
> (c) **leaderboard enabled** — probe 6's `category=SPORTS` returned 200
> directly (no fallback category needed). **[2026-09-03 fix pass]** This is
> now on firmer ground: `SPORTS` was compared against `OVERALL` (the
> documented default) and the two return different `proxyWallet` sets
> (29/50 overlap) — `category` has a real, confirmed filtering effect, it is
> not a no-op that happened to return 200.
> (d) **resolution fields, named exactly as observed** (gamma `markets`
> endpoint, matched via `condition_ids=<id>` against a live market): `closed`
> (bool), `umaResolutionStatuses` (**plural** — the design doc's assumed
> `umaResolutionStatus` singular field does not exist), `outcomePrices`,
> `gameStartTime`, `clobTokenIds`, `sportsMarketType`, `endDate`.
> `umaResolutionStatuses`, `outcomePrices`, and `clobTokenIds` are all
> **JSON-encoded strings** (e.g. `"[]"`, `"[\"0.445\", \"0.555\"]"`), not
> native lists — a consumer must `json.loads()` them.
> **[CORRECTED in the 2026-09-03 fix pass — the original batching claim was
> wrong.]** A real 2-id test (not the original single-id call, which had no
> comma to parse and so proved nothing about batching) shows: the
> comma-joined CSV form `condition_ids=<id1>,<id2>` returns **0/2 matches —
> BROKEN**; the repeated-param form `condition_ids=<id1>&condition_ids=<id2>`
> returns **2/2 matches — WORKS**. Any collector code must use the
> repeated-param form, never comma-joined CSV, for `condition_ids`. Batch cap
> beyond 2 ids: **NOT PROBED**. `conditionId` (singular) is silently ignored
> and returns the same unrelated 20-row default page regardless of value
> (page saved as `whale_gamma_conditionId_singular_default.json`). The
> design's original 2-days-old batch-id list (r1/r2) was also re-examined:
> those ids are NOT ">2 days old" vs a "live" id — r1, r2, and the matched
> c1 are all from the same watch poll (`started_at=2026-08-09`), ~24 days
> old as of this probe. Why c1 matches gamma's default index and r1/r2 don't
> is UNKNOWN (closed? archived? something else?) — still NOT VERIFIED. No
> `closed:true` market was reached by this or the original probe, so
> resolution-time values (including any winner field) remain UNVERIFIED.
> (e) **`whale.taker_only = true`** — isolates one row per fill from the
> taker's own side, avoiding double-counted rows when grouping prints by
> wallet. Probe 3's row-count difference, the discovery-bias number for spec
> §13: `takerOnly=false` returned 11 rows for the same 5 underlying fills that
> `takerOnly=true` returned as exactly 5 rows (1 per `transactionHash`, 0
> duplicates) — a 6-row / ~2.2x inflation from counting both legs of each
> match.
>
> The acceptance criterion's degraded fallback (one call per market, markets
> starting within 6h only, `whale.lookahead_hours=6`) was not triggered: it
> only applies if BOTH probe 1 and probe 5 fail, and both worked (probe 1's
> CSV batching returned rows from all 3 requested markets; probe 5's global
> feed exists). (a)'s `batched` mode is the normal per-market CSV-batched
> collector, not this degraded fallback.

> **Task 1 follow-ups (2026-09-03, controller):** three findings change later
> tasks; the spec is amended to match.
> 1. **Resolved markets are unverified.** `condition_ids` only reaches Gamma's
>    default (open) index. Task 4 opens with a re-probe: `markets?condition_ids=<c>&closed=true`
>    for a market from the 2026-08-09 poll, plus a `markets?closed=true&limit=5`
>    listing as the fallback query; record `closed`, `umaResolutionStatuses`
>    (plural; the singular does not exist), `outcomePrices` at resolution, and
>    any winner field as a fixture and dated rows. Task 4 uses only those
>    names. If no resolved market is reachable by any documented query, Task 4
>    stops and reports BLOCKED.
> 2. **The collector must not depend on `start` being honored** (probe 4
>    inconclusive) nor on a known `limit` cap (probe 2 inconclusive). Task 3
>    pages by `offset` in `whale.page_size` steps and stops when the oldest row
>    on a page is older than `last_seen − overlap_secs`; it sends `start`
>    anyway. Task 3 also re-probes `limit` and `start`/`end` once against a
>    high-volume market taken from `whale_trades_global.json` and records the
>    result.
> 3. **Backfill source is `/activity?user=&type=TRADE`**, not
>    `/closed-positions`: activity returns full trade rows (verified, probe 8),
>    so backfilled prints run through the same `positions_from_prints` and
>    hygiene filters as live prints, and resolve through Task 4's reader.
>    `closed-positions` is kept only as an `avgPrice` cross-check. Decision (b)
>    therefore becomes: **backfill enabled via activity, gated on follow-up 1**.
> 4. **Decision (a) is reversed: collector mode = `global`.** The gate "≥3,000
>    rows/min" was written assuming a 1,000-row cap, i.e. "can one call a
>    minute keep up". The fix pass showed `limit=10000` returns 10,000 rows
>    against a platform rate of ≈1,400–2,000 rows/min, so one global call per
>    `poll_secs` captures the whole taker tape with ~5× headroom, versus ≈80
>    batched per-market calls per cycle. Task 3 implements `global` as the
>    primary mode: one `GET /trades?takerOnly=<cfg>&limit=<page_size>` per
>    cycle, rows filtered client-side to the universe's condition ids, paging
>    by `offset` only when a page is full and its oldest row is still newer
>    than `last_seen − overlap_secs`. The per-market comma-CSV batched mode
>    (`market=<a>,<b>`, verified in probe 1 — the repeated-param requirement
>    applies to Gamma's `condition_ids`, not to `/trades`) stays as
>    `whale.collector_mode: batched` fallback. Task 3's step 0
>    adds one call to confirm the global feed is ordered newest-first and that
>    `offset` returns older rows.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. Design authority: `docs/superpowers/specs/2026-09-03-whale-consensus-lane-design.md` (the spec). Where this plan and the spec disagree, the spec wins and this plan gets a dated correction.

**Goal:** Measure, as a paper REVIEW lane, whether following the consensus of demonstrably skilled Polymarket wallets into pregame sports markets earns a positive settled return at the follower's post-print touch, with a signal-driven exit.

**Architecture:** A collector and scorer own `data/whale_tape.sqlite` (prints, wallets, backfill, resolutions, signals). A booking lane reads scores and signals, fetches live books, writes `quotes` rows, and books through `arb.tracker.process_poll` into `data/arb.sqlite`. Settlement is Polymarket-native for kind `WHALE`. Exits are append-only `settled` events with `source='exit:<reason>'`.

**Tech stack:** Python 3.13 (`.venv/bin/python`), SQLite, `Decimal` money math, pytest against recorded fixtures.

## Why this lane exists

| Question | V1 (June 2026) | L14 |
|---|---|---|
| Follower price | The whale's own fill (fiction) | Post-print best ask + fee, fetched at signal time |
| Whale qualification | ≥100 trades, win rate in [0.60, 0.72] | Lower credible bound on realized edge in points, hierarchical shrinkage, no trade floor |
| Buckets | Category concentration ≥50% | sport × market_type with cross-sport transfer, no sports↔nonsports transfer |
| Exit | None | Consensus flip / whales left / confidence drop, pregame, bid − fee |
| Settlement | Manual except MLB | Polymarket-native for every sport |
| Kill rule | None pre-registered | §12 of the spec, dated in PREREGISTRATION.md |

## Global constraints

Copied from CLAUDE.md; every task's requirements include these.

- **Rule 1** paper only; public unauthenticated GETs only. Nothing here places an order.
- **Rule 2** no Odds API credits. This lane never touches the Odds API.
- **Rule 3** no invented endpoints or fields. Task 1 verifies every endpoint and field in spec §4 and records dated rows; later tasks may only use what Task 1 verified.
- **Rule 4** every threshold in `config.yaml` (`whale:` block).
- **Rule 5** `Decimal` for prices, fees, stakes, P&L; floats only inside the shrinkage arithmetic, re-quantized to 6 places on output.
- **Rule 7** every book fetched at signal or exit time is persisted as `quotes` rows.
- **Rule 9** `flat_bets`/`bet_events` append-only. The collector never opens `data/arb.sqlite`. Exits are new `bet_events` rows.
- **Rule 12** `classify()` is the only eligibility predicate; the WHALE branch is added there and nowhere else.
- **Rule 13** the `whale` process runs only under the supervisor from a committed HEAD after a green `pytest`.
- Modules under ~300 lines. Test command: `.venv/bin/python -m pytest -q`.
- Workflow per task: failing test → minimal implementation → full `pytest -q` → `arb report --daily` unchanged except where the task says otherwise → commit with evidence.

## File structure

| File | Responsibility |
|---|---|
| `scripts/probe_whale_endpoints.py` (new) | Task 1 live probes; writes fixtures; prints findings. Never imported by `arb/`. |
| `arb/whale_tape.py` (new) | Collector: schema, universe, batched trade polls, leaderboard snapshot, per-wallet backfill. I/O only. |
| `arb/whale_resolve.py` (new) | Gamma resolution reader; `resolutions` table; `settle_auto_pm`. |
| `arb/whale_score.py` (new) | Pure: prints → positions; hygiene; hierarchical scores with `as_of`. |
| `arb/whale_signal.py` (new) | Pure: V1/V2/V3 points; fire rule; exit evaluation. |
| `arb/lanes/whale.py` (new) | The `whale --once` cycle; quotes rows; candidates; exits. |
| `arb/bench_whale.py` (new) | L14 `LaneResult`. |
| `arb/settlement.py`, `arb/models.py`, `arb/eligibility.py`, `arb/tracker.py`, `arb/invariants.py`, `arb/reconcile.py`, `arb/metrics.py`, `arb/schema.py`, `arb/migrations.py`, `arb/report.py`, `arb/dashboard.py`, `arb/regrade.py`, `arb/heartbeat.py`, `arb/cli.py`, `arb/commands.py`, `arb/config.py`, `config.yaml` (modify) | Wiring listed per task. |
| `tests/test_whale_tape.py`, `tests/test_whale_resolve.py`, `tests/test_whale_score.py`, `tests/test_whale_signal.py`, `tests/test_whale_lane.py`, `tests/test_whale_ledger.py` (new) | One failure mode per test. |
| `tests/fixtures/whale_*.json`, `tests/fixtures/whale_frozen_clock.sqlite` (new) | Recorded in Task 1 and Task 5. |

---

## Task 0 — Preconditions (operator, no code)

- [ ] Restart the instrument: `git status` clean, `.venv/bin/python -m pytest -q` green, then the supervisor per SPEC §16. Confirm `polls` gains fresh `watch` rows before Task 8 runs live.
- [ ] Decide the Terms-of-Use question in spec §3 and record the decision in `KNOWN_ISSUES.md`.
- [ ] Confirm `data/verified_facts.md` PM fee rate row is not stale (`fee_is_stale`); refresh if it is.

## Task 1 — Rule-3 verification probes and fixtures

**Files:** `scripts/probe_whale_endpoints.py` (new), `tests/fixtures/whale_trades_batch.json`, `whale_trades_global.json`, `whale_leaderboard.json`, `whale_closed_positions.json`, `whale_activity.json`, `whale_gamma_resolution.json` (new), `data/verified_facts.md` (modify).

Probes, each a single GET with a 20s timeout, one call per probe, logged with status and elapsed time:

1. `/trades?market=<c1>,<c2>,<c3>&limit=100` for three condition ids from the latest watch poll. Record: does CSV batching work; are rows from all three markets present.
2. `/trades?market=<c1>&limit=10000` then `limit=1001`. Record the real cap from the response length or error.
3. `/trades?market=<c1>&takerOnly=false&limit=200` vs `takerOnly=true`. Record row-count difference and whether any `transactionHash` repeats across the two, and within one call.
4. `/trades?market=<c1>&start=<t−3600>&end=<t>`. Record whether the filter is honored.
5. `/trades?limit=1000` with no `market`. Record whether a global feed exists, its cap, and the rows-per-minute rate implied by the timestamp span.
6. `/v1/leaderboard?category=SPORTS&timePeriod=MONTH&orderBy=PNL&limit=50&offset=0`. If `SPORTS` 400s, try the documented category list. Record fields.
7. `/closed-positions?user=<w>&limit=50&offset=0` for the largest `proxyWallet` seen in probe 1. Record fields, especially `avgPrice`, `outcome`, `outcomeIndex`, `conditionId`, `realizedPnl`, and any resolution timestamp. Then `offset=50` to confirm paging.
8. `/activity?user=<w>&type=TRADE&limit=100`. Record fields.
9. Gamma by condition id: try `markets?condition_ids=<c1>,<c2>` and `markets?conditionId=<c1>`; record which works and the batch cap. Record `closed`, `umaResolutionStatus`, `outcomePrices`, `gameStartTime`, `clobTokenIds`, `sportsMarketType`, `endDate`, and any winner field for one resolved market (pick a market whose `start_utc` in `quotes` is more than 2 days old).
10. Backfill-bias check: for the probe-7 wallet, count closed positions with `avgPrice > 0` and realized payout 0 (held to zero). Record whether losers-to-zero appear at all.

**Steps:**
- [ ] Write the script; every probe wrapped so one failure does not stop the others; output a markdown table to stdout.
- [ ] Run once: `.venv/bin/python scripts/probe_whale_endpoints.py`. Save each response verbatim as the named fixture (rule 2 fixtures are recorded responses).
- [ ] Append one dated row per finding to `data/verified_facts.md`, verdicts EXISTS / ABSENT / CAPPED-AT-n, evidence = fixture name.
- [ ] Decision gate recorded at the top of this plan as a dated note: (a) collector mode = `global` if probe 5 returns ≥ 3,000 rows/min at one call per minute, else `batched`; (b) backfill enabled iff probe 7 fields include entry price and outcome and probe 10 shows losers-to-zero; (c) leaderboard enabled iff probe 6 returns a sports category; (d) resolution fields named exactly as observed; (e) `whale.taker_only` set to the value the collector will send, with probe 3's row-count difference recorded so the discovery bias in spec §13 has a number.
- [ ] Commit: `L14 task 1: verified data-api/gamma facts for the whale lane (fixtures recorded)`.

**Acceptance:** `data/verified_facts.md` has ≥ 8 new dated rows; six fixtures exist; the decision note is written. If probe 1 fails (no batching) and probe 5 fails (no global feed), the plan's collector falls back to one call per market for markets starting within 6 hours and `whale.lookahead_hours` is set to 6; record this in the note.

## Task 2 — Config block, schema, invariants

**Files:** `config.yaml`, `arb/config.py`, `tests/test_config.py` (extend), `KNOWN_ISSUES.md`.

- [ ] Failing tests: `load()` on a config carrying `whale:` fails (unknown key) — proves the loader is strict; and `load()` on the literal spec §14 block, `V2: null` included, must succeed once the schema lands (today `NUM = (int, Decimal)` rejects `None` at config.py:198-199, and `load_config` runs before every subcommand at cli.py:124, so a bad block breaks every command).
- [ ] Add the `whale:` block exactly as spec §14, with an inline comment per key (rule 4). Add the schema in `arb/config.py` (`MAP`/`NUM`/`BOOL`/`STR`/nested) plus a new leaf type `NUM_OPT = (int, Decimal, type(None))` used only for `fire.min_points.V2` and `V3`. Add `_check_invariants` clauses: `fire.ratio > 0.5`, `exit.flip_ratio < fire.ratio`, `size.min_stake_usd <= size.max_stake_usd`, `poll_secs >= 30`. The "null only during calibration" rule is enforced by the lane cycle (Task 8), not the loader.
- [ ] Remove the dead `tape_check` and `exit` blocks and their schema entries; record both in `KNOWN_ISSUES.md` as "config with no implementation, removed 2026-09-03; intent carried by `whale.exit` and by Task 3's prints table".
- [ ] Tests: config loads; each invariant fails with a clear `ConfigError`; `tape_check` absent.
- [ ] Full `pytest -q`; `arb report --daily` unchanged.
- [ ] Commit: `L14 task 2: whale config block, strict schema, invariants; dead tape_check/exit blocks removed`.

## Task 3 — Collector (`arb/whale_tape.py`)

**Files:** `arb/whale_tape.py` (new), `tests/test_whale_tape.py` (new), `arb/cli.py`, `arb/commands.py` (subcommands `whale-tape --once`, `whale-backfill`).

**Interfaces:**

```python
def open_tape(path) -> sqlite3.Connection        # creates tables, WAL, busy_timeout
def universe(store_ro, cfg, now) -> list[Market]  # from latest watch poll's PM sports quotes
def poll_prints(cfg, con, markets, now, get=get_json) -> dict   # {calls, rows, dupes, status_counts}
def snapshot_leaderboard(cfg, con, now, get=get_json) -> int
def discover_wallets(cfg, con, now) -> int        # tape-sourced wallets over the notional threshold
def backfill(cfg, con, now, get=get_json) -> dict # per-wallet /activity?type=TRADE pages -> prints(source='activity'); bucket map via Gamma condition_ids
```

Tables in `data/whale_tape.sqlite`: `prints`, `markets`, `wallets`, `leaderboard_snapshots`, `wallet_history`, `resolutions`, `signals`, `base_rates`, `meta`. Columns per spec §5 and §6; every price/size/notional is TEXT holding a 6-place `Decimal` string. Unique index on prints `(tx_hash, proxy_wallet, asset, side, price, size, ts)`.

- [ ] **Step 0, rule 3:** three calls at most. (i) `GET /trades?takerOnly=true&limit=10000` with no `market`: record whether rows are ordered newest-first (timestamps non-increasing) and the timestamp span in minutes. (ii) `GET /trades?takerOnly=true&limit=100&offset=10000`: record whether it returns rows older than call (i)'s oldest. (iii) against the `conditionId` with the most rows in `tests/fixtures/whale_trades_global.json`: `GET /trades?market=<c>&takerOnly=true&start=<newest_ts−600>&end=<newest_ts>`, record whether rows outside the window appear. Append dated `data/verified_facts.md` rows and save (i) truncated to 200 rows as `tests/fixtures/whale_trades_global_ordered.json`. Whatever the result, the collector filters client-side, pages by `offset`, and stops on age (spec §5 step 2); `start` is never relied on.
- [ ] Failing tests, one each: schema creates all tables; `universe()` returns only PM sports markets inside the window and carries `condition_id`, tokens, sport, market_type, line, start; `poll_prints` in `global` mode makes one call with no `market`, keeps only rows whose `conditionId` is in the universe, persists them (`tests/fixtures/whale_trades_global.json` shaped) with `observed_at`, rejects duplicates on a second call, sends `takerOnly` from config, pages by `offset` only when a page has exactly `page_size` rows and its oldest row is newer than `last_seen − overlap_secs`, stops on a short page or on age, records a non-200 and returns a backoff flag; in `batched` mode it uses the comma-CSV `market=<a>,<b>,<c>` form (verified in Task 1 probe 1; `tests/fixtures/whale_trades_batch.json` shaped) honoring `markets_per_call`; `snapshot_leaderboard` persists `tests/fixtures/whale_leaderboard.json` rows with `source='leaderboard'`; `discover_wallets` adds a wallet only above `discovery_min_notional_usd` counting pregame BUYs only; `backfill` pulls `tests/fixtures/whale_activity.json`-shaped pages into `prints` with `source='activity'`, pages until a short page, stores `bucket=NULL` for unmappable markets, respects `wallets_per_cycle`, and is FIFO by `first_seen`.
- [ ] Implement. `get_json` is injected so tests never touch the network; the module opens the ledger through `arb.replay.open_ro` for `universe()` only, and `universe()` filters `quotes` by the latest `watch` `poll_id` (served by `idx_quotes_poll`), never by a `fetched_at` range, which would scan the 37 GB table. The collector sends `takerOnly=<whale.taker_only>` explicitly on every call. Backfill order is FIFO by `wallets.first_seen`. `open_tape` sets WAL and `busy_timeout` (two processes touch this file: `whale` and `settle`).
- [ ] Rate guard: a token bucket sized by `whale.max_calls_per_min`; on any non-200 write `meta.backoff_until`.
- [ ] Full `pytest -q`; commit: `L14 task 3: whale print collector, leaderboard snapshot, backfill (own SQLite file)`.

**Acceptance:** `.venv/bin/python -m arb whale-tape --once` against the live universe returns a row count and status table; `sqlite3 data/whale_tape.sqlite 'select count(*) from prints'` > 0.

## Task 4 — Resolution reader and Polymarket-native settlement

**Files:** `arb/whale_resolve.py` (new), `arb/settlement.py`, `arb/models.py`, `arb/tracker.py` (kind tuple), `arb/commands.py` (kind tuple, `cmd_settle`), `arb/invariants.py` (kind tuple), `arb/reconcile.py` (skip WHALE), `tests/test_whale_resolve.py` (new).

**Interfaces:**

```python
def read_resolutions(cfg, con, condition_ids, now, get=get_json) -> int   # persists resolutions rows
def resolution_for(con, condition_id) -> Resolution | None                # status resolved|void|open
def settle_auto_pm(cfg, store, con, now) -> int                           # settles open WHALE bets
```

- [ ] **Step 0, rule 3 (before any code):** Task 1 could not reach a resolved market. Probe, one call each, at most 4 calls: `GET https://gamma-api.polymarket.com/markets?condition_ids=<c>&closed=true` for a condition id from the 2026-08-09 watch poll (`data/arb.sqlite`, read-only, `poll_id = (SELECT MAX(id) FROM polls WHERE kind='watch')`), and `GET .../markets?closed=true&limit=5` as the fallback listing. Save the resolved-market response verbatim as `tests/fixtures/whale_gamma_resolved.json`; append dated `data/verified_facts.md` rows for: reachability of resolved markets by `condition_ids&closed=true`, the resolved values of `closed`, `umaResolutionStatuses`, `outcomePrices` (exact strings), and any winner field. If neither query reaches a resolved market, stop and report BLOCKED — do not guess a field. Every later step uses only these names.
- [ ] `arb/models.py`: `KINDS += ("WHALE",)`, `RESOLUTION_SETTLED_KINDS += ("WHALE",)`. `arb/settlement.py`: `SETTLEMENT_PATHS["WHALE"] = "resolution"`; `open_resolution_bets` uses `RESOLUTION_SETTLED_KINDS` instead of its literal tuple; `settle_auto_mlb` skips `kind == 'WHALE'`; the tripwire skips `source` beginning `exit:`. The three other hand-typed `('EV','NEGRISK','BTC')` tuples — `tracker._active_bets` (tracker.py:85-87), `commands.cmd_status` (commands.py:521), `invariants` check 3 (invariants.py:49) — import `RESOLUTION_SETTLED_KINDS`. `reconcile()` (reconcile.py:99-102) excludes kind WHALE. `cmd_settle` calls `settle_auto_pm` after `settle_auto_mlb` inside a `try/except Exception` that logs and continues, so `reconcile()` and `check()` always run; `settle_auto_pm` itself catches `sqlite3.OperationalError` from the tape file, logs, and returns.
- [ ] Failing tests: a resolved fixture yields `winner_outcome_index`; a 0.5/0.5 `outcomePrices` fixture yields `void`; an open market yields `open` and no row; a closed market without a winner past `resolve_giveup_days` writes one annotation and no settlement; `settle_auto_pm` appends exactly one `settled` event for a WHALE bet with the right `pnl` (WON, LOST, VOID cases), using `settle_bet`; an EV bet is untouched; a WHALE bet settled 30 s after entry with `source='exit:flip'` is not tripwired, the same bet with `source='pm-gamma:x'` is; a settled WHALE bet is not returned by `tracker._active_bets` for `poll_kind='whale'`, so a re-fired signal on the same market books again; `invariants` check 3 treats WHALE as resolution-settled; `reconcile()` over a fixture with a settled and an exited WHALE bet performs no Gamma read and writes no `pair_demotions`; a `settle_auto_pm` that raises leaves `cmd_settle` running `check()`.
- [ ] Implement, using only the field names Task 1 verified.
- [ ] Full `pytest -q`; `arb report --daily` unchanged (no WHALE rows yet). Commit: `L14 task 4: Polymarket-native resolution reader and settle_auto_pm; WHALE kind registered`.

## Task 5 — Pure scoring (`arb/whale_score.py`)

**Files:** `arb/whale_score.py` (new), `tests/test_whale_score.py` (new), `tests/fixtures/whale_frozen_clock.sqlite` (new, built by a test helper from JSON, not hand-edited).

**Interfaces:**

```python
@dataclass(frozen=True) class Position: wallet, condition_id, outcome_index, avg_price, size, notional_usd, entry_ts, last_ts, pregame, sport, market_type, root, source
def positions_from_prints(prints, markets, cfg) -> list[Position]           # BUY windows; SELL reduces FIFO
def hygiene_flags(prints, positions, cfg) -> dict[str, set[str]]           # wallet -> flags
def base_rate(outcomes, root, as_of, cfg) -> tuple[Decimal, int]
def scores(positions, resolutions, as_of, cfg, base_rates) -> dict[(wallet, root, sport, bucket), Score]
# Score: n_bucket, n_sport, n_root, mu, sd, lb, lb_report (all Decimal q6)
```

- [ ] Failing tests, one each: two BUY prints 100 s apart form one position with the size-weighted price; a third print 400 s later forms a second position; a SELL reduces size FIFO and never forms a position; a print after `start_utc` marks the position `pregame=False`; VOID resolutions are excluded from n; `base_rate` returns the default below `base_rate_min_n`; the spec §6 worked table is reproduced to 3 decimals for n = 5, 15, 40, 120, 400; at `n = 0` a level's variance equals `prior_sd² + var_parent` and at `n = 10⁶` equals `obs_var/n` to 1e-9; the transfer example (200 MLB at +0.08, 3 NBA at 0) gives NBA lb80 = +0.006 ± 0.001 and −0.067 ± 0.001 without history, and the `Score` carries `n_bucket = 3` and `native = False`; a nonsports position never changes a sports score (root isolation); `hygiene_flags` flags the `mm`, `both_sides`, `wash` fixtures and not a clean one; prints with `source='activity'` (backfill) build positions and are hygiene-filtered through exactly the same calls as live prints, and `Position.source` carries the value through so the report can split them; **frozen clock**: `scores(as_of=T0)` equals `scores` over a copy with rows `resolved_at >= T0` deleted, bit-for-bit on every `Decimal` field.
- [ ] Structural test: every SQL or group-by in `whale_score.py` that aggregates positions includes `root` (grep-test, same pattern as `tests/test_hygiene.py`).
- [ ] Implement. Floats allowed inside the normal-normal arithmetic, outputs quantized `q6`.
- [ ] Full `pytest -q`; commit: `L14 task 5: pure whale scoring — positions, hygiene, hierarchical lower bound, frozen-clock proof`.

## Task 6 — Pure signalling and exit evaluation (`arb/whale_signal.py`)

**Files:** `arb/whale_signal.py` (new), `tests/test_whale_signal.py` (new).

**Interfaces:**

```python
def points(variant, whales_on_side, cfg) -> Decimal                     # V1/V2/V3
def evaluate(market, open_positions, score_lookup, touch, now, cfg) -> list[Signal]  # one per variant
# Signal: variant, side, points_yes, points_no, ratio, whale_ids, vwap, fired, suppressed_reason
def exit_reason(bet, open_positions_now, score_lookup, cfg) -> str | None   # flip | left | confidence | None
def conf_stake(S, cfg) -> Decimal
```

- [ ] Failing tests, one each: V1 counts one point per qualifying whale; V2 uses `score_lb − min_score_lb`; V3 applies the log size weight; two whales 3 s apart count once; ratio below 0.80 suppresses with reason `ratio`; points below `min_points.V1` suppress with `min-points`; ask more than 0.03 above VWAP suppresses with `stale`; less than 600 s to start suppresses with `too-close`; a print older than `max_print_age_secs` suppresses with `late-print`; V2 and V3 with `min_points` null evaluate but never fire, reason `uncalibrated`; a `Signal` records each whale's `n_bucket` and `native` flag and the `flagged_edge = S − m0`; `calibrate_min_points(signals_rows, cfg)` reproduces the spec §7 formula on a fixture (3 × the 10th percentile of qualifiers' `score_lb − min_score_lb`) and returns `None` below `calibration_min_whales`; `exit_reason` returns `flip` when the ratio falls below 0.50, `left` when signal whales sold ≥ 50%, `confidence` when the weighted lb drops below −0.01 together with the whale, level, and resolved position that moved it, `None` otherwise, and `None` post-start regardless; `conf_stake` is 0.10 at S = min_score_lb, 1.00 at S = min + 0.10, clipped both sides (formula: `min_stake + clip((S − min)/cap, 0, 1) × (max_stake − min_stake)`).
- [ ] Implement.
- [ ] Full `pytest -q`; commit: `L14 task 6: pure signal aggregation (V1/V2/V3), fire rule, exit rule, confidence stake`.

## Task 7 — Ledger changes

**Files:** `arb/schema.py`, `arb/migrations.py`, `arb/tracker.py`, `arb/eligibility.py`, `arb/metrics.py`, `tests/test_whale_ledger.py` (new), `tests/test_hygiene.py` (unchanged, must stay green).

- [ ] Failing tests: migration 7 (`SCHEMA_VERSION = 7`) adds `variant` and `signal_id` to `flat_bets` and is idempotent on a DB that already has them; `_book` writes `stake_usd` from `cand["stake_usd"]` and `Decimal(1)` when absent, and writes `variant`/`signal_id`; `classify({"kind": "WHALE", ...})` returns `(False, "whale-signal", "review")` before any other check (a WHALE candidate with `depth_ok=False` still reports `whale-signal`); `metrics.bet_pnl_optimistic` returns `payload.pnl` for a `settled` event whose `source` begins `exit:` — this needs a new early branch before the existing WON/LOST/VOID guard at metrics.py:65-66, which would otherwise return `None`; `metrics.bet_confirmed` returns `(True, pnl)` for an EXITED bet and its fallthrough comment at metrics.py:114 names EXITED; the rule-9 grep test passes over the tree.
- [ ] Implement; the `_book` change is one line: `"stake_usd": cand.get("stake_usd", Decimal(1))`.
- [ ] `arb report --daily` output on the existing DB must be byte-identical before and after (no WHALE rows exist yet); attach the diff to the commit message.
- [ ] Commit: `L14 task 7: variable stake, variant/signal_id columns, WHALE eligibility branch, exit pnl in metrics`.

## Task 8 — The lane (`arb/lanes/whale.py`) and supervisor wiring

**Files:** `arb/lanes/whale.py` (new), `arb/heartbeat.py`, `arb/cli.py`, `arb/commands.py`, `config.yaml` (`supervisor.processes.whale: true`, `commands.whale: "whale --once"`, `max_child_runtime_secs.whale: 120`), `tests/test_whale_lane.py` (new).

**Interface:** `run_whale_cycle(cfg, store, con, now, get=get_json, post=post_json) -> dict` returning counts: prints, wallets, signals_evaluated, fired, booked, exited, quotes_rows, status.

Cycle order as spec §5 steps 1–9. Book quotes rows via a `book_quote_rows`-style helper (pattern: `arb/lanes/political.py`) for both tokens of every market touched. Candidates follow `candidate_from_opp`'s dict shape (`process_poll` accepts plain dicts; no `Opportunity` is built) with `kind='WHALE'`, `category='sports'`, `venue_pair='polymarket:self'`, `event_key = 'pm:' + event_slug` always, `position_key` via `position_key("WHALE", sport, event_key, market_type, line, outcome=..., side=...)` (the existing generic branch; no new branch), `fingerprint = sha1(base|variant|sizing)`, `stake_usd`, `variant`, `signal_id`, `edge_points = flagged_edge`, `legs=[one PM leg]`. Calibration guard: when `min_points.<variant>` is null and the tape's `signals` span exceeds `fire.calibration_days` (outage days excluded), log CRITICAL and book nothing for that variant. Exits: `fetch_books` → bid → `pnl = (bid − fee_exit − cost − fee_cost) × contracts` quantized → `store.insert("bet_events", settled EXITED with reason, and for `confidence` the whale/level/position that moved the score)`; no bid → annotation `exit-no-bid`.

- [ ] Failing tests against fixtures (prints + books + a pre-scored wallet set): one cycle books exactly two `flat_bets` rows per fired variant with distinct fingerprints, the same `signal_id`, `stake_usd` 1.00 and the conf stake, `lane='review'`, `poll_kind='whale'`; `quotes` rows exist for both tokens with `venue_market_id = condition_id:token`; the `signals` table has one row per variant with `fired`/`suppressed_reason`; a second cycle with unchanged prints books nothing (fingerprint dedupe); an exit cycle with a flipped consensus appends one `settled` event with `result='EXITED'`, `source='exit:flip'`, and the right `pnl`; a book with no bids appends the `exit-no-bid` annotation and no event; post-start no exit is evaluated; a `whale.enabled: false` config books nothing and records nothing.
- [ ] Heartbeat: `_base_lane_interval` branch for `whale` → `whale.poll_secs`; `POLL_KIND["whale"] = "whale"`; supervisor entries. Test: stale-lane check recognises `whale`.
- [ ] Implement; keep `arb/lanes/whale.py` under 300 lines by delegating to Tasks 3–6.
- [ ] Full `pytest -q`; live acceptance under rule 13: commit, then `.venv/bin/python -m arb whale --once` from a clean HEAD; paste the counts dict into the commit message of the follow-up.
- [ ] Commit: `L14 task 8: whale lane cycle — quotes rows, dual-stake booking, signal-driven exits; supervisor wiring`.

## Task 9 — Report and bench

**Files:** `arb/report.py`, `arb/bench_whale.py` (new), `arb/bench_lanes.py`, `arb/dashboard.py` (counts only), `tests/test_whale_lane.py` (extend), `tests/test_bench.py` (extend).

- [ ] Failing tests: `daily_frame` groups by `(kind, lane, variant)` and existing rows (variant NULL) group as before; `report.write_daily`, `report.print_daily`, and `dashboard.state()` (the three sites that unpack the key today: report.py:217, report.py:263, dashboard.py:169-170) all run on a DB holding one WHALE row and one legacy row; the `whale` section renders on an empty tape DB without error; exit-vs-hold counterfactual P&L is computed for an exited bet whose market later resolved; `_bootstrap_mean_ci(by_event, percentiles=(0.10, 0.90))` returns the 10th/90th percentiles and its existing callers still get (0.025, 0.975) by default; the whale section splits signals and ROI by `native` vs `transfer` whales; `L14` is in `LANES` and its `LaneResult.primary` is V2/flat mean ROI with the `event_key` cluster CI at the one-sided percentiles spec §12 names; verdict is `UNDERPOWERED` below `kill.decision_n`.
- [ ] Implement the section fields listed in spec §12; cluster bootstrap reuses `arb/regrade._bootstrap_mean_ci` parameterised by percentile pair, `event_key` as the cluster key.
- [ ] `arb report --daily` on the existing DB: identical except a `variant` column (empty for existing rows) and the new empty `whale` section; attach the diff. Task 7's byte-identical check stays valid because Task 7 does not touch `by_class`.
- [ ] Commit: `L14 task 9: whale report section and L14 bench lane`.

## Task 10 — Documentation and pre-registration (before the first supervised poll)

**Files:** `STRATEGIES.md`, `PREREGISTRATION.md`, `SPEC.md`, `KNOWN_ISSUES.md`, `data/verified_facts.md`.

- [ ] `STRATEGIES.md` §3: `### L14 (new 2026-09-03). Whale consensus — the V1 debunk re-tested` citing L11's scrap reasons and each correction (post-print touch, shrinkage lower bound, exits, PM-native settlement).
- [ ] `PREREGISTRATION.md` amendment row: hypothesis; primary metric (V2/flat mean settled ROI per $ at follower touch, `event_slug`-clustered 90% CI); secondary metrics; variants and their status; sizing rows; exit policy and the hold counterfactual; kill rules 1–4 and the success rule with the replication month; the `min_points.V2/V3` calibration procedure (first 14 days of `signals`, then a dated amendment); the statement that the lane is REVIEW forever and that a promotion is a report.
- [ ] `SPEC.md` §5: pointer from the "deliberately absent … whale tracker" sentence to L14.
- [ ] Add the ToS decision (Task 0) to `KNOWN_ISSUES.md`.
- [ ] Commit: `L14 task 10: STRATEGIES L14 entry, PREREGISTRATION amendment, SPEC pointer`.

## Task 11 — First run and calibration

- [ ] Enable `supervisor.processes.whale: true` in a commit of its own; restart the supervisor.
- [ ] Day 1: confirm `prints` grows, `wallets` fills, backfill progresses at `wallets_per_cycle`; watch status codes; if any 429 appears, halve `max_calls_per_min` by amendment.
- [ ] Day 7: write `reports/whale-week1-YYYY-MM-DD.md` with G, `f_print`, `f_fire` (V1 only fires until calibration), qualifying-whale counts by sport, backfill coverage, and the projected days to n = 100.
- [ ] Day 14 (outage days excluded): run `arb whale-score --calibrate`, which applies spec §7's formula (`3 × q10` of qualifiers' `score_lb − min_score_lb`, V3 scaled by the median print size) and prints the two values or `None` if fewer than `calibration_min_whales` qualified (then the window extends 7 days and the report says so daily); write the values as a dated amendment in `PREREGISTRATION.md` and `config.yaml`; V2/V3 begin firing from the next commit.
- [ ] Every daily report thereafter: the `whale` section is committed with the rest of `reports/daily`.

## Self-review

- **Spec coverage.** §4 → Task 1; §5 → Tasks 3, 8; §6 → Task 5; §7 → Task 6; §8 → Tasks 7, 8; §9 → Tasks 6, 8, 4 (tripwire); §10 → Task 4; §11 → Task 7; §12 → Task 9, 10; §13 → Task 5; §14 → Task 2; §15 → Task 10.
- **Placeholders.** `min_points.V2/V3` are deliberately null until Task 11's calibration; the loader warns for 14 days, then errors. No other TBDs.
- **Type consistency.** `Position`, `Score`, `Signal` are frozen dataclasses defined once (Tasks 5, 6) and imported by Task 8; every money field is `Decimal` quantized `q6` at the store boundary.
- **Rule 9.** No task writes `UPDATE`/`DELETE` against the ledger; the collector never opens `data/arb.sqlite` for writing; exits are inserts.
- **Scope.** One lane, one new SQLite file, one migration, two pure modules, one loop. Nonsports, Kalshi, Kelly, §10.8 policies, and the global feed (unless verified) are out.

## Result

(appended after Task 11 with measured numbers)
