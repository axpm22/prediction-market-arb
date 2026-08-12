# Whole-model issues sweep — errors, slowness, profit leaks (June 2026)

A deliberately exhaustive pass over the *entire* model, not just arbs. Arb-specific issues
live in `ARB_WEAKNESSES.md` and `ARB_LADDER_AND_FRICTIONS_SPEC.md`; EV estimation issues live
in `EV_WEAKNESSES.md`. This doc catalogs everything else and the cross-cutting infrastructure,
tagged:

- **[ERROR]** — produces a wrong number or a wrong decision.
- **[SLOW]** — performance / scaling; wastes time or will degrade as data grows.
- **[LEAK]** — money left on the table, double-counted, or capital/measurement lost.
- **[ARGUABLE]** — I'm not sure it's a bug; I argue both sides and give a verdict.

Measured against the live `data/arb.sqlite` (2,002,084 quotes; 8,843 flat_bets; 3,904 polls;
1.4 GB + a 1.1 GB `.bak`). Where a claim rests on a number, the query is shown.

---

## Performance / slowness

### S1 [SLOW] — `record_tracker_poll` full-scans the entire `flat_bets` table every poll

`_active_rows` runs `SELECT * FROM flat_bets ORDER BY id` and rebuilds the per-fingerprint
lifecycle in Python on **every poll** (`tracker.py:102`, via `lifecycle.active_rows_by_fingerprint`).
That's O(all bets) per poll and O(polls × bets) cumulatively. With 8,843 bets × 3,904 polls the
cost is already meaningful and grows without bound — the scan loop gets slower the longer the
system runs, which silently lowers scan frequency and *that* costs arbs.

Same antipattern in `settlement_health._active_sports_bets` and `exposure.compute_exposure`
(both load wide row sets and reduce in Python).

**Fix:** query only candidate-active fingerprints (latest lifecycle row not terminal) via an
indexed subquery, or maintain a small `current_state` table updated on each transition. The
indexes exist (`idx_flatbets_fingerprint`, `idx_flatbets_kind_status`); the code just doesn't
use them to narrow the scan.

### S2 [SLOW/LEAK] — `quotes` grows unbounded (2.0 M rows / 1.4 GB in 13 days), no retention or rollup

`save_poll_quotes` appends every quote forever (SPEC §6 wants the backtest data — legitimate),
but there is no rollup, partition, or cold-store. ~154 k rows/day → ~5.6 M/yr, plus the 1.1 GB
`.bak`. Effects: every non-indexed read and `summary()`'s `COUNT(*)` slow over time; the WAL
checkpoint and the daemon's writes contend with analytics readers (each `connect()` sets
`busy_timeout=30000`, so a reader can stall up to 30 s behind a writer on a big file).

**Fix:** roll closed-poll quotes into a compact monthly table (or Parquet cold-store) and keep
the live `quotes` table to a recent window; `VACUUM` after pruning; drop the redundant
`max_size_usd` column (it's written equal to `depth_usd` — `store.py:200-201`).

### S3 [SLOW] — a fresh SQLite connection (and PRAGMAs) per helper call; `init_db()` on every `Store()`

`store.connect()` opens a new connection and re-issues `PRAGMA journal_mode=WAL` + `busy_timeout`
on **every** call (`store.py:29-39`), and many helpers call `connect()` several times per poll.
`Store.__init__` runs `init_db()` — the full schema script + `_migrate_existing_tables` (PRAGMA
table_info on every migrated table) + index creation — on **every CLI invocation**
(`store.py:27,41`). WAL mode is persistent; re-setting it each connect is wasted work, and
re-running migrations on every command is avoidable.

**Fix:** set PRAGMAs once per connection lifetime; reuse one connection within a poll (the
`transaction()` helper already exists — thread it through the tracker/settlement helpers);
gate migrations behind a schema-version check.

### S4 [SLOW] — `load_aliases()` re-reads and re-parses `data/aliases.json` from disk with no cache

`matching.normalize_name` falls back to `load_aliases()` whenever `aliases` is None
(`matching.py:36-40`), and `load_aliases` does a fresh `read_text()` + `json.loads` every call
(`matching.py:24`). Adapters pass a once-loaded dict (good), but any code path that calls
`normalize_name`/`event_key` without threading the dict re-loads the file. It's a static file.

**Fix:** `functools.lru_cache` on `load_aliases` (invalidate on mtime if you hot-edit it).

### S5 [SLOW] — analytics reduce in Python instead of SQL

The capacity report, daily report, and several breakdowns pull wide row sets and compute
medians/sums in Python (`arb_capacity.py:34`, `tracker.py:_bet_records_for_date`). Fine at 8 k
rows, linear-growth risk as the ledger grows. **Fix:** push aggregates into SQL where the index
already supports it; keep Python only for the Decimal-exact money lines.

---

## Correctness errors

### E1 [ERROR] — open-bet counting double-counts re-observations (982 vs 15)

Counting `status='OPEN'` rows directly reports **982** open EV/ARB "bets"; counting *distinct
unsettled bases* (no terminal child) reports **15**. The 982 is inflated ~65× by re-observation
OPEN rows across polls. Any code or report that counts `status='OPEN'` as "open positions"
(e.g. naive dashboards, `summary()` adjacent logic) overstates exposure and pending count
massively. This is the same missing position-key/dedup discipline flagged as ARB C2, but it
bites every per-status count model-wide.

```sql
-- inflated:  SELECT count(*) FROM flat_bets WHERE kind IN('EV','ARB') AND status='OPEN';      -- 982
-- correct:   ... AND NOT EXISTS (terminal child)                                              -- 15
```

**Fix:** define "open" once as "latest lifecycle row is OPEN/CONFIRMED" (the `lifecycle.py`
helper already does this) and route *every* count through it; never `COUNT(status='OPEN')`.

### E2 [ERROR] — integer-line totals priced as clean binaries (push overstates EV)

`_annotate_quality_and_kelly` flags `push-risk-integer-total` but still books EV at the full
`(p−c)/c` (`cli.py` push-risk block). An integer total can PUSH (stake returned), which lowers
true EV, so every integer-line total over-states edge and (if it clears the gate) over-stakes.

**Fix:** price the push explicitly — subtract `P(total==line)` mass (needs a simple
score-distribution model per sport) or exclude integer lines from actionable EV, not just flag
them.

### E3 [ERROR] — EV holding-cost charged to event start, not settlement

The EV `uneconomic-horizon` gate and capital charge use `opp.event.start_utc`
(`cli.py` `_annotate_quality_and_kelly`), but an EV bet ties up the $1 until **settlement**,
which is after the event ends (sometimes days). Holding cost is understated → the gate passes
some bets that lose to cash. (Mirror of ARB C4; same fix — charge to estimated settlement.)

### E4 [ERROR] — partial-coverage consensus can overwrite good consensus on an aborted scan

On an Odds-API credit/4xx abort, `run_scan` breaks the sport loop but still computes consensus
from the **partial** quote set and (unless `--dry-run`) persists it (`cli.py:368-411`).
`save_consensus` uses `ON CONFLICT … DO UPDATE`, so a partial-book consensus **overwrites** the
prior complete one for those events. EV then prices against a thinner, more biased `p̂`.

**Fix:** on abort, skip the consensus write (or write only events whose full book was fetched
this poll); never overwrite a complete consensus row with a partial one.

### E5 [ERROR] — `start_utc is None` is treated as pregame and stays actionable

`split_pregame` puts any quote with `start_utc is None` into the **pregame** (actionable) lane
(`cli.py:468`). Combined with adapters that can emit a null/!wrong start (ARB D1: Kalshi falls
back to `close_time`), a start-unknown quote can become an actionable bet that no time guard can
protect. **Fix:** treat `start_utc is None` as non-actionable (research lane), never pregame.

### E6 [ARGUABLE→ERROR] — consensus history is destroyed (single row per outcome, upserted)

`consensus` is keyed `(event_key, outcome)` and upserted each scan (`store.py:142-156`), so only
the **latest** `p̂` survives; the line's pregame trajectory is gone.

*Argument it's fine:* SPEC §6 persists every raw quote, so any historical consensus can be
*recomputed* by re-devigging the stored books per poll. Storage saved.

*Argument it's an error:* the system's central proof-of-edge instrument is CLV — closing-line
value needs the consensus/мid **at multiple times**, and recomputing it requires replaying
devig over 2 M quotes, which nothing does. In practice the trajectory is unavailable, so CLV
and "did the line move toward us" are measured weakly (see EV_WEAKNESSES #9). For a model whose
entire EV thesis is "we beat the closing line," throwing away the line's history is a real
analytical loss.

**Verdict:** error of omission. Keep a lightweight `consensus_history(event_key, outcome,
p_hat, computed_at)` append table (cheap: 620 live consensus rows, not 2 M) so CLV is direct.

### E7 [ERROR] — dead/duplicated accounting paths (`positions`/`ledger` vs `flat_bets`)

`positions` and `ledger` have **0 rows**; `auto_fill: false` means `fill_opportunity` never
runs, so the bankroll-debit ledger is dead, while `exposure.py` derives committed capital from
`flat_bets.stake_usd` instead. Two parallel accounting models, one inert. Not wrong today, but a
landmine: turning `auto_fill` on would start a *second*, divergent ledger that no report
reconciles against `flat_bets`. **Fix:** pick one source of truth (the append-only `flat_bets`)
and either delete the `positions/ledger` path or make it a pure view over `flat_bets`.

---

## Profit leaks

### L1 [LEAK] — arb actionable P&L is ≈ 0 by configuration (whole class is REVIEW)

`trusted_pairs: []` and every cross-venue pair un-whitelisted → 145 of 148 distinct arb
fingerprints are REVIEW, and `auto_fill: false`. So the arb pipeline currently produces
**measurement only**; the "clean" arb P&L rests on **2** trusted single-venue fingerprints. If
the intent is to actually capture cross-venue arbs, the gating (correctly conservative) plus an
empty whitelist means none ever graduate. **Decide:** which venue-pairs you trust enough to
whitelist (after the A2 reconciler proves their locks hold), or accept arbs are research-only
and stop sizing/Бudgeting them as if live.

### L2 [LEAK] — settlement is structurally unhandled for non-MLB sports

Only MLB has a no-window resolver (`settle_backfill_mlb` via MLB Stats API). The Odds-API path
is capped at `settle_days_from: 3`. Today no bets are orphaned (the 15 open unsettled bases are
all MLB and recent), so it isn't biting — **but** any NBA/soccer/tennis/MMA bet not settled
within 3 days has **no** backfill path and would become a permanent OPEN (lost realized P&L +
stuck "capital" in the measurement).

*Argument it's not urgent:* current open book is MLB-only, so the gap is latent.
*Argument it is:* the moment EV/arb fires on another sport (the scan covers 5), it's
unsettleable past 3 days. **Fix:** add a per-sport final-score backfill resolver (or a manual
settle prompt) before widening sport coverage; `settle-health` should alarm on any non-MLB open
bet older than the window, not just count overdue days.

### L3 [LEAK] — taker-only pricing; maker rebates / spread capture never modeled

Every leg is priced as a **taker** (ask + fee + buffer). SX is taker-0/maker-0 so neutral, but
Polymarket sports taker is 0.03 and Kalshi's is the quadratic curve — both avoidable by resting
**maker** orders, which also *earn* the spread instead of paying it.

*Argument against caring:* this is a paper model; maker fills aren't guaranteed and modeling
queue priority is hard. *Argument for:* a large fraction of "no-edge after fees" quotes flip to
+EV as a maker, and the neg-risk/arb classes especially are fee-sensitive at sub-4% edges. An
entire profit avenue is invisible to the model. **Verdict:** worth a measurement-only "maker
shadow" lane: re-price the same opportunities at the maker side/fee and log the counterfactual
edge; promote to live only if fills prove out.

### L4 [LEAK] — neg-risk coverage is page-limited

`negrisk_scan_pages: 3`, `negrisk_scan_limit: 200` (config) cap how much of Polymarket's
neg-risk universe is scanned per run. Baskets beyond the first pages are never seen — and
neg-risk is your *safest* arb class (single venue, single oracle). **Fix:** paginate to
exhaustion for neg-risk (it's cheap, public, and the highest-quality edge), or justify the cap.

### L5 [ARGUABLE] — the depth floor excludes thin books from ARB entirely

`split_depth_lanes` sends any quote with known depth < `min_executable_usd` (50) to the micro
(EV-only) or too-thin lane; ARB/divergence only run on the normal lane (`cli.py:475-492`).

*Argument it's a leak:* small real arbs on thin books are discarded.
*Argument it's correct:* a cross-venue arb needs *both* legs filled; a thin leg creates
partial-fill leg risk that can turn a "lock" into a naked position — exactly the failure the
whole REVIEW discipline guards against. **Verdict:** not a bug; it's the right conservative
default. Revisit only with real ladders (then size to the thin leg's actual depth instead of
excluding wholesale).

### L6 [LEAK] — `left_on_table` edge is reported but never recovered

Flags killed by liquidity/depth/horizon filters print a summed edge (`cli.py:317-319`) but are
neither persisted nor revisited. Some are genuinely uncapturable; some (horizon-killed) become
capturable as the event nears. **Fix:** persist `left_on_table` to a table and re-evaluate
horizon-killed flags on later polls, so a bet filtered for "too far out" isn't lost forever when
it enters the window.

---

## Cross-cutting / outside-the-box

### X1 [ERROR-risk] — every guard depends on the host wall clock; no drift check

Consensus age, leg-skew, started/final, and horizon gates all compare to
`datetime.now(timezone.utc)` scattered across modules. If the daemon host's clock drifts
(VM sleep, NTP failure), pregame events read as in-play (or vice versa), staleness windows
mis-fire, and arbs settle/skip wrongly. **Fix:** sample `now` once per poll and pass it down
(several functions already accept a `now` param — use it everywhere); optionally assert clock
sanity against a venue response `Date` header.

### X2 [LEAK landmine] — the fee-staleness gate silently quarantines the whole headline

`_warn_stale_fees` marks a venue stale after `fee_stale_days: 30`, and
`_annotate_quality_and_kelly` then stamps every bet touching that venue `stale-fee` → dropped
from clean/headline. Kalshi and SX `confirmed_on` are **2026-06-10** (13 days old today). In
~17 days they cross 30 days and, unless someone re-confirms the dates, **every** Kalshi/SX EV
silently leaves the headline. A maintenance lapse looks like "edge disappeared."

*Argument it's correct:* a stale fee genuinely biases edges; quarantining is safe.
*Argument it's a trap:* it fails *closed and silent* on a calendar trigger unrelated to whether
the fee actually changed. **Verdict:** keep the gate, but make it loud (settle-health/daemon
banner: "N venues will go stale-fee in M days") and allow a `confirmed_on` bump without a code
change. Don't let a date in a YAML file zero your P&L unannounced.

### X3 [SLOW/cruft] — dead `scans` table and always-NULL `scan_id`

`scans` has 1 row and `quotes.scan_id`/`opportunities.scan_id` are always written NULL (polls
replaced scans). Harmless but confusing; the unique index on `quotes` includes columns but not
`scan_id`. **Fix:** drop the `scans` table and the dead columns in a migration.

### X4 [ARGUABLE] — `arb_stale_warn: 0.04` may quarantine real fat arbs

Any arb with ROI > 4% is tagged `likely-stale-quote` → REVIEW. *Argument:* >4% cross-venue
"arbs" are almost always one stale leg, so this is good hygiene. *Argument against:* genuine
dislocations (injury news, suspended market reopening) can briefly exceed 4% and would be
quarantined. **Verdict:** correct default; just make sure these land in a reviewable lane (they
do) and spot-check a few to confirm none were real. Not a bug.

---

## Priorities (model-wide, complementing the arb docs)

1. **E4** (partial-consensus overwrite) and **E5** (null-start actionable) — silent
   wrong-number bugs in the EV path that are cheap to fix.
2. **S1** (per-poll full table scan) — the one that slowly throttles scan frequency and thus
   real capture; fix before the ledger grows further.
3. **E1** (open-count dedup) and **E7** (one accounting source) — make "open" and "committed"
   mean one thing everywhere.
4. **X2** (fee-stale landmine) and **X1** (clock) — operational traps that can zero or corrupt
   the headline without any code change.
5. **L2** (non-MLB settlement) before widening sport coverage; **E6** (consensus history) to
   make CLV real; **L4** (neg-risk pagination) for the safest incremental edge.
6. **S2/S3/S4/S5** — storage retention and connection/cache hygiene as the system ages.
7. **L1/L3** — strategic: decide whether arbs are live (whitelist + reconcile) and whether to
   measure a maker-side shadow lane.
```
