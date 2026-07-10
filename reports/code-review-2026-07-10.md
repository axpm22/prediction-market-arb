# Full-project review — 2026-07-10

Five parallel subsystem audits (money-math core, persistence/settlement, venue adapters,
CLI/ops, cross-cutting consistency) plus a manual pass over the unassigned small modules.
Baseline: 168/168 tests pass. Headline claims were re-verified against source before
inclusion; items the agents flagged "possibly intentional" were checked against SPEC.md
and reclassified where the spec settles it.

Severity legend: **Critical** = hard-rule (CLAUDE.md) violation or breaks functionality.
**High** = incorrect logic / real bug. **Medium** = inconsistency, smell, missing error
handling. **Low** = style/dead code. Items marked **[FIXED]** were repaired in Phase 3;
**[DECISION]** items await the owner's ruling.

---

## PHASE 1 — AUDIT

### Critical

**C1. Rule 4 (no magic numbers) — thresholds/schedule times hardcoded.** [FIXED]
- `arb/matching.py:27` — `MATCH_WINDOW = timedelta(hours=6)`: the MLB-schedule match window.
- `arb/settlement.py:135` — `window = timedelta(hours=6)`: doubleheader same-pair-finals
  disambiguation window (a *different* value from `execution.doubleheader_cluster_tol_secs`
  = 2h, which covers matching).
- `arb/heartbeat.py:80,182,186` — `"05:00"`/`"05:30"` shape-check and canary fire times,
  each literal repeated twice, while every sibling slot is config-pinned.
- `arb/commands.py:307` + `arb/dashboard.py:30` — heartbeat-freshness multiplier `3 *
  watch_interval_secs` duplicated in two files.
- `arb/commands.py:203` — political Gamma scan hardcodes `max_pages=2`.

**C2. Dead knobs that claim to govern behavior (silent-default drift).** [FIXED where wired]
- `log_retention_days: 90` (config.yaml:210) is read by nothing; `arb/logutil.py:27`
  hardcodes 90 and no caller passes it. Changing the config does nothing.
- `negrisk.scan_pages: 3` (config.yaml:151 — the knob that fixed a named V1 bug) is never
  read; `arb/venues/polymarket.py:33` hardcodes `max_pages=10`.
- Not wired (unimplemented features → DECISION): `negrisk.kalshi_events`, `tape_check.*`,
  `exit.exit_price_basis`/`exit.policies` (metrics hardcodes bid-minus-fee),
  `mismatch_pricing.rule_of_three` (eligibility.py:82 hardcodes 3/n).

**C3. [DECISION] Rule 7 candidate — negrisk and political scans persist no quotes.**
`arb/commands.py:83-92` (`cmd_negrisk_scan`) and `:213-216` (`cmd_political`) write
`polls` + `opportunities` rows but zero `quotes` rows; `cmd_political` even reports
`"n_quotes": len(tokens)` for rows that don't exist. If rule 7 ("persist every quote…
this is the backtest data") is scoped to watch/live cycles only, this is fine — but the
political `n_quotes` figure is misleading either way.

**C4. [DECISION] Rule 13 candidate — supervisor never verifies committed HEAD / clean
tree / green pytest.** `arb/heartbeat.py:116-124` starts spawning without any
`git status --porcelain` or pytest gate. If enforcement is procedural, nothing in code
stops a dirty-tree launch.

### High — fixed in Phase 3

- **H1** `arb/metrics.py:97-101` — `bet_confirmed` infers result class from the *sign* of
  the optimistic pnl. A profitable PARTIAL (fraction > cost) hits the `opt > 0` branch and
  is booked as a full WIN recomputed on the confirmed price, contradicting the function's
  own comment. Untested branch (test only covers fraction < cost).
- **H2** `arb/settlement.py:72` — PARTIAL settlements run `_leg_result(leg, "PARTIAL")`,
  writing garbage `per_leg` (every YES leg "LOST"). `arb/reconcile.py:126-128` then
  compares venue verdicts against that fiction → false `settlement_mismatch` → permanent,
  append-only venue-pair demotion off garbage data.
- **H3** `arb/pricing.py:24-27,48,64` — `devig()` crashes on out-of-domain odds (odds < 1
  → uncaught OverflowError; odds == 0 → DivisionByZero; empty list → div-by-zero), and the
  power/Shin solvers silently return non-normalized "probabilities" when their hard caps
  bind (verified: shin on 1.30/1.30 returns sum=1.115; power on 5.0/5.0 returns sum=0.894).
  Feeder `arb/venues/oddsapi.py:63` does no validation — one malformed book price kills a
  live scan or poisons consensus.
- **H4** `arb/arbitrage.py:122-125` — the `continue` for suspect groups skips the
  divergence scan, so the "suspect groups may log divergences (tagged), never locked
  flags (§8.2)" comment is dead and the `"match-suspect"` ternary at :180 is unreachable.
- **H5** `arb/capacity.py:20-27` — `_leg_quote` joins `outcome=? AND side=?` with post-map
  leg identity, but quotes persist pre-map (`arb/cycle.py:77` stores raw fetch). Every
  complement-mapped NO leg misses → position silently excluded from projections *and*
  drags `ladder_fraction` under the gate, pinning capacity at INSUFFICIENT_DEPTH_DATA.
- **H6** `arb/sizing.py:22-25` — `parse_ladder` catches `(ValueError, SyntaxError,
  TypeError)` but `Decimal("abc")` raises `InvalidOperation`; one malformed ladder_json
  row crashes `arb report --daily`. Contradicts its own docstring.
- **H7** `arb/commands.py:77-78` — every negRisk basket fee-priced as "politics"
  regardless of tag; economy/weather events get the wrong fee rate → overstated edges
  persisted to `opportunities`.
- **H8** `arb/venues/sx.py:38` — SX ladder levels store *USD taker depth*; Polymarket
  ladders store *contracts*; the shared consumer (`arb/sizing.py` vwap/q_star,
  `arb/capacity.py:45`) reads all ladders as contracts. Every stored SX ladder is in units
  the sizing model misreads — and quotes are the permanent backtest record.
- **H9** `arb/venues/base.py:64-83` — retries don't consult the per-venue `Deadline`; one
  slow request can stretch a 30s venue budget to ~77s. Plus silent pagination truncation
  (`polymarket.py:35`, `kalshi.py:106` — page cap hit → loop ends, status stays ok:true),
  and kalshi has no deadline at all.
- **H10** `arb/eligibility.py:86-87` vs `arb/tracker.py:86-95` — trust decided in two
  places with different answers: `classify` checks `trusted_pairs` only; `booking_trusted`
  also checks `pair_demotions`. After a mismatch demotion, classify still says clean.
  Rule 12 wants one predicate.
- **H11** `arb/commands.py:332` — sntp offset parsed from `split()[-2]`, which is the
  server hostname on real macOS output; float() raises, blanket except swallows it, the
  clock-skew alarm can never fire (`clock_skew_alarm_secs` dead at runtime).
- **H12** `arb/commands.py:389-390` — `VACUUM INTO` an existing same-day target raises
  OperationalError; second `arb backup` in a day crashes, violating the CLI's stated
  idempotency contract (cli.py:3).
- **H13** `arb/dashboard.py:78` + `arb/lanes/live.py:33` — `datetime('now', '-2 hours')`
  compared against `T`-separated ISO strings: `'T' > ' '` makes every same-date row pass
  regardless of time (the trap `arb/invariants.py:56-59` documents and avoids).
- **H14** `arb/heartbeat.py:129,165` — supervisor children spawned as `python -m arb <cmd>`
  without forwarding `--db`/`--config`; a supervisor started on a non-default DB silently
  splits data across two databases.
- **H15** `arb/logutil.py:36-37` — every module logger attaches its *own*
  TimedRotatingFileHandler to the same `logs/scan.log`; at midnight each handler
  independently renames the file, clobbering backups (within one process alone there are
  ~10 handlers on scan.log).
- **H16** `arb/tracker.py:52-64` — `candidate_from_basket` has zero callers and reads keys
  (`payout`, `roi`, `cost_sum`, `proof_status`) that don't exist on the rows it would be
  given (plus a nonsense `endale=None` param). Dead code masquerading as the NEGRISK
  booking lane. Deleted; wiring NEGRISK booking is a DECISION (below).

### High — needs your ruling [DECISION]

- **D1. NEGRISK baskets are never paper-booked.** SPEC §10.1 says "$1 across the basket";
  the scan writes only `opportunities`. Wiring it is a feature build during the
  pre-registration freeze, and three latent bugs fire the moment it's wired: basket legs
  `{label, side, cost}` lack the keys every consumer needs (capacity/sizing/tracker →
  KeyError), `bet_confirmed` assumes payout=1 (wrong for NO-baskets, and `flat_bets` has
  no payout column to derive it), and `settle_bet`'s `_leg_result` KeyErrors on basket
  legs. Decide: wire booking (with a dated amendment) or record that NEGRISK is
  flag-only for now.
- **D2. `nonsports_max_days` booking gate is dead.** `arb/tracker.py:209-213` gates only
  when `event_start_utc` is truthy; nonsports candidates always carry None. Enforcing it
  needs a resolution-date field plumbed from the venue (market endDate). Political lane is
  exempt anyway, and other nonsports lanes are disabled, so it's currently harmless — but
  eligibility.py:40-42 documents a guard that doesn't exist.
- **D3. Settle path never consults the eligibility predicate; `RESOLVED_REVIEW` (SPEC §11)
  appears nowhere.** Lane segregation survives via the entry-time `lane` column, so no
  P&L leak — but rule 12's consumer list is not true of the code. Implement or amend spec.
- **D4. `headline()` vs `scripts/headline.sql` disagree** on a settled bet with an
  unresolved leg (metrics returns pending/None; the SQL counts it with `ELSE 0`).
  Likely unreachable if invariant 6 holds; align the SQL or document.
- **D5. Reconcile never gives up**: SX legs (always unresolvable) and entry-settled locked
  bets (no per_leg → mismatch detection structurally impossible) are re-fetched every
  pass, forever — venue GETs growing with book size for zero signal.
- **D6. Reclassified as intentional after SPEC check** (no action, for the record):
  tennis/MMA surname+first-initial normalization is spec-pinned (SPEC.md:321) — the
  same-surname-same-initial collision risk is accepted at spec level (note: a venue
  listing "Surname Firstname" order would still key wrong; spec-level limitation).
  Polymarket `POST /books` is SPEC §6.2-sanctioned; CLAUDE.md rule-1 wording says "GETs"
  — the rule text and spec disagree, substance (unauthenticated, read-only) is fine.

### Medium (report only — say which to fix)

Correctness-adjacent:
- M1 `arb/venues/kalshi.py:70-97` — one malformed market row KeyErrors the whole venue's
  poll (polymarket skips bad rows; kalshi doesn't).
- M2 `arb/venues/sx.py:37` — `percentageOdds == "0"` → DivisionByZero kills the venue poll.
- M3 `arb/venues/oddsapi.py:43` — `raise_for_status()` embeds the full URL *including the
  API key* in the exception; an uncaught traceback bypasses log redaction.
- M4 `arb/commands.py:137-148` — `CreditsLow` mid-scan propagates as a raw traceback and
  skips the `oddsapi-credits` annotation on exactly the path where credits matter.
- M5 `arb/store.py` — no transactions around multi-insert logical units (bet + entry
  event; settle + tripwire + blacklist; mismatch + demotion): a crash between commits
  leaves half-written state.
- M6 `arb/settlement.py:100-114` — `settle_manual` winner token unvalidated
  case-sensitively; a typo silently settles every YES leg LOST, immutably.
- M7 settle counters count grace/future refusals as settlements (settlement.py:113,179).
- M8 `arb/negrisk.py:29` — mids computed before the two-sided guard; disable
  `require_two_sided_all_outcomes` and a one-sided outcome crashes (Decimal + None).
- M9 `arb/negrisk.py:43,85` — silent drops (missing ask, > max_outcomes) with no
  left_on_table diagnostic, contrary to the project's own bug-E2 doctrine.
- M10 `arb/metrics.py:123-124` — pnl_markout contracts basis excludes fees; every other
  metric includes them (markout drift inflated by the fee fraction).
- M11 `arb/tracker.py:141` — intra-poll position dedup missing: two new same-position
  candidates in one poll book two bets.
- M12 `arb/tracker.py:196` — `consensus_age_secs` read but never set by any producer;
  stale-consensus booking gate inert (matters the day `ev_enabled` flips).
- M13 `arb/invariants.py:61,83-88` — clock not injectable; persisting violations
  re-append identical annotation rows every run (append-only spam).
- M14 `arb/settlement.py:15` etc. — `ZoneInfo("America/New_York")` hardcoded in 5 modules
  while cfg["timezone"] is read in 4 others; the knob half-works.
- M15 `arb/commands.py:413` — `cmd_canary` imports from `tests/` at runtime; the
  supervisor schedules it monthly.
- M16 `arb/lanes/verify.py:50-60` — each bet verified at most once ever; lane absent from
  supervisor.processes; `str(r) if r else None` turns a legit Decimal("0") requote into
  unverifiable.
- M17 `arb/settlement.py:46` — TOTAL leg with null line → `Decimal(None)` TypeError
  crashes the settle pass.
- M18 `arb/archive.py:45-62` — a mid-INSERT failure leaves the txn open; `finally: DETACH`
  then fails and masks the original error. Plus `CREATE TABLE … AS SELECT WHERE 0`
  snapshots today's columns (latent break on future ALTER TABLE).
- M19 `arb/lanes/live.py:189` — SX requoter emits unfiltered quotes → "coverage" can
  exceed 100%; `arb/report.py:61-74` live section skips the annotation-exclusion rule the
  headline applies.
- M20 `arb/consensus.py:89-90,104,116` — no dedupe of a book listing the same outcome
  twice; outlier-drop floor `3` hardcoded (documented as binding comment in config).

Hygiene/consistency:
- M21 ~50 config leaves schema-required but read by nothing (`consensus_anchor`,
  `ai_matcher`, `scoring.*`, `validation.ev_filter`, `mc_sim`, `caps`, `kind_taxonomy`,
  `risk_free_rate`, `parlay`, `hot.*`, `live_lane.kill.*`, most of `deployment`/`ev`/
  `micro`, `max_stake_pct`, `ev_threshold`, `push_pricing`, `break_windows`…). Many are
  deliberate pre-registration freezes — but config.yaml:1 ("every value the system
  reads") is now false for ~30% of the file. Suggest a "reserved" marker or SCHEMA split.
- M22 Dead code: `arb/settlement_matrix.py` (whole module, incl. the §18.2 gate),
  `oddsapi.fetch_scores`, `kalshi.fetch_event/fetch_trades/refresh_series_cache`,
  `sx.fetch_market_status` (while `lanes/live.py:130-133` re-hardcodes the same endpoints
  inline, unchunked → 414 risk), `leadlag.cross_correlation`, `metrics.pnl_confirmed_cell`,
  `metrics.exit_policy_pnls` (the §10.8 exit engine — computed by nothing, appears in no
  report), the `'void'` bet_events kind (consumed in 5 queries, produced nowhere).
- M23 Duplicated logic: `NO_DRAW_SPORTS` ≡ `STRICT_BINARY_WINNER_SPORTS`; locked-basket
  ROI math implemented 3× (arbitrage/tracker/negrisk) *without* calling the rule-8
  vector-pinned `pricing.true_arb_sum`/`basket_edge` (quantization drift possible in the
  6th decimal); `('EV','NEGRISK','BTC')` literal in 3 SQL strings; EXCLUSION_ANNOTATIONS
  duplicated; supervisor-status logic duplicated between cmd_status and dashboard (which
  also hardcodes `.locks/supervisor/pid` instead of importing heartbeat.LOCK_DIR).
- M24 `arb/commands.py` is 474 lines vs the binding ~300-line rule.
- M25 CLI: `regrade`/`hot`/`btc`/`nonsports` subcommands wired to "not implemented yet";
  `--once` parsed everywhere and ignored by every handler.
- M26 Missing indexes for the stated read paths: `flat_bets(created_at)`,
  `polls(started_at)`, `heartbeats(process, beat_at)`, `annotations(target_kind,
  target_id)` — metrics.py:22 docstring claims indexes that don't exist; heartbeats
  full-scanned per 30s dashboard refresh; `heartbeats`/`annotations` grow unbounded
  outside the archive path.
- M27 `arb/dashboard.py:126` — fresh `Store` (re-running migrations) per request, never
  closed; `cmd_report` computes `daily_frame` twice (write + print).
- M28 `arb/heartbeat.py:131-134` — `_interval()` special-cases only "live"; any other
  enabled lane silently runs at watch cadence (verify.poll_secs read by nothing). No
  SIGTERM handling; stop-flag shutdown can take max_child_runtime_secs per child.
- M29 config-required map entries unprotected: `line_velocity_points_per_sec["default"]`,
  `max_child_runtime_secs["default"]`, `odds_api_markets["default"]` are hard KeyErrors
  if removed — MAP() validates value types only.
- M30 `arb/store.py:44-52` — `insert_many` takes columns from `rows[0]` but values from
  each row's own dict order: silent column misalignment if a caller ever builds rows with
  differing key order (all current callers are single-comprehension, so theoretical).

### Low
Unused imports (`invariants.py:5` json, `matching.py:6` re, `matching.py:13` **rapidfuzz —
a whole third-party dep with zero call sites**, `models.py:5` field, several in tests);
`__import__("os")`×3 in heartbeat, `__import__("zoneinfo")` in dashboard; `from decimal
import Decimal` inside loops (markout.py:50, commands.py:58,68); falsy-zero rendering
(`report.py:206-229` — values rounding to 0 print '—'; `report.py:262-264` prints literal
"mean=Nones" on an empty day; `verify.py:60` Decimal("0") → None); `tracker.py:118`
`.strip("; ")` strips a char-set, not the suffix; kalshi `_empty_book` string-compares
`"1.0000"`; `polymarket.py:105` `m.get('conditionId', m.get('id'))` yields "None:…" on
present-but-null; backup globs disagree (`arb-*.sqlite` vs `*.sqlite`); config.yaml:211
says ".backup" but code does VACUUM INTO; `exit.policies` still lists "hold" (H10 removed
it); stale fixture provenance note in oddsapi.py:5 (fixture is real per
data/verified_facts.md); 11 of 19 fixtures never loaded by anything; everything
cwd-relative (running outside repo root creates a parallel universe of state);
`commands.py:459` vestigial always-true `isdisjoint(set())` in the sx shape-check;
`sntp` scraping is fragile even when fixed (single source, no fallback);
`consensus.py` normalizes every row twice; `settlement.py:175` a tied MLB feed row would
settle away-team-wins instead of refusing.

---

## PHASE 2 — IMPROVEMENT OPPORTUNITIES (no behavior change intended)

**Performance**
1. Report path is O(bets × 3-5 queries) and computed twice per `arb report`
   (report.py:104-118, commands.py:116-117). Batch bet_events/annotations/markouts per
   day into three keyed queries and pass the frame from write_daily to print_daily.
   Expected: ~4× fewer queries, halved report cost.
2. Add the four missing indexes (M26). Three lines; makes the H5 intent real.
3. Dashboard: one Store for the server lifetime (M27); avoids re-running migrations
   every 30s refresh.
4. Reconcile: SQL anti-join on fully-reconciled bets + a give-up bound (D5) keeps the
   pass O(open work) instead of O(history) — also stops pointless venue GETs.
5. `cycle.persist_poll` shells `git rev-parse` per poll — cache at process start.
6. tracker `process_poll` N+1s (has_confirm per bet per poll; 3 queries per candidate)
   → two batched queries per poll.

**Logic simplification (ponytail-tagged)**
- `delete:` rapidfuzz import + `fuzzy_match_threshold` key + requirements pin (zero call
  sites; the project's own doctrine forbids fuzzy matching).
- `delete:` or wire: settlement_matrix.py, exit_policy_pnls, pnl_confirmed_cell,
  cross_correlation (+`leadlag.max_lag_polls`), the six dead venue functions, 11 orphan
  fixtures. A gate module nothing calls is worse than no gate module.
- `shrink:` arbitrage/tracker/negrisk inline ROI math → call the rule-8 vector-pinned
  pricing functions (kills the 6th-decimal drift and makes rule 8's "single math
  authority" true in the call graph).
- `shrink:` `NO_DRAW_SPORTS` → import STRICT_BINARY_WINNER_SPORTS.
- `shrink:` one `supervisor_state()` helper for cmd_status + dashboard (kills M23's
  duplication and the hardcoded lock path).
- `shrink:` generate `IN ('EV','NEGRISK','BTC')` from models.RESOLUTION_SETTLED_KINDS;
  move EXCLUSION_ANNOTATIONS to models.
- `stdlib:` `__import__("os")`/`__import__("zoneinfo")` → normal imports.
- `yagni:` split config SCHEMA into wired vs reserved so the strict loader stops forcing
  ~50 inert keys on every boot (M21) — or at minimum mark reserved blocks in the file.

**Architecture**
- The pre-map/post-map quote identity seam (H5) is the one real design flaw: detection,
  tracker and cycle agree on post-map identity; the quotes table is pre-map. The Phase 3
  fix makes capacity complement-aware; longer term, persist the mapped identity alongside
  the raw one so one representation owns the join key.
- Trust-boundary validation is inconsistent by module: config.py rejects unknown keys,
  fees.py KeyErrors unknown venues, but oddsapi/parse_ladder/negrisk accept anything.
  Validate odds and ladders once, at ingestion.
- 12 modules log to one `logs/scan.log`; settle/verify/dashboard forensics land in a file
  named "scan". Consider per-lane files (also finishes the H15 rotation fix).
- `commands.py` (474 lines) wants splitting along lane lines anyway (M24).

---

## Notes on the pre-registration freeze

Fixes H1/H2/H3/H4/H7/H8 change measured outputs (consensus rows, divergence counts,
stored SX ladders, PM fee costs, PARTIAL-bet P&L). Per the freeze discipline these are
bug-fix amendments, recorded as a dated entry in PREREGISTRATION.md rather than silent
changes.
