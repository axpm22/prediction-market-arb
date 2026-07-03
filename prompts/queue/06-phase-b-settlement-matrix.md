# 06 — Phase B: fill the settlement matrix, enforce it, retro-check it

Read `CLAUDE.md`, `SPEC.md`, `MODEL_FOUNDATIONS_FIX_SPEC.md` **Phase B in full**,
`SETTLEMENT_RUNBOOK.md` §10 (the MMA parity precedent), and `arb/settlement_matrix.py`
first. Executor: Opus 4.8. Requires prompts 01–03. Parallel-safe with 07/08 (separate
worktrees; disjoint modules).

## Hard rules (restated, binding here)

- **Cross-venue opportunities are REVIEW-only** unless the venue-pair + market type is
  whitelisted — and after this prompt, a whitelist entry is only *effective* if the
  sport's matrix section is fully green. `trusted_pairs` ships and stays empty unless the
  user adds an entry.
- **No invented endpoints.** Rulebook facts come from each venue's published rules pages /
  per-market rules fields already fetched (Kalshi `rules_primary`/`rules_secondary`, Gamma
  `description`, SX betting rules at help.sx.bet). Cite section strings per cell.
- **`flat_bets` append-only**; reconcile mismatches write new incident rows, never edits.
- **No Odds API credits in tests.**

## Context

Settlement mismatch is the project's named #1 fake-arb source, still handled by an empty
whitelist and judgment. The Phase B module (`arb/settlement_matrix.py` + 
`data/settlement_matrix.yaml` + `tests/test_settlement_matrix.py`) was committed by prompt
01, but the matrix content and enforcement are unverified. The precedent that proves the
method: the 2026-07-02 MMA winner-market comparison (SETTLEMENT_RUNBOOK §10) found Kalshi
settles a cancelled bout to a discretionary "fair price" while Polymarket voids 50/50 —
verdict REVIEW-only, no whitelist.

## Task

1. **Fill `data/settlement_matrix.yaml`** for the sports in scope, per the foundations-spec
   scenario lists:
   - tennis: retirement mid-match, retirement before start, walkover, suspended/resumed
     next day
   - baseball_mlb: extra innings, suspended game, postponement, doubleheader game-2
     mixups, mercy/shortened
   - soccer: extra-time scope, penalties scope, abandoned match, venue change
   - mma: no-contest, draw, weight-miss rebooking, card reshuffle (seed from
     SETTLEMENT_RUNBOOK §10 — the cancellation cell is already known ❌)
   Cell values: `SETTLES | VOIDS | CONDITIONAL:<rule> | UNKNOWN`, each with the rulebook
   citation string. Unfilled = `UNKNOWN` — never guess a cell. Where a rule can only be
   read from a live per-market rules field, one free public GET per venue is fine; record
   the response as a fixture.
2. **Enforcement:** a cross-venue opportunity is eligible for `trusted` only if every
   scenario row for its sport is non-UNKNOWN and venue-agreeing for the venues involved.
   Any disagreeing cell → permanent REVIEW + note naming the scenario
   (`settle-mismatch:tennis-retirement`). `trusted_pairs` entries that point at a non-green
   section produce a loud startup warning and are ignored.
3. **Retro-check:** extend `arb reconcile` so that for every settled cross-venue bet it
   verifies both venues resolved the same direction; a mismatch writes a
   `settlement_mismatch` incident row (append-only) and flips the matrix cell to observed
   reality (the yaml edit is a cited, dated change — lived settlements are the only ground
   truth that counts).

## Decision rule (pre-committed; foundations spec, verbatim)

No cross-venue sport gets `trusted` until its matrix section is green. If reconcile ever
finds a mismatch on a trusted pair, the pair reverts to REVIEW immediately and stays there
until the matrix cell is amended with the rulebook citation explaining it.

## Tests (fixtures only)

1. Green section + whitelist entry → trusted; one UNKNOWN cell → REVIEW with the scenario
   named; one disagreeing cell → permanent REVIEW.
2. Reconcile fixture where the two venues resolve differently → `settlement_mismatch`
   incident row + pair reverts.
3. A `trusted_pairs` entry pointing at a non-green section is ignored with a warning.

## Acceptance (show output)

1. `pytest` green.
2. The filled matrix printed (or the yaml shown) with citations — every cell either sourced
   or UNKNOWN.
3. A dry-run of the trusted gate over the current config (`trusted_pairs: []`) showing
   nothing changes behavior today, plus the counterfactual: which sports WOULD be
   whitelistable if the user chose to (i.e., which sections are green).

## Out of scope

- Do not add any `trusted_pairs` entry — whitelisting is the user's policy call.
- Golf scenarios only if golf is actually scanned (check config); otherwise leave the
  section absent, not UNKNOWN-spammed.
