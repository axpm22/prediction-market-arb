# 09 — One eligibility predicate + position_key (port SPEC_V2 F2/F3 onto V1)

Read `CLAUDE.md`, `SPEC.md`, `SPEC_V2.md` **§3 and §4 in full** (the design source), and
`MASTER_PLAN.md` issue #7 first. Executor: Opus 4.8. Requires prompts 01–03 and ideally
06–08 landed (this consolidates their lanes). Serial — do not run alongside other prompts;
it touches the money path everywhere.

## Hard rules (restated, binding here)

- **Paper trading only.**
- **`flat_bets` is append-only.** New columns arrive via the existing migration path;
  historical rows get lane values derived at read time, never rewritten.
- **Cross-venue is REVIEW-only** unless whitelisted; this prompt makes that rule
  *structurally* unavoidable instead of re-implemented per stage.
- **Money math `Decimal`; no magic numbers; fixtures-only tests; no Odds API credits.**

## Context — why this is the structural fix

V1's headline failure was architectural: safeguards set at detection were ignored by the
paths that book money — 314/314 settled V1 arbs were forbidden REVIEW arbs (A1). The same
family recurred on 2026-07-02: the in-play guard existed in `split_pregame` but not at the
tracker boundary until f131d84 patched that one spot. Today "clean" still means three
different things (trusted flag for arbs, `quality_flag IS NULL` for EV, status
strings) — the J1 dual-track foot-gun. SPEC_V2 §3 designed the cure for the V2 rebuild;
this prompt ports it onto the live V1 tree.

## Task

1. **`arb/eligibility.py`** — one pure function, one module:
   `eligibility(bet, cfg, now) -> Eligibility(eligible: bool, reason: str, lane: "clean"|"review"|"invalid")`,
   per SPEC_V2 §3.1:
   - trusted (flag or whitelist, per-market-type) AND start known AND
     `placed_at < start_utc` AND `depth_basis == "real"` → `clean`;
   - `placed_at >= start_utc` → `invalid` (never settles into any lane);
   - otherwise → `review` with the first failing clause as `reason`
     (`review-untrusted`, `start-unknown`, `placed-after-start`, `depth-unknown`).
2. **Every money-touching stage imports and calls it** — detection, tracker (fill), settle,
   reconcile, capacity, exposure, report-clean-aggregates (SPEC_V2 §3.2 table). Delete the
   local re-implementations they currently carry (the trusted checks in
   `settlement.py`/`arb_sizing.py`/`capacity.py`, the tracker's in-play refusal becomes a
   call into the same predicate, etc.). Keep behavior identical for the shipped config —
   this is a consolidation, not a rules change; any behavioral delta must be listed
   explicitly in the commit message.
3. **`position_key` and `venue_pair`** as real columns (SPEC_V2 §4):
   `position_key = (sport, event_key, outcome, market_type, line)`; every aggregate dedups
   by it; a re-flag of an active position updates quote history, never creates a new
   position; the reappearance rule is unchanged but reappearances share the position_key
   for economic aggregates. `event_key` gains the doubleheader `game_index` disambiguator
   if not already present (check — doubleheader clustering was delivered in June).
4. **Frozen tests** ported from SPEC_V2 §3.3/§4.2 — T-ELIG-1 through T-ELIG-5 and
   T-POS-1/2. T-ELIG-5 is the architectural grep test: the eligibility logic exists in
   exactly one file (`grep -rn "def eligib\|trusted == 1\|trusted==1" arb/` → one module).
5. **Historical rows:** a read-time lane derivation for pre-existing rows (they lack
   `depth_basis` etc.) so reports stay renderable; label the derived lane `legacy-derived`.

## Definition of done (SPEC_V2 §17, verbatim intent)

A VERIFY pass confirms the same eligibility predicate object gates detection, fill, settle,
reconcile, capacity, and exposure, with a passing test (T-ELIG-1) that a REVIEW arb never
reaches clean/headline P&L.

## Tests

T-ELIG-1..5, T-POS-1..2 (fixtures), plus: full-suite green with zero changes to the SPEC
§8/§9 vectors; a before/after fixture-report diff proving headline numbers are unchanged
for the shipped config.

## Acceptance (show output)

1. `pytest` green; the grep test output pasted.
2. The before/after report diff (must be empty or fully explained line-by-line).
3. `python -m arb report` against the live DB renders with historical rows in
   `legacy-derived` lanes.

## Out of scope

- No detection-math changes, no new lanes, no whitelisting, no schema changes beyond the
  named columns. No V2 rebuild — this is V1 hardening.

**STOP for review after this prompt.**
