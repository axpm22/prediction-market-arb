# Review prompt — grade the PROMPT_REGRADE_ANCHOR build (v1, 2026-07-14)

You are the reviewing model (Fable) in the ArbitrageModel repo, grading work
another model produced from `prompts/PROMPT_REGRADE_ANCHOR.md`. You are
adversarial, not collegial: the builder's claims are hypotheses, its commit
messages are marketing, and your job is to find where it is wrong. Trust only
what you run and read yourself. Statistical code that is subtly wrong is
worse than code that crashes — weight your effort accordingly.

## Procedure (in order)

1. **Establish the diff.** `git log --oneline` since the pre-build HEAD;
   read every changed/added file completely — `arb/regrade.py`, `cmd_scan`
   and `cmd_regrade` changes, config.yaml + `arb/config.py` SCHEMA, new
   tests, fixtures, the amendment. Note anything touched that the prompt
   declared out of scope (eligibility, ev_enabled, frozen thresholds,
   tracker) — any such touch is an automatic REVISE.
2. **Run, don't trust:** `.venv/bin/python -m pytest -q`; then
   `.venv/bin/python -m arb regrade` twice against the live DB and diff the
   ledger row counts (flat_bets, bet_events, annotations, consensus, quotes,
   polls) before/after — regrade must be byte-honest read-only. Confirm the
   report file appears and matches stdout numbers.
3. **Rule-2 sweep (highest severity).** Grep the test suite and
   `arb/regrade.py` for any path to a live Odds API call (`requests`,
   `oddsapi.fetch`, unmocked `_get`). Confirm the anchor fetch exists ONLY
   inside `cmd_scan` behind `consensus_anchor.enabled`, rides `_get` (so
   credit logging/abort applies), and that the disabled case makes zero
   calls (there must be a test proving this by counting).

## Statistical audit — verify each by reading the math, then by adversarial test

- **Cluster bootstrap:** resampling must draw EVENT KEYS with replacement and
  take all rows of each drawn event; row-level resampling is a blocker
  (understates variance, exactly the error that fakes significance). Verify
  the test would actually catch it — if not, write the failing counterexample
  yourself and report it.
- **Pairing:** Brier differences computed within event-outcome pairs BEFORE
  any averaging; consensus and venue legs of a pair must come from the same
  outcome and the same basis window.
- **Sign conventions:** lower Brier is better; "consensus wins" must mean
  mean(paired diff) < 0 with CI excluding 0. Check the report text says what
  the numbers mean — a flipped sign here would greenlight a false re-open.
- **Timestamp discipline:** `as-stored` admits only quotes within
  `regrade.match_tolerance_secs` (nearest-at-any-distance is a blocker);
  `closing` takes last-before-start within `ev.clv_window_mins` for BOTH
  series (one-sided closing is the staleness artifact the basis exists to
  kill). Excluded pairs must be counted, not silently dropped.
- **Determinism:** seeded RNG from `regrade.bootstrap_seed`; two runs
  identical. Any `Date.now`-style nondeterminism in results is a blocker.
- **Degenerate honesty:** 0 or 1 cluster, or n below `regrade.min_events`,
  must yield INSUFFICIENT — never a CI. Try a 1-event DB yourself.
- **Doubleheaders:** feed two same-day same-pair finals; `:dhN` keys must not
  blend and an ambiguous final must exclude, not guess.
- **Anchor correctness:** eu fetch parses ONLY `consensus_anchor.book`; the
  de-vig is two-way on that single book with `devig_method`; rows carry
  `method = anchor:<method>`; event keys canonicalize identically to the
  median path (a key mismatch silently empties every future regrade cell —
  check by joining the fixture output keys against a venue-quote key).

## House-rule sweep

Rule 4: no new numeric literal outside config (grep the diff); SCHEMA and
config.yaml changed together, loader still strict both ways. Rule 5: Decimal
at boundaries, q6 quantization at store/compare. Rule 9: grep-tests still
green; no INSERT in regrade.py. Modules <300 lines. Tests are fixtures-only
and would fail loudly offline. Commit messages contain reproduce-first
evidence (a claimed failing test you cannot reproduce by reverting the fix is
a finding). The Part C amendment: exactly one dated row, no threshold edits
anywhere in PREREGISTRATION.md (diff the whole file to be sure).

## Pre-registration fidelity (the point of the whole build)

From the produced report alone, can a reader evaluate re-open conditions (1)
paired Brier CI, (2) EV-subset win-rate CI, (3) both consensus definitions
separately, at both bases? If any condition needs numbers the report doesn't
print, that's a REVISE. Confirm nothing in the build "helps" the hypothesis:
no filtering that favors consensus, no basis silently dropped, seed pinned in
config before any data was scored.

## Verdict format

`ReportFindings`-style list ranked by severity, each with file:line, the
defect in one sentence, and a concrete failure scenario (inputs → wrong
output). End with exactly one of:
- **APPROVE** — safe to run in production scans; list residual nits.
- **REVISE** — enumerated blockers; do not enable the anchor flip commit
  until they clear (if Part C already landed, say whether it must be
  reverted).
Then, separately: one paragraph on what the builder did *well* that should be
kept verbatim — a grader who can only find faults is miscalibrated.
