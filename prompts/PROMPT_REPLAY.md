# Claude Code prompt — `arb replay`: offline policy replay over stored quotes (v1, 2026-07-27)

You are Claude Code (Opus 5) working in the ArbitrageModel repo. Read
`CLAUDE.md`, SPEC.md §4, and `PREREGISTRATION.md` before touching anything;
all three bind verbatim.

**What this builds.** A read-only command, `arb replay --from DATE [--to DATE]
[--kind watch|live]`, that re-runs matching-group reconstruction + `detect()`
over quotes already persisted in SQLite (SPEC rule 7 data), and prints what
the current code/config would have flagged versus what was flagged at the
time. Purpose: any change to thresholds, detection, or the negRisk/covering
proofs becomes testable against weeks of real data in seconds, with zero API
credits and zero live HTTP.

**What this prompt is NOT.** It changes no lane, no threshold, no eligibility
logic, and writes nothing to the database — not a row, not a table, not an
index. Replay output is stdout (and optionally a file under `reports/replay/`,
gitignored). If a replay result motivates a rule change, that change is its
own dated PREREGISTRATION.md amendment later; nothing here implements one.

**Precondition (rule 13):** committed HEAD, green `pytest -q`. One commit per
phase, failing-test-first evidence in the message (workflow §16). Fixtures
only in tests (rule 2). Money `Decimal` (rule 5). `arb/eligibility.py`
untouched (rule 12). New module `arb/replay.py` stays under ~300 lines.

**Database facts — verify each before relying on it (workflow §16: quote
file:line or query output in the commit message):**

- The live DB is `data/arb.sqlite` (~4 GB, WAL, actively written by the
  supervisor). Replay MUST open it read-only via URI
  (`file:data/arb.sqlite?mode=ro`) with a busy timeout, and MUST stream
  per-poll (`idx_quotes_poll`) — never load the whole quotes table.
  NEVER run replay tests against this file; tests build a small temp DB
  through `arb/schema.py`.
- `quotes` rows do NOT store raw `home`/`away`/`venue_side`/`gamepk`
  (`_quote_row`, `arb/cycle.py:22`). Therefore replay does NOT call
  `build_groups()` — it reconstructs groups directly from the persisted
  `event_key` column (stamped by matching at scan time, `arb/matching.py:182`;
  format `sport:date:away:home[:dhN]`). Grouping by stored key is exact by
  construction and sidesteps the live-MLB-schedule dependency entirely.
- The `outcome` column is already the normalized label (matching normalizes
  before persist, `arb/matching.py:136`). Do not re-normalize.
- `detect(cfg, groups, now=..., mode=...)` (`arb/arbitrage.py:78`) already
  accepts an explicit clock and mode. Replay passes the poll's `started_at`
  as `now`, and maps poll kind → mode (`watch`→`pregame`, `live`→`live`).
  Confirm with a grep that `detect` reads no other wall-clock source.
- Confirm `venue_side`/`gamepk` are unused inside `detect()` and
  `_suspect_reason()` (grep). If anything in the detect path reads a field
  the quotes table lacks, STOP and report the gap — do not synthesize values.
- Stored `opportunities` rows carry `poll_id` + `fingerprint` (`_opp_row`,
  `arb/cycle.py:37`) — that is the join for the diff in Phase R3.

---

## Phase R1 — `quote_from_row`: the inverse of `_quote_row`

- In `arb/replay.py`, implement `quote_from_row(row) -> Quote`: Decimal
  casts for money fields, ISO-parse for datetimes, `home`/`away` parsed from
  `event_key`, `venue_side=""` (reconcile-only field; per the verification
  above it never reaches detect).
- Failing test first: round-trip — build a representative `Quote` (include a
  NO-side quote, a None-line WINNER, a TOTAL with line, None depth/bid),
  pass through `_quote_row` then `quote_from_row`, assert every
  detect-relevant field equal, `Decimal` types intact (rule 5).
- Acceptance: `pytest -q tests/test_replay.py`.

## Phase R2 — the replay loop and CLI

- `replay(cfg, con, from_date, to_date, kind) -> ReplaySummary`: iterate
  polls of that kind in `[from, to]` (dates are America/New_York days,
  consistent with `event_key` dating); for each poll, load its quote rows,
  rebuild `Quote`s, group by `event_key` into `MatchedGroup`s (Event
  reconstructed from key + stored `start_utc`/`start_grade`; recompute
  `suspect` via the existing `_suspect_reason` if its inputs are all
  persisted — verified in R1 — else carry `suspect=None` and say so in the
  output header), then `detect(cfg, groups, now=poll_started_at, mode=...)`.
- Aggregate per day and total: polls replayed, quotes, opps by kind/status,
  reject counts by reason. Sums of money/edge are `Decimal`.
- CLI: wire `arb replay --from --to --kind` following the existing command
  pattern in `arb/cli.py`/`arb/commands.py`. Read-only connection as above.
  No new config keys unless a real threshold appears (rule 4) — none is
  expected.
- Tests: temp DB seeded through `arb/schema.py` with a handful of quote rows
  derived from existing test fixtures — include one poll that yields a known
  opp and one that yields a known reject. Assert counts. No live DB, no HTTP.
- Acceptance: `pytest -q` green, then
  `python -m arb replay --from <yesterday> --to <yesterday>` against the live
  DB completes read-only and prints a summary (paste it in the commit
  message). Run `ls -la data/` before and after and state that no `-wal`
  ownership or new file appeared beyond what the live writer produces.

## Phase R3 — diff vs. what was persisted at the time

- For each replayed poll, compare replayed opp fingerprints against stored
  `opportunities` rows for that `poll_id`. Report three sets: `both`,
  `replay-only` (current code/config would newly flag), `stored-only`
  (current code/config no longer flags). Per-set counts plus up to N=20
  example fingerprints with event_key and edge.
- This diff is the entire point of the tool: after any detection change, the
  delta over real history is the review artifact. Label the output clearly:
  differences mean *the code/config changed*, not that history changed.
- Test: seed temp DB with stored opps that partially overlap a replay run;
  assert the three sets.
- Acceptance: `pytest -q` green; a live read-only run over the last 3 days
  pasted into the commit message.

## Out of scope (state, don't build)

- negRisk and political lanes (`arb/negrisk.py:112`, `arb/lanes/political.py:57`)
  consume raw books/market dicts, not `Quote` rows — replay v1 is the sports
  ARB/XSPREAD detect path only. Note this limitation in the replay output
  header so nobody mistakes a quiet negRisk day for a replayed one.
- Tracker/eligibility/settlement replay: detection only. No `flat_bets`
  interaction of any kind (rule 9 stays untouched by construction).
- Any persistence of replay results into the live DB.
