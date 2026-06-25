# Prompt for Claude Code — fix the audited model issues

Paste everything below the line into Claude Code, running from the repo root.

---

You are working in the ArbitrageModel repo. A prior audit produced six issue docs in the repo
root. Your job is to **resolve every CRITICAL and HIGH issue, and the documented MEDIUM/LOW
issues where the fix is contained**, deeply and thoroughly — with verification before and after
each fix, and a regression test for every change.

## Read first (do not skip, in this order)
1. `CLAUDE.md` — the hard rules. They override everything below. In particular: paper trading
   only / public unauthenticated GETs only; never spend Odds API credits in tests or loops (all
   tests use `tests/fixtures/`); `Decimal` for all money/price/fee/stake/ledger math (float only
   for display); `flat_bets` is append-only (never UPDATE/DELETE — corrections are new rows);
   every threshold/fee/cap lives in `config.yaml` (no magic numbers); cross-venue arbs are
   REVIEW-only unless whitelisted in `config.trusted_pairs`; modules stay under ~300 lines.
2. `SPEC.md` — the source of truth for behavior and the unit-test vectors.
3. The audit docs, which contain Symptom → Cause (file:line) → Evidence → Fix logic for each
   item: `CRITICAL_AND_HIGH.md` (the master list + status), `ARB_WEAKNESSES.md`,
   `ARB_LADDER_AND_FRICTIONS_SPEC.md`, `MODEL_ISSUES_SWEEP.md`, `MODEL_ISSUES_SWEEP_2.md`,
   `EV_WEAKNESSES.md`.

`CRITICAL_AND_HIGH.md` is your worklist and ordering. The other docs are the detailed spec for
each fix — follow their "Fix logic" sections; they were written to respect the hard rules.

## Working method — apply to EVERY issue
1. **Reproduce first.** Before changing code, confirm the bug against the live DB
   (`data/arb.sqlite`, open read-only: `file:data/arb.sqlite?mode=ro&immutable=1`) or a fixture,
   and quote the number/row that proves it. If you cannot reproduce it, say so and do not "fix"
   it blindly — note the discrepancy and move on.
2. **Write a failing test first** (pytest, against `tests/fixtures/`, never live endpoints).
   Then implement the minimal fix that makes it pass. Then run the full suite.
3. **Keep money math in `Decimal`**, additions append-only, thresholds in `config.yaml`
   (add new keys with sensible defaults and document them in `.env.example`/comments).
4. **Public reads only.** New resolvers/fetchers use unauthenticated GETs with the existing
   rate-limited session pattern; never call order-placement or authenticated endpoints.
5. **Prove the fix.** Re-run the reproduction query/command and show the before/after. For
   data-shape changes, show a row diff.
6. **Update the status block** in the relevant doc (mark the item FIXED with the test name) as
   you close each one.
7. Do **not** change a money/strategy *policy* decision on your own — specifically, do **not**
   populate `config.trusted_pairs` (whitelisting a venue-pair is the user's call). Build the
   machinery so that whitelisting *would* work, and leave it empty.

## Phase order (from CRITICAL_AND_HIGH.md → "Recommended order")
Work one phase at a time. After each phase: run `pytest`, run the phase's acceptance command,
and show output. Do not start a phase until the previous one is green.

**Phase 1 — A2 follow-through (reconcile actually verifies the book).**
Build `polymarket_leg_result(leg)` and `sx_leg_result(leg)` (public settled-resolution reads),
mirroring `kalshi_leg_result`'s `WON|LOST|VOID|None` contract. Compose resolvers by venue in
`reconcile_settlements` so a bet is only "unresolved" if *its own* leg's venue resolver returns
None (stop skipping the whole bet because one venue lacks a resolver). Until a bet is reconciled,
label its realized P&L `provisional` wherever it surfaces. Tests with recorded settled fixtures
for each venue, incl. a VOID and a venue-disagreement (CORRECTED) case.

**Phase 2 — N1 (CLV go/no-go gate is biased).**
Make the no-vig CLV (`grade_clv_novig`) the default for the verdict; keep the fee-loaded
`grade_clv` available behind a flag for comparison. Add the `quality_flag IS NULL` filter to
`_ev_flags` so quarantined flags don't enter the edge-proof sample. Test: a fixture where the
fee-loaded metric says "not proven" but the no-vig metric is positive → default verdict flips.

**Phase 3 — H1 (EV-exit ledger corruption).**
In `ev_exit._append_exit_row`, compute and store optimistic and confirmed P&L *separately*
(confirmed from `confirmed_entry_cost`; NULL → confirmed P&L NULL, never the optimistic value),
mirroring `settlement._append_settled_row`. Also route EV-exit's Kalshi/Polymarket book reads
through the rate-limited adapter session (H2). Test: an EXITED row from a never-confirmed bet has
NULL `pnl_confirmed`.

**Phase 4 — E4 + E5 (silent EV wrong-number bugs).**
E4: on an Odds-API abort in `run_scan`, do not overwrite good consensus with a partial-book one
(skip the consensus write, or only write events whose full book was fetched this poll). E5: in
`split_pregame`, treat `start_utc is None` as non-actionable (research lane), never pregame.
Tests for both.

**Phase 5 — C0 (neg-risk fee curve).**
In `negrisk.find_negrisk_arb`, price fees with the Polymarket **linear** model
(`all_in_fee_cost(..., basis="linear")`) on both ask and bid sides, not the Kalshi quadratic.
Test: a basket that is break-even under linear fees must not flag as an arb.

**Phase 6 — S1 (per-poll full-table scan).**
Make `record_tracker_poll` (and the `settlement_health`/`exposure` active-bet reads) query only
candidate-active fingerprints via the existing indexes instead of `SELECT * FROM flat_bets`.
Preserve exact behavior (the derived lifecycle must be identical). Add a test asserting identical
active-set output on a fixture DB, and note the row-scan reduction.

**Phase 7 — B1/B3/B4 (real ladder capture) — follow `ARB_LADDER_AND_FRICTIONS_SPEC.md` §0.**
Add the Polymarket per-token CLOB `/book` multi-level ladder fetch for arb legs; define and
enforce the shared `depth_usd` = taker-$-notional invariant across Kalshi/SX/Polymarket with a
calibration unit test per venue against a recorded book; make `run_arb_capacity` return
`INSUFFICIENT_DEPTH_DATA` while the captured-ladder fraction is below a new config threshold.
Note: historical ladders can't be backfilled — do not fabricate them; the report stays UNKNOWN
until live captures accumulate. (This also un-blocks **G1**; with no trusted settled arbs the
sizing model correctly returns flat $1 — make that empty-population state explicit in its output
rather than silently returning $1.)

**Phase 8 — I1/I2 (scorer zero-drag).**
Do not change the scoring policy blindly. First, report per-segment confirmation rate alongside
the score (the 42% NULL-confirmed rate is the real finding). For arbs, score on the locked
(realized-at-entry) P&L since the EV "confirmed" concept doesn't map onto a lock. Add tests; if
any threshold changes, justify it from the data and put it in `config.yaml`.

**Phase 9 — C/D frictions & remaining MEDIUM items** from `ARB_LADDER_AND_FRICTIONS_SPEC.md`
(C1 simultaneity haircut, C3 portfolio cap binding detection, C4 holding-period to settlement,
D1 Kalshi start fallback, D2 doubleheader clustering, D3 SX winning-bet fee) and the SWEEP
medium/low items where the fix is contained. Each behind a config flag where it changes
detection behavior; default-off only if it's a measurement change, default-on if it's a
correctness fix. Move the hardcoded magic numbers in `arb_sizing.py` (EDGE_CAPS,
market_depth_fraction, buckets) into `config.yaml` (**G2**).

## After all phases — verification pass (static review can't find these)
Run, against fixtures (never live credits): the full `pytest` suite; `arb settle --fixtures`,
then `arb reconcile`; `arb scores`; `arb clv --novig` (and without, to show the gap); the
capacity report. Then write a short `FIXES_REPORT.md` summarizing, per issue: the reproduction
before, the change, the test that locks it, and the after. Call out anything you could not
verify or deliberately left as a policy decision for the user (e.g. `trusted_pairs`).

## Guardrails / definition of done
- All hard rules in `CLAUDE.md` upheld; no authenticated/live calls in tests or loops.
- Every fixed issue has a named regression test; `pytest` is green.
- No `flat_bets` mutations; corrections are appended rows.
- New thresholds live in `config.yaml`; no new magic numbers.
- Each audit doc's status block updated to reflect what's now FIXED.
- `trusted_pairs` left empty (machinery ready, policy left to the user).
- If you discover a new issue mid-work, log it in the relevant doc with the same
  Symptom→Cause→Evidence→Fix structure before fixing it.

Work carefully and incrementally; prefer many small, tested commits over one large change.
