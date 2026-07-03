# prompts/queue — run order, gates, parallelization

Produced by the 2026-07-02 master audit (see `MASTER_PLAN.md`, the plan these prompts
implement). Every prompt is self-contained (runnable cold in Claude Code from the repo
root, no chat context), restates the hard rules it binds, and names its own acceptance
commands. All prompts are calibrated for **Opus 4.8** as the executor; none of them
delegates to a smaller model. Where a prompt wraps an existing repo work order
(10, 11) it says so and adds only audit-discovered deltas.

## Run order and gates

| # | Prompt | Needs | Gate after? |
|---|--------|-------|-------------|
| 01 | `01-verify-commit-working-tree.md` | — | **STOP — review commits + pytest** |
| 02 | `02-settle-tripwire-and-invariants.md` | 01 | no (03 gates the stage) |
| 03 | `03-baseline-cutover-and-supervised-restart.md` | 01, 02 | **STOP — status table, clean report header** |
| 04 | `04-phase-a-regrade-run.md` | 01 | **STOP — EV lane verdict** |
| 05 | `05-fee-model-corrections.md` | 01 | **STOP — cost vectors re-frozen** |
| 06 | `06-phase-b-settlement-matrix.md` | 01–03 | no |
| 07 | `07-phase-c-markouts-live.md` | 01–03 | no |
| 08 | `08-phase-f-time-rigor.md` | 01–03 | stage gate: **one week of clean supervised data days** |
| 09 | `09-eligibility-predicate-position-key.md` | 01–03 (06–08 recommended) | **STOP — before/after report diff** |
| 10 | `10-remove-divergence.md` | 09 | no |
| 11 | `11-ux-ui-consistency.md` | 09, 10 | **STOP — end of Stage 3** |
| 12 | `12-negrisk-exhaustive-maker-shadow.md` | Stages 0–2 + 05 | per its own pre-registered gates |

## Parallelizable in separate worktrees

- **04 ∥ 05** (archive analysis vs fee plumbing — disjoint; 04 is read-only).
- **06 ∥ 07 ∥ 08** (settlement matrix / markouts / time rigor — disjoint modules).
- 04 may also run in parallel with 02/03 (read-only, archive-based).
- **Never parallelize 01, 03, or 09** — they touch the tree/process/money-path globally.

## Invariants that hold across every prompt

Paper trading only; no authenticated or order endpoints, ever. No Odds API credits in
tests or loops (fixtures only; log `x-requests-remaining` live, abort < 50). No invented
endpoints or fields (404 → stop and report). `Decimal` for all money math. `flat_bets`
append-only — corrections are annotations or new rows. Every threshold in `config.yaml`.
Persist every quote. Cross-venue is REVIEW-only unless whitelisted (and after 06, only
with a green settlement-matrix section). Decision rules are recorded in
`PREREGISTRATION_V2.md` *before* their data arrives; when one fires, the trigger numbers
are recorded next to the action. If `data/stop-all` is present, only prompt 03 (final
step) may remove it.
