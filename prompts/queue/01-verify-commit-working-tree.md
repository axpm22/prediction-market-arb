# 01 — Verify and commit the dirty working tree (nothing else starts until this is done)

Read `CLAUDE.md`, `SPEC.md`, and `MASTER_PLAN.md` §1–§2 first. Executor: Opus 4.8.

## Hard rules (restated, binding here)

- **Paper trading only.** Never call authenticated or order-placement endpoints.
- **Never spend Odds API credits in tests or loops.** All tests run against
  `tests/fixtures/`. Log `x-requests-remaining` on any live call; abort below 50.
- **`flat_bets` is append-only.** No UPDATE/DELETE anywhere in this task.
- **Money math uses `Decimal`, never float.**
- **No magic numbers** — thresholds live in `config.yaml`.
- **`data/stop-all` stays in place for this entire prompt.** Nothing restarts here.

## Context (verified 2026-07-02 by the audit)

HEAD is `dd7854a` but the tree is dirty. Uncommitted work includes, at minimum:

- `arb/tracker.py` — the poll-interleaving dedup fix (absent-close only within the owning
  poll cadence; `_poll_kind`/`_poll_kinds`) from `prompts/fix_poll_interleaving_dedup.md`.
- `arb/settlement.py` — never settle a game whose `leg_start` is in the future (the
  settled-rebook-loop root fix from `FIX_SETTLED_REBOOK_LOOP_PROMPT.md` #4-adjacent).
- `arb/nonsports_ai.py` — MODEL_FOUNDATIONS Phase E (rules-hash verdict expiry:
  `rules_sha`, `verdict_is_current`, latest-verdict-wins cache read).
- Untracked new modules: `arb/heartbeat.py` (Phase D), `arb/settlement_matrix.py`
  (Phase B), `arb/experimental/analysis/regrade.py` (Phase A),
  `arb/experimental/analysis/markout.py` (Phase C),
  `arb/experimental/analysis/cadence.py` (Phase F), plus `tests/test_heartbeat.py`,
  `tests/test_markout.py`, `tests/test_regrade.py`, `tests/test_settlement_matrix.py`,
  `tests/test_devig_regrade.py`, and diffs in `arb/cli.py`, `arb/config.py`,
  `arb/arbitrage.py`, `arb/matching.py`, `tests/test_settlement.py`, `tests/test_tracker.py`,
  `tests/test_trusted_gate.py`, `config.yaml` (supervisor block).

This project has been burned twice by fixes sitting in the tree while an old process kept
betting. The 2026-07-02 daemon ran from this dirty tree (the `heartbeats` row proves it).
None of this work has a recorded green `pytest` run.

## Task

1. `git stash list` / inspect for anything else pending. Read every diff fully before
   committing anything — you are the verifier, not a rubber stamp.
2. Run the **full** `pytest`. If red: fix only what the failing test proves broken, or move
   the offending uncommitted module to a branch (`git stash` / feature branch) and record
   that in the commit message. Do not weaken a test to get to green. Do not delete the SPEC
   §8/§9 vectors or any T-* test.
3. Commit in reviewable slices, each with its acceptance evidence pasted into the commit
   message (foundations-spec cross-cutting rule):
   a. interleaving dedup fix (+ its regression tests) — cite
      `prompts/fix_poll_interleaving_dedup.md`;
   b. settlement future-start guard (+ tests);
   c. Phase D heartbeat/supervisor/status (+ tests) — include the `config.yaml`
      `supervisor:` block;
   d. Phase A regrade module (+ tests) — code only; the *run* is prompt 04;
   e. Phase B settlement-matrix module (+ test) — content/enforcement is prompt 06;
   f. Phase C markout module (+ test) — the persist hook is prompt 07;
   g. Phase E rules-hash verdict expiry (+ tests);
   h. Phase F cadence module;
   i. anything remaining that belongs together (docs, prompts, config comments).
4. For each slice, run the narrowest meaningful acceptance command offline (fixtures/read-
   only DB) and paste output — e.g. `python -m arb status` for (c),
   `python -m arb regrade --help` for (d). Read-only against `data/arb.sqlite` is fine;
   `file:data/arb.sqlite?mode=ro` for anything analytical.
5. Leave the tree **clean** (`git status` empty apart from git-ignored files) and record
   the final HEAD hash.

## Tests / acceptance (show all output)

1. `pytest` — full suite green on the final HEAD.
2. `git status --short` — empty.
3. `git log --oneline -12` — the slices above visible.
4. `python -m arb report` — runs against the live DB read path without error (no schema
   drift introduced).

## Out of scope

- No restart, no stop-flag removal (prompt 03).
- No new features, no running `regrade`/markout backfills (prompts 04/07).
- No DB writes of any kind.
- No fee/config value changes (prompt 05).

**STOP for review after this prompt.** Show the commit list and the pytest tail.
