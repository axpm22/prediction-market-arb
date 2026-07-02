# Prompt for Claude Code — remove DIVERGENCE measurement

Copy everything below into Claude Code, run from the repo root.

---

Read `CLAUDE.md` and `SPEC.md` first. Task: **stop detecting and recording DIVERGENCE opportunities.** They can never be bet (both legs are the same outcome — there is no lock and no settle path), they add ~3,400 zero-stake rows of ledger noise, and nothing is lost by removing them: hard rule #7 still persists every quote, so divergences can always be recomputed offline from the `quotes` table if ever wanted.

## Scope — remove the live measurement

1. `arb/arbitrage.py` — delete `detect_divergences()` and its helpers that nothing else uses. **Careful:** `same_outcome_spread`, `start_drift_secs`, `leg_skew_secs`, and `status_for_legs` are shared with ARB detection and `split_match_suspects` feeds the `suspect_matches` sanity gate — keep everything ARB still calls. Only the divergence-specific assembly goes.
2. `arb/cli.py` (and any daemon/watch wiring) — remove the `detect_divergences` call, the divergence threshold plumbing, and stop passing DIVERGENCE opportunities to `record_tracker_poll`.
3. `arb/tracker.py`, `arb/report.py`, `status.sh` — remove DIVERGENCE-only branches (e.g. the zero-stake special case in `_opportunity_stake`, divergence report sections/cards). Historical rows must still render harmlessly wherever generic kind-grouping touches them — do not special-case them into crashes.
4. `arb/exposure.py` — the `kind != 'DIVERGENCE'` filter can stay (it is now vacuous but guards historical rows).
5. `arb/experimental/webserver.py` + dashboard — drop the DIVERGENCE card/panel.
6. `config.yaml` — remove the divergence threshold key(s); leave a one-line comment pointing at this removal.
7. Tests — delete divergence-only test files; update any shared fixtures/tests that assert divergence output. Do not weaken any ARB/matching test to make this pass.

## Do NOT

- Do not delete or edit any existing `flat_bets` rows (append-only, hard rule #9) — history stays.
- Do not touch the schema; `kind='DIVERGENCE'` remains a valid historical value.
- Do not remove the match-suspect sanity gates — they protect ARB matching and predate divergence.
- Do not remove quote persistence anywhere.

## Acceptance

- `pytest` green.
- `python -m arb watch --once --dry-run` runs end-to-end and flags zero DIVERGENCE.
- `python -m arb report` and `report --daily` still render on the existing DB (historical DIVERGENCE rows grouped generically, no crash).
- `grep -rn "detect_divergences" arb/` returns nothing.

Also update `ArbModelV2_Plans/06 - Opus 4.8 Complete Build Guide.md`: delete `detect_divergence.py` from the layout and drop DIVERGENCE from the `kind` list, with a one-line note ("divergence dropped — unbettable; recompute from quotes offline if needed").
