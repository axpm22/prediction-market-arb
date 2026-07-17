# Claude Code prompt — HARDENING pass (logic audit fixes, v1, 2026-07-08)

You are Claude Code working in the ArbitrageModel repo. Read `CLAUDE.md` and
SPEC.md §4 before touching anything; both bind every phase below verbatim.
This prompt fixes defects found in a full logic audit against the live DB
(2026-07-08, ~209 watch polls). Every finding below carries file:line evidence
— reproduce it yourself before writing the failing test (workflow §16).

**Precondition (rule 13):** start from a committed HEAD with green `pytest -q`.
If the tree is dirty, commit or stash that work first; never mix it into these
phases. One commit per phase, evidence in the message.

**Global constraints, restated because they bind here:**
- Tests run on recorded fixtures only; no live HTTP in pytest (rule 2).
- No `UPDATE`/`DELETE` on `flat_bets` or `bet_events`, ever (rule 9). Every
  fix below is expressible as new reads, new append-only rows, or new tables.
- `arb/eligibility.py` stays the only `def classify(` (rule 12); nothing below
  modifies it.
- Money math `Decimal`, q6 at boundaries only (rule 5). No magic numbers —
  every new threshold goes in `config.yaml` (rule 4).
- After each phase: full `pytest -q` green, and `arb report --daily` unchanged
  except where the phase explicitly says otherwise.

Phases are ordered by severity: latent crashes and wrong-money first, then
product correctness, then latency, then ops. Implement strictly in order.

---

## Phase H1 — `bet_confirmed` returns a nested tuple (latent report crash)

`arb/metrics.py:73`:

```python
return True, q6(stake * (1 - cost_sum) / cost_sum) if opt > 0 else (True, opt)
```

Python parses this as `(True, X if opt > 0 else (True, opt))`. For a confirmed
resolution-settled bet whose optimistic pnl is ≤ 0 but not exactly `-stake`
(VOID → `opt == 0`; any PARTIAL fraction), the function returns
`(True, (True, Decimal(...)))`. `report._live_section` then executes
`p["confirmed_pnl"] += pnl_conf` (`arb/report.py:71`) and `daily_frame` stores
it into `pnl_if_confirmed` — a `TypeError` crash or a corrupt cell in THE
deliverable, armed the day the first EV/NEGRISK bet voids or settles partial.

- Failing test first: a confirmed EV bet with a VOID settlement; assert
  `bet_confirmed` returns `(True, Decimal(0))`, and a PARTIAL case returning
  `(True, <the partial pnl>)` — both currently produce nested tuples.
- Fix: parenthesize/restructure into explicit branches. While there, decide
  and document what a confirmed-then-LOST resolution bet returns (the current
  intent looks like: confirmed basis for wins, `-stake` for losses, `opt`
  passthrough otherwise — make that explicit).
- Acceptance: `pytest -q tests/test_metrics.py` green including the new cases.

## Phase H2 — multi-leg resolution bets never produce P&L

`arb/metrics.py:45`: `r = results[0] if len(results) == 1 else None` — any
settled bet with >1 leg falls through and `bet_pnl_optimistic` returns `None`
forever. NEGRISK baskets and any multi-leg EV row are permanently "pending" in
every report the moment they settle. `scripts/headline.sql` has the same gap
(`per_leg[0]` only) — it and metrics.py agree with each other but both are
wrong for baskets.

- Failing test: a settled 3-leg NEGRISK NO-basket (fixture); assert the pnl
  equals the per-leg payoff sum. Derive the expected number by hand in the
  test comment (SPEC §7.1: NO-basket payout N−1 per unit).
- Fix in metrics.py only at read time (rule 9 untouched): per-leg payoff =
  Σ_legs [leg WON → contracts × (1 − c_i); LOST → −contracts × c_i;
  VOID → 0], with the same contracts-per-leg identity sizing uses (§7.2).
  Update `scripts/headline.sql` to match, and extend the CI fixture-DB
  assertion that the two agree to include a multi-leg settled bet.
- Acceptance: `pytest -q`; `arb report --daily` may change ONLY by settled
  multi-leg bets moving from pending to settled cells (state this in the
  commit message with the before/after cell values).

## Phase H3 — doubleheader missettlement in `settle_auto_mlb`

`arb/settlement.py:138-146`: a bet matches a final by team pair +
`|Δstart| ≤ 6h`, taking the FIRST match in the finals list. A doubleheader's
two games are both inside 6h of either bet's start — game 2's bet can settle
with game 1's result. Compounding it: `build_groups` had `gamePk`/`gameNumber`
at booking time (`arb/matching.py`, MatchedGroup.gamepk) and dropped them —
`tracker._book` never persists them.

- Failing test: two finals same pair same day (gameNumber 1 and 2), two bets
  with `:dh1`/`:dh2` event keys; current code settles at least one bet with
  the wrong game.
- Fix, two parts:
  a. Persist `gamepk` on `flat_bets` at booking (new nullable column via
     migration — an entry-time fact, so rule 9 is untouched).
  b. In `settle_auto_mlb`: settle by `gamePk` when the bet has one; otherwise
     pick the CLOSEST final by |Δstart|, and if two finals of the same pair
     fall inside the window, refuse to settle and append an annotation
     (`ambiguous-doubleheader`) instead — no guessing, consistent with rule 3's
     ethos.
- Also fix the sibling: `consensus._event_key` builds keys without the `:dhN`
  suffix, so consensus rows can never join doubleheader venue quotes (silent
  miss) — document it, and at minimum tag such rows rather than letting them
  join the wrong game. (EV is frozen; a documented tag is enough for now.)
- Acceptance: `pytest -q tests/test_ops.py tests/test_tracker.py` green plus
  the new settlement test.

## Phase H4 — reconcile is one-shot and its mismatch verdict is toothless

Two defects in `arb/reconcile.py`:

a. `reconcile()` skips any bet that already has ONE `reconciled` event
   (`arb/reconcile.py:80-81`), but it writes that event as soon as ANY leg
   resolves. A 2-leg bet with one leg confirmed and one venue still pending
   never gets its second leg checked — the docstring's per-(bet, venue) claim
   is not what the code does.
b. On mismatch it logs CRITICAL and appends an annotation saying the pair
   "must revert to REVIEW" (`arb/reconcile.py:105-117`) — and nothing
   enforces that. `tracker._book` computes `trusted` from static config
   (`arb/tracker.py:92-93`); the next bet on that pair books trusted as if
   nothing happened. SPEC §11.2 says "reverts the pair to REVIEW immediately".

- Failing tests: (a) fixture bet with legs on two venues, first pass resolves
  one leg, second pass must attempt the other (currently doesn't);
  (b) after a `settlement_mismatch` on pair P, a new booking on P must have
  `trusted = 0` (currently 1).
- Fix (a): re-reconcile until every leg has a non-None verdict — e.g. skip a
  bet only when the union of its `reconciled` events covers all legs; each
  pass appends a new event with the newly-resolved verdicts only (append-only,
  rule 9).
- Fix (b): a `pair_demotions` table (append-only: pair, reason, created_at)
  written by reconcile on mismatch; `tracker._book` computes trusted as
  (pair in config) AND (no demotion row). This is entry-time trust — the one
  place that stamps `trusted` — so rule 12 (classify) is untouched. Surface
  demotions in `arb status` and the daily report.
- Acceptance: `pytest -q`; grep-test additions if you add SQL touching
  flat_bets (the CI grep must stay green).

## Phase H5 — daily boundaries are UTC, config says America/New_York

`config.yaml` line 3 pins `timezone: America/New_York` — "all daily boundaries
EST/EDT". Every report query slices with `date(created_at)` /
`date(started_at)` on UTC ISO strings (`arb/report.py:44,60,85,91,102,134,139`)
and `cmd_report` defaults to machine-local `date.today()`
(`arb/commands.py:105`). A 9pm ET bet lands on the next day's report.

- Failing test: a bet created 01:00 UTC (= 21:00 ET previous day) must appear
  in the ET day's frame.
- Fix: one helper (in metrics.py or report.py — pick one, don't duplicate)
  mapping an ET day string to `[start_utc, end_utc)` ISO bounds; replace every
  `date(col)=?` slice with a half-open range compare (this also lets SQLite
  use the fetched_at/created_at indices instead of a function scan). Default
  day in `cmd_report` = today in `cfg["timezone"]`.
- `_cadence`, `_live_section`, `daily_frame`, dashboard queries, and
  `invariants` all slice — sweep them all in this one phase.
- Acceptance: `pytest -q`; `arb report --daily` for a day with no
  boundary-straddling bets must be byte-identical before/after (show the diff
  of the two JSON files in the commit message).

## Phase H6 — `max_reappearances` warns but books anyway; invariant-4's escape hatch is dead code

`arb/tracker.py:225-227` logs a warning at the cap and then books the row
regardless. `invariants.py:57-66` (invariant 4) excuses storms annotated
`reappearance-storm` — but nothing anywhere writes that annotation (grep: only
invariants.py mentions it). So a flapping quote books unbounded rows/24h and
invariant 4 fires forever with no remediation path.

- Failing test: (max_reappearances+1)-th sighting in 24h must NOT create a
  flat_bets row; it must go to `left_on_table` with reason
  `reappearance-storm` and write the `reappearance-storm` annotation
  (satisfying invariant 4's existing query shape).
- Fix in the tracker booking path only. The cap value stays in config (rule 4).
- Also fix invariant 4's window: `datetime('now','-1 day')` compares a
  space-separated SQLite string against `T`-separated ISO strings —
  lexicographically this reduces to a date-granularity window (off by up to a
  day). Pass a Python-computed ISO bound as a parameter instead, matching how
  `tracker.py:222` already does it.
- Acceptance: `pytest -q tests/test_tracker.py tests/test_hygiene.py` green.

## Phase H7 — fetch latency: 15–18s venues, self-inflicted leg skew

Live evidence (poll_venues, last 50 polls): kalshi avg 0.7s; polymarket avg
15.3s; sx avg 18.2s max 23.7s. Intra-poll quote-timestamp span reached 17.8s —
against the 10s actionable bound, exactly the skew watch.py's own docstring
forbids ("sequential fetching self-inflicts inter-leg skew"). Causes:

a. `arb/venues/base.py:36` — `requests.request(...)` fresh per call: no
   `Session`, so every one of the dozens of sequential calls per venue pays a
   new TCP+TLS handshake. Fix: one `requests.Session` per thread
   (`threading.local()`), used by `_request`. All adapters benefit.
b. Serial fan-out inside adapters: sx iterates 5 sports × leagues × ≤10
   pagination pages × order chunks (`arb/venues/sx.py:80-101`); polymarket
   iterates tags × pages × book chunks (`arb/venues/polymarket.py:106-118`).
   Parallelize the per-sport (sx) and per-sport-tag (polymarket) loops with
   the same ThreadPoolExecutor pattern `watch.py` already uses. Expected:
   venue time ≈ slowest single sport, ~3–5s.
c. `venue_fetch_timeout_secs: 15` is commented "per-venue budget"
   (`config.yaml:110`) but applied per-request — sx routinely runs 18–23s
   total. Make the semantics honest: keep the per-request timeout AND add a
   per-venue deadline (new config key) checked between pagination/chunk
   requests; on expiry stop fetching, return what's collected, and mark the
   venue `ok: false`/partial so §10.2 lifetime-carry semantics apply (a
   truncated fetch must never mass-close lifetimes — that's the V1 foot-gun).
- Tests: fixtures only. Session reuse: assert `_request` uses the shared
  session (mock transport). Deadline: fake clock, assert pagination stops and
  status degrades to partial. NO live-endpoint timing tests.
- Acceptance: `pytest -q`; then one supervised live cycle after commit, and
  compare poll_venues latency_ms before/after in the commit message.
  Afterwards `watch_interval_secs` MAY be lowered (separate dated config
  change; watch venues are keyless so rule 2 is untouched) — do not bundle it
  into this commit.

## Phase H8 — DB growth is unsustainable (~3 GB/day) and backups copy all of it

Live evidence: 1.25M quote rows in ~4.8h (~720MB), 271MB of it `ladder_json`.
At current cadence that's roughly 3 GB/day, and `cmd_backup` VACUUMs the FULL
DB into a new file daily. Rule 7 requires persisting every quote (it's the
backtest data) — it does not require them all to live in the hot DB forever.

- Add an `arb archive` command: move quotes (and their polls' child rows)
  older than `archive_after_days` (config) into a monthly archive DB
  `data/archive/arb-YYYYMM.sqlite`, then delete from the hot DB in the same
  transaction and VACUUM. Quotes/polls are NOT flat_bets/bet_events — rule 9
  does not protect them, but the CI grep-test must be checked so the new SQL
  doesn't trip it; every archived row must exist in the archive before the
  hot-DB delete commits (attach + `INSERT ... SELECT`, verify counts, then
  delete).
- Bets' markout windows read quotes ≤ 1h back; keep `archive_after_days`
  comfortably above that and above the lead-lag study's lookback — check
  `arb/leadlag.py` and document the floor next to the config key.
- Backups: `db_backup.keep_last` (config) — prune oldest beyond it after each
  backup. Archive DBs are backed up by existing means (they're immutable once
  the month closes).
- Supervisor schedules `archive` daily (config time, like the others).
- Failing test: fixture DB with old+new quotes; archive moves exactly the old
  rows, hot DB row count drops, archive DB contains them, report for a recent
  day unchanged.
- Acceptance: `pytest -q`; `arb report --daily` byte-identical for a recent
  day before/after archiving on a copy of the live DB.

## Phase H9 — supervisor: zombie children and boot-time schedule burst

`arb/heartbeat.py`:
a. Scheduled one-shots (`scan`/`settle`/`report`/`backup`, lines 116-131) are
   `Popen`'d and never waited — each leaves a zombie until the supervisor
   exits. Keep the handles in a list and reap (`poll()`) each loop tick.
b. `_due` fires every slot that is merely in the past: a supervisor started
   (or restarted) at 23:00 immediately fires ALL of that day's scan_times,
   settle, report, and backup in one burst — multiple credit-spending scans
   back-to-back (rule 2 exposure; the abort-below-50 guard is the only thing
   between this and a drained budget). On startup, initialize `last_fired`
   marks for slots more than `schedule_catchup_grace_mins` (config) in the
   past — fire only slots missed within the grace.
c. The stop path and `once` path call `proc.wait(timeout=cap)` bare — a hung
   child raises `TimeoutExpired` and crashes the supervisor mid-shutdown;
   catch it, `kill()`, and proceed. Same for the force-kill at line 106:
   follow `kill()` with `wait()` so the child is reaped.
- Tests: unit-test `_due`-with-grace against fixed datetimes and the reap list
  bookkeeping with a stub Popen. No real subprocess sleeps in pytest.
- Acceptance: `pytest -q tests/test_ops.py` green.

## Phase H10 — small sweeps (one commit, grouped)

a. **Duplicate events across tags**: `fetch_negrisk_events`
   (`arb/venues/polymarket.py:121-126`) and `cmd_political`
   (`arb/commands.py:180-186`) extend event lists across tags with no dedupe —
   overlapping tags will produce duplicate candidates with the same
   fingerprint in one poll, and `process_poll` books each `cands` entry
   (`active_fps` is snapshotted before the loop). Dedupe events by id at
   fetch, and add a belt in `process_poll`: skip a fingerprint already booked
   in this same poll.
b. **`exit_policy_pnls` "hold"** (`arb/metrics.py`): the key is initialized,
   never computed, and returned as None forever. Either compute it (final
   quote in the series, sell-basis) or delete the key and its column from the
   report — a dead column in the product is worse than no column. Check
   report.py usage before choosing.
c. **Polymarket outcome order assumption**: `quotes_from_event` maps
   `outcomes[0], outcomes[1]` to away/home (`arb/venues/polymarket.py:76`).
   Grouping is order-safe (frozenset), so impact is cosmetic — add the
   assertion to the recorded-fixture test so drift gets caught rather than
   trusted.
- Acceptance: `pytest -q` green; `arb report --daily` unchanged except the
  hold column if (b) chooses deletion (say so in the commit).

---

## Ordering, verification, and what NOT to do

- Implement H1→H10 strictly in order; each phase is one commit with the
  failing-test-first evidence quoted in the message (workflow §16).
- After H4 and H8, run `arb check-invariants` against a copy of the live DB
  and paste the (clean) output into the commit message.
- Do not refactor beyond each phase's blast radius. Do not touch
  `eligibility.py`, the math vectors (§7), or any fixture. Do not add
  dependencies — stdlib `threading`/`concurrent.futures` and the existing
  `requests` cover everything above.
- Modules stay under ~300 lines — if a fix would push one over, extract the
  smallest coherent piece, don't reorganize.
- If any live endpoint behaves differently than a fixture claims while you
  verify H7 (404/4xx), STOP and report per rule 3 — do not adapt.
