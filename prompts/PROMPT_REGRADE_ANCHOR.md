# Claude Code prompt — Pinnacle anchor + `arb regrade` (EV re-open test rig, v1, 2026-07-14)

You are Claude Code (Sonnet 5) working in the ArbitrageModel repo at the repo
root. Read `CLAUDE.md` (the 13 hard rules) and `PREREGISTRATION.md` §"EV lane
(frozen OFF, with a re-open trigger)" BEFORE touching anything; both bind
verbatim. Use the project venv: `.venv/bin/python -m pytest -q` (all tests
green at HEAD; keep them green).

**What this builds and why:** the EV lane is frozen by the Phase A verdict
(soft-book consensus lost to venue mids: Brier 0.238 vs 0.221/0.223, ECE 0.30,
venue side won 52.1%). The pre-registered re-open trigger needs two things
that don't exist yet: (A) the **Pinnacle single-anchor** consensus recorded
alongside the soft-book median, and (B) the **`arb regrade`** command — the
read-only paired-Brier re-scoring job (the CLI subparser exists; the handler
prints "not implemented yet"). You build both. You do NOT re-open EV; the
numbers do that later, or never.

---

## Part 0 — Constraints that bind every phase

1. **Rule 2 is the sharpest edge here.** The Odds API budget is 500
   credits/month. The anchor fetch may run ONLY inside `cmd_scan` (the one
   credit-spending command besides settle scores). No test, loop, or regrade
   path may ever construct a live Odds API call — tests use fixtures/fakes
   exclusively, and `arb regrade` reads ONLY the local DB plus the keyless
   MLB Stats API.
2. Rule 4: every new threshold is a `config.yaml` key mirrored in
   `arb/config.py` SCHEMA (strict both ways — the loader errors on unknown AND
   missing keys, so config.yaml and SCHEMA must change together). Reuse
   existing keys where they fit (`ev.clv_window_mins`, `devig_method`,
   `consensus_anchor.*`) instead of minting near-duplicates.
3. Rule 5: money/probability math in `Decimal`; floats only inside
   statistical root-finding/bootstrap internals, re-quantized `q6` at
   store/compare boundaries.
4. Rule 9: `arb regrade` is **read-only against the ledger**. It must not
   INSERT/UPDATE/DELETE any table. Its only outputs are stdout and
   `reports/regrade-<date>.md`. Add a test that proves ledger row counts are
   identical before/after a regrade run.
5. Rule 13 + workflow (§16): committed HEAD, then per change: quote the
   failing behavior, write the failing test, minimal implementation, full
   `pytest -q`, commit with evidence. Keep modules under ~300 lines.
6. **Frozen means frozen:** do not touch `ev_enabled`, any PREREGISTRATION
   threshold, `arb/eligibility.py`, or the amendment log except for the ONE
   dated amendment specified in Part C.

---

## Part A — Pinnacle anchor recording (inside `cmd_scan` only)

When `cfg["consensus_anchor"]["enabled"]` is true, `cmd_scan` additionally
fetches, per sport in `consensus_sports`, ONE extra Odds API call with
`regions=<consensus_anchor.region>` and `markets=<consensus_anchor.markets>`
(h2h only — that is a budget decision, config comment explains). From the
response keep ONLY rows whose book equals `consensus_anchor.book`; discard the
rest (they are soft EU books, not the hypothesis).

- De-vig the anchor's two-way odds with the configured `devig_method` and
  store rows in the existing `consensus` and `consensus_books` tables with
  `method` = `f"anchor:{devig_method}"` — same event-key canonicalization as
  the median path (`compute_consensus` internals are reusable; prefer reuse
  over a parallel implementation).
- Credit math, stated in a comment and asserted nowhere live: us h2h+totals
  (2) + eu h2h anchor (1) = 3 credits × sports × 4 scans/day ≈ 360/month for
  one sport — inside budget. The existing `x-requests-remaining`
  logging/warn/abort already covers the anchor call because it rides `_get`.
- Tests (fixtures only): an eu-shaped fixture (extend
  `tests/fixtures/oddsapi_h2h.json` pattern with a `pinnacle` book) proving:
  only the anchor book survives filtering; rows land with `method`
  `anchor:power`; a scan with `consensus_anchor.enabled: false` makes zero
  anchor calls (count calls on a monkeypatched fetch).

## Part B — `arb regrade` (the §18.3 re-scoring job)

New module `arb/regrade.py` (<300 lines) + `cmd_regrade` wiring. It computes,
for EACH consensus definition present in the DB (`method` not like `anchor:%`
= soft median; `method` like `anchor:%` = the anchor) over WINNER rows of
settled MLB events:

1. **Realized outcomes** from the MLB Stats API finals (keyless, the
   project's settlement oracle), matched through the same normalization the
   settle path uses (`matching.load_aliases`/`normalize_team`), honoring
   doubleheader `:dhN` suffixes — two same-day games must never blend. An
   event without an unambiguous final is EXCLUDED and counted in the report,
   never guessed (rule-3 ethos).
2. **Two comparison bases**, each producing a paired series over events:
   - `as-stored`: each consensus row's `p_hat` vs the venue mid ((cost+bid)/2
     of the venue quote for the same event/outcome) nearest in time to the
     consensus row's `computed_at`, admissible only within
     `regrade.match_tolerance_secs`; no quote in tolerance → pair excluded
     and counted.
   - `closing`: the LAST consensus row and the LAST venue mid strictly before
     event start, both within `ev.clv_window_mins` of start — this is the
     staleness-artifact control (trap 13) the pre-registration demands.
3. **Paired Brier**: per event-outcome, `(p_consensus − y)² − (p_venue − y)²`;
   report the mean difference per basis per method with a **cluster bootstrap
   by event_key** 95% CI (`regrade.bootstrap_n` resamples, seeded by
   `regrade.bootstrap_seed` — determinism is pre-registered honesty, and
   tests need it). Negative mean with CI excluding 0 = consensus better.
   Resample CLUSTERS (event keys) with replacement, never individual rows.
4. **EV-subset win rate** (re-open condition 2): the subset where
   `|p_consensus − venue mid| ≥ ev.e_min` at the `as-stored` basis; a "win"
   = the outcome landed on the side consensus favored against the venue.
   Report win rate with a cluster-bootstrap CI; the trigger needs CI > 0.5.
5. Fewer than `regrade.min_events` scored events per (method, basis) cell →
   that cell prints `INSUFFICIENT n=<k>` and makes NO claim.

Output: rich table to stdout + `reports/regrade-<today>.md` with every number
above plus exclusion counts (no-final, out-of-tolerance, thin cells). Honor
`--regrade-db PATH` (score an alternate/archive DB read-only; default
`args.db`).

New config block (mirror in SCHEMA): `regrade: { match_tolerance_secs: 90,
bootstrap_n: 2000, bootstrap_seed: 20260714, min_events: 50 }`.

Tests (all synthetic/fixtures, no network): hand-computed paired-Brier vector
(3 events, known mids/outcomes → exact mean diff); cluster bootstrap
determinism (same seed → same CI) and cluster-not-row resampling (a dominant
event's rows move together); tolerance exclusion; doubleheader non-blending;
the read-only guarantee (row counts unchanged); INSUFFICIENT rendering below
`min_events`.

## Part C — Flip the anchor on (last, gated)

Only after Parts A+B are green and committed: set
`consensus_anchor.enabled: true` in config.yaml and append ONE dated
amendment row to the PREREGISTRATION.md log: anchor recording enabled
<date>, ~360 credits/month projected, re-open evaluation to run only when
`regrade.min_events` is met per cell; no thresholds changed. Nothing else in
that file moves.

## Acceptance (run these, paste output in the final commit)

1. `.venv/bin/python -m pytest -q` — green, no network.
2. `.venv/bin/python -m arb regrade` against the live DB — runs read-only,
   prints both methods × both bases (expected today: mostly INSUFFICIENT —
   that is correct, not a failure), writes `reports/regrade-<date>.md`.
3. `git log --oneline` shows one commit per part with failing-test-first
   evidence in the messages.

Out of scope, do not touch: `ev_enabled`, eligibility, tracker booking paths,
the non-sports lanes, anything under `data/` except nothing — regrade writes
no data files.
