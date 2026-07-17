# Claude Code prompt — OPS + SPEED pass (post-review fixes, v1, 2026-07-17)

You are Claude Code working in the ArbitrageModel repo. Read `CLAUDE.md` and
SPEC.md §4 before touching anything; both bind every phase below verbatim.
This prompt fixes the findings of `reports/nonsports-pipeline-review-2026-07-17.md`
(review run against the live system: ANJ regulator DNS block on Polymarket
since 2026-07-16, 15.2 GB WAL, watch cadence collapsed to 215 polls/day vs
~990 expected). Reproduce each finding yourself before writing the failing
test (workflow §16).

**Precondition (rule 13):** start from a committed HEAD with green `pytest -q`.
The tree currently carries untracked work (`scripts/nonsports_pipeline.sh`,
`reports/*`, `prompts/*`, modified `.DS_Store`, `data/archive/`) — Phase 0
settles that with the operator BEFORE any code phase. One commit per phase,
evidence in the message.

**Global constraints, restated because they bind here:**
- Tests run on recorded fixtures only; no live HTTP in pytest (rule 2).
  Polymarket is regulator-blocked right now, so live verification of anything
  PM-side is IMPOSSIBLE — no phase below may depend on it.
- No `UPDATE`/`DELETE` on `flat_bets`/`bet_events` (rule 9); every fix is new
  reads, new append-only rows, or process changes.
- Money math `Decimal` (rule 5); new thresholds go in `config.yaml` (rule 4).
- `arb/eligibility.py` is untouched (rule 12).
- After each phase: full `pytest -q` green; `arb report --daily` unchanged
  except where a phase explicitly says otherwise.

**DO-NOT-TOUCH list (interference with frozen research state):**
- `arb/nonsports/ai.py::PROMPT`, `SCHEMA`, `match.py::SHAPE_VERSION`,
  `shape_relation`, `parse_relation`, and the `_RULES_SHA` computation. ANY
  byte change resets the verdict-cache sha and invalidates the accumulating
  §18.5 evidence stream (PREREGISTRATION 2026-07-13 entries). Phase 4 edits
  the same FILE — it must not move these definitions.
- `ai_matcher.enabled` stays `false` (flips only by dated amendment after the
  §18.5 bar passes on post-freeze pairs). Same for every other gate flag.
- No threshold, fee, or schedule value changes anywhere in this pass.

## Non-interference plan (read before Phase 1)

Files touched, per phase — verify this stays true as you work; if a phase
grows into another phase's file, stop and re-plan instead of interleaving:

| Phase | Files | Depends on |
|---|---|---|
| 1 | `arb/venues/base.py`, `config.yaml` (+1 key), `tests/test_venues.py` | — |
| 2 | `arb/commands.py` (cmd_backup), `arb/dashboard.py`/`arb/store.py` (reader audit only), `tests/test_ops.py` | — |
| 3 | `arb/logutil.py` (one env knob), `tests/conftest.py` | — |
| 4 | `arb/nonsports/ai.py` (`evaluate`, `run_ai`, `_cached` ONLY), `tests/test_nonsports_ai.py` | — |
| 5 | `arb/nonsports/mapped.py`, `tests/test_mapped.py` | Phase 1 merged |
| 6 | `arb/nonsports/scan.py`, `tests/test_nonsports_match.py` | Phase 1 merged |
| 7 | `arb/lanes/political.py`, `tests/test_lanes.py` | — |

Cross-phase contracts that make them safe together:
- Phase 1 changes RETRY behavior only; `VenueError` remains the one exception
  type callers see, so mapped/scan/watch/live `try/except` blocks (and Phases
  5–6, which restructure two of those callers) need no coordination beyond
  landing Phase 1 first.
- Phase 1's block-skip state must live inside `base.py` (per-process, reset
  each poll/run) so NO lane code changes for it — that is what keeps Phases
  5–7 independent of it.
- Phases 5 and 6 introduce concurrency/batching that leans on `base.py`'s
  existing thread-local `_session()` (H7) and thread-safe `Deadline`
  (monotonic reads). Do not add shared mutable state to `base.py` in Phase 1
  beyond the per-venue block flag; if the flag dict can be touched from
  threads (Phase 6 pool), guard it with a `threading.Lock` in Phase 1 now.
- Phase 4's three edits sit in three disjoint functions of one file — do them
  in one commit so no other phase ever sees a half-edited `ai.py`.

Phases are ordered by severity: live operational damage first, then data/ops
risk, then hygiene, then speed. Implement strictly in order.

---

## Phase 0 — settle the tree (with the operator; no code)

Ask the operator to commit or explicitly discard the untracked work listed in
the precondition. Add `.DS_Store` and `data/archive/` to `.gitignore` and
`git rm --cached .DS_Store`. Acceptance: `git status` clean, `pytest -q` green.

## Phase 1 — SSL cert failures are non-retryable; blocked venue short-circuits

Evidence: `arb/venues/base.py:68-93` runs the full retry ladder (backoff
sleeps included) on `requests.exceptions.SSLError`, though a certificate
mismatch cannot succeed on retry. Since 2026-07-16 every PM call dies this way
(ANJ DNS poisoning, cert `CN=*.anj.fr`; 225 failures 07-16, 277 on 07-17),
and the wasted ladders degraded EVERY lane: watch made 215 polls by 16:30
on 07-17 (60s target ⇒ ~990), p95 poll gap 3219s (`arb report --daily`).

- Failing tests first (fixtures/mocked session, rule 2):
  a) `_request` raises `VenueError` after exactly ONE attempt when the session
     raises `SSLError` — assert no sleep, no second attempt (mock counts).
  b) After an SSLError for a host, subsequent `get_json`/`post_json` calls to
     the SAME host within the same block-window raise `VenueError`
     immediately without touching the session; calls to OTHER hosts are
     unaffected.
  c) After the window expires the host is probed again (block is a cache,
     not a ban — the ANJ block will someday lift and no restart should be
     needed to recover).
- Fix: in `_request`, catch `requests.exceptions.SSLError` before the generic
  `RequestException` arm and raise `VenueError` immediately, logging ONE line
  per host per block-window: host, served cert CN if cheaply available, and
  "non-retryable TLS failure — venue blocked?(regulator/MITM)". Keep a
  module-level `{host: monotonic_expiry}` dict (lock-guarded, see the
  non-interference plan) consulted at the top of `_request`. Window length is
  a new `config.yaml` key (rule 4), e.g.
  `execution.tls_block_cache_secs: 300` — comment it as the ANJ-block
  fast-fail, cache-not-ban.
- Acceptance: `pytest -q` green; on the live box a single watch poll's log
  shows at most ONE blocked-venue line per PM host instead of a warning per
  request. `arb report --daily` unchanged (quotes from blocked venues were
  already zero).

## Phase 2 — WAL checkpoint in the backup window + long-lived-reader audit

Evidence: `data/arb.sqlite` 13.8 GB with a 15.2 GB `-wal` (2026-07-17) —
checkpoints never complete. `arb/store.py:31-32` sets WAL mode but nothing
ever forces a checkpoint, and some connection (dashboard and/or a lane loop)
holds reads open across polls, starving the auto-checkpoint.

- Failing test first: `cmd_backup` on a tmp DB with a fat WAL (write rows,
  keep a second connection's read open then close it) leaves the WAL at ~0
  bytes after the backup step and logs the `(busy, log, checkpointed)` triple
  returned by `PRAGMA wal_checkpoint(TRUNCATE)`.
- Fix: in `cmd_backup` (`arb/commands.py`), after the `VACUUM INTO`/backup
  succeeds, run `PRAGMA wal_checkpoint(TRUNCATE)` on the live DB and log the
  result triple; a busy/partial result is a WARNING, never a failure (the
  04:10 slot is the quiet window, it will win most nights). Then AUDIT
  readers: every `Store` consumer that holds a connection across polls
  (dashboard first suspect) either closes/reopens per request or opens with
  `mode=ro` short-lived cursors. Fix what the audit finds in the same phase
  ONLY if it is a pure connection-lifecycle change; anything bigger gets
  reported, not done.
- Acceptance: `pytest -q` green; after the next 04:10 run (or a manual
  `arb backup` in a quiet moment, operator's call) `ls -la data/` shows the
  WAL collapsed. Report unchanged.

## Phase 3 — pytest stops writing into production logs

Evidence: `logs/supervisor.log` 2026-07-17 16:33:39 and 16:36:00 — pairs of
"supervisor up"/"stop-all flag present" emitted by TEST runs
(`tests/test_ops.py:68` monkeypatches `STOP_FLAG`/`LOCK_DIR` but not the
logger; `arb/logutil.py:45-60` caches real-path `TimedRotatingFileHandler`s
at first import). Production forensics now contain fake supervisor events.

- Failing test first: a test that imports `arb.heartbeat` under the redirect
  and logs; assert nothing was appended to the repo's `logs/` (snapshot
  mtimes/sizes) and the message landed under the redirected root.
- Fix: `get_logger` honors an `ARB_LOG_DIR` env var as the root for relative
  logfile paths (default unchanged: repo-relative). `tests/conftest.py` sets
  `ARB_LOG_DIR` to a session tmp dir BEFORE any `arb` import. This is test
  plumbing, not a tunable — env var, not `config.yaml`, is deliberate.
  Mind the `_file_handlers` cache: the env must be read at handler-creation
  time, and conftest must set it before the first `arb` module import.
- Acceptance: run `pytest -q` twice and diff `logs/` mtimes — untouched.

## Phase 4 — `ai.py` read-path fixes (one commit: evaluate + budget + cache)

Three disjoint-function edits in `arb/nonsports/ai.py`; the DO-NOT-TOUCH list
above governs the rest of the file. All three are test-first against a tmp
store + candidates fixture; NO API client is constructed in any test.

4a. `evaluate()` (line ~199) drops a human `same-inverted` verdict from
    ground truth (`in ("same", "different")`), silently shrinking the §18.5
    denominator. Fix: truth-set filter accepts any human verdict that
    `startswith("same")` or equals `"different"`, and maps the startswith
    case to truth `"same"` (the AI side already normalizes via
    `verdict.startswith("same")`). Test: candidates fixture with a human
    `same-inverted` pair + a cached `same-inverted` AI verdict scores TP=1.
4b. `run_ai()` budget counts shape-only pairs: `classified` increments (line
    ~183) and `_spent_today` counts every verdicts row, so 248 free
    code-decided verdicts burn API budget. Fix: count ONLY pairs that
    actually called the API — increment a separate counter for API calls,
    gate on that vs budget, and make `_spent_today` count
    `WHERE cost_usd > 0` (comment: shape verdicts are free and unlimited;
    the cap is Anthropic spend, config comment `max_new_pairs_per_day`
    updated to say "API-classified pairs"). Shape-only verdicts still insert
    (cache completeness) and still write annotations.
4c. `_cached()` is one SELECT per candidate into a 13.8 GB live DB (~257
    round trips per run). Fix: one
    `SELECT pair_key FROM verdicts WHERE rules_sha=? AND model=?` into a
    `set` before the loop; drop the per-pair helper or reduce it to the set
    lookup. Behavior identical (test: same `todo` selection on a store with
    a mix of cached/uncached pairs, one query observed via a counting
    wrapper).
- Acceptance: `pytest -q tests/test_nonsports_ai.py` green including the
  three new tests; `arb nonsports --eval` on the live box prints the SAME
  §18.5 numbers as 2026-07-17 (TP 9 / FP 0 / FN 0 / TN 248, precision 1.0)
  — these fixes must not move the score, only harden its edges.

## Phase 5 — mapped lane: batch the CLOB books call (Gamma batching DEFERRED)

Evidence: `arb/nonsports/mapped.py:84-111` + `run_mapped` — per pair per
poll: one Gamma GET + one `POST /books` carrying only that pair's 2 tokens.
9 pairs ⇒ 18 sequential requests. `POST /books` is the verified batch quote
endpoint (rule 1/§6) and takes all tokens in one payload.

- SCOPE GUARD: batching the Gamma status GET via a comma-list `condition_ids`
  is NOT verified in `data/verified_facts.md`, and PM is currently blocked so
  it CANNOT be verified live (rule 3). Keep per-pair Gamma GETs. Leave a
  dated comment: "batch condition_ids unverified — probe when the ANJ block
  lifts, then collapse".
- Failing test first (fixtures): `run_mapped` over 3 map entries issues
  exactly ONE `POST /books` containing all 6 tokens (assert via a recording
  fake of `post_json`), and per-pair quote/opportunity output is IDENTICAL to
  the current per-pair implementation (golden comparison on the existing
  fixture data). A pair whose Gamma GET fails still only poisons ITS quotes
  (error isolation preserved — today's per-pair `try/except` in `run_mapped`
  must survive the restructure).
- Fix: hoist the books fetch: collect `(entry, tokens)` for all non-banned,
  open, two-token pairs first (per-pair Gamma GETs, each in the existing
  try/except), then one `post_json(CLOB/books)` for the union, then build
  each pair's quotes from the shared `{asset_id: book}` dict. A books-call
  failure marks ALL pairs errored this poll (it is one request now) — log it
  as such.
- Acceptance: `pytest -q` green; fixture-verified identical output. Live
  verification waits for the block to lift; note that in the commit message.

## Phase 6 — matcher: parallelize the Kalshi series fetch

Evidence: `arb/nonsports/scan.py:22-45` — up to `max_series_fetch: 150`
sequential `GET /markets?series_ticker=` calls inside a 240s deadline;
truncation warnings observed. Pure I/O fan-out; `base.py` already keeps one
`requests.Session` PER THREAD (H7) precisely for this shape.

- Failing test first: with a fake `get_json` that sleeps 50ms per call, 20
  concept-hit series complete well under the sequential lower bound (assert
  wall-clock < ~0.5× sequential), and the returned rows equal the sequential
  implementation's rows (order-insensitive compare on ticker).
- Fix: `concurrent.futures.ThreadPoolExecutor` over the capped `hits` list,
  pool size from a new `nonsports.series_fetch_workers` key (rule 4; default
  8, comment why). Deadline: check `deadline.expired()` before SUBMITTING
  each batch and let in-flight calls finish (Deadline reads are thread-safe;
  `_request` already trims per-call timeout to `remaining()`). Preserve the
  existing partial-surface warning when the deadline truncates. Results
  collected in submission order so `kalshi_rows` output stays deterministic.
  The alternative single-stream design (global `/markets?status=open` listing,
  verified 2026-07-14) is NOT this phase — it changes coverage semantics;
  record it as an option in the code comment only.
- Acceptance: `pytest -q` green; a live `arb nonsports` run (Kalshi is
  reachable) completes `_kalshi_side` without the truncation warning and the
  candidates merge is append-only as before (diff `nonsports_candidates.yaml`:
  no existing entry modified).

## Phase 7 — political lane: parse each book once

Evidence: `arb/lanes/political.py:16-45` vs `48-85` — `book_quote_rows` and
`scan_market` independently recompute best-ask/best-bid/size from the same
`books` dict every poll (two passes, duplicated `min`/list-scan logic, two
places for the next bug to hide).

- Failing test first: golden test capturing current outputs of BOTH functions
  on the existing fixture (all fields), then refactor must keep it green.
- Fix: extract one `_best(book) -> (ask, bid, size)` helper used by both, or
  merge into a single pass returning `(rows, opportunity)`; keep whichever
  yields the smaller diff. Field-for-field identical output including
  `fee_cost`, `depth_usd`, `venue_market_id`, note strings, and the
  `arb_stale_warn` REVIEW flip. No Decimal→float anywhere (rule 5).
- Acceptance: `pytest -q tests/test_lanes.py` green; golden test proves
  byte-identical rows.

---

## Operator checklist (not code; do alongside the phases)

1. **Restart the supervisor** after Phases 1–2 land (parent process predates
   the last five commits; children already run current code at each spawn).
   Quiet window; `touch data/stop-all`, wait for clean halt, remove flag,
   relaunch. [operator]
2. **PREREGISTRATION amendment (dated)** recording the PM/ANJ outage window
   from 2026-07-16: lane kill-gates and flag-rate metrics must exclude
   blocked-venue days, and the open resolution-settled bet with a PM leg hits
   `reconcile_give_up_days: 14` → UNRESOLVABLE around 2026-07-30 — decide
   before the timer does. DRAFT the wording; the operator signs and commits
   it. [drafted by agent, operator approves]
3. **`db_backup.dest` to a different volume/synced folder** (§13.3 — the
   startup warning is correct; one disk loses ledger + backups). Config-only
   change once the operator names the destination. [operator]
4. Network options for the ANJ block (alt resolver / VPN / Kalshi-only) are a
   legal/operator judgment — NOT addressed by any phase here. [operator]

## Explicitly not doing (YAGNI, recorded so it isn't re-litigated)

- Candidates-YAML incremental writes (S5): 683 KB round-trip is fine at 257
  pairs; revisit at thousands.
- Gamma `condition_ids` batching: blocked on live verification (see Phase 5).
- Any change to `shape_relation` recall (PM rules text into shape input):
  recall-loss-only, safe direction, and it would reset `_RULES_SHA` — banned
  by the DO-NOT-TOUCH list until a deliberate, dated prompt iteration.
