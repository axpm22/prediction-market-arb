# 10 — Remove DIVERGENCE measurement (execute the existing prompt)

Executor: Opus 4.8. Requires prompt 09 (the predicate consolidation makes this deletion
safer). The full work order already exists in the repo:
**`REMOVE_DIVERGENCE_PROMPT.md` — read it in full and execute it as written.** It was
verified NOT yet implemented as of 2026-07-02 (audit: `detect_divergences` still live;
1,590 divergence opportunities and 177 zero-stake bet rows produced in one day on the
fresh DB).

## Hard rules (restated — the existing prompt binds them too)

- **`flat_bets` is append-only** — historical DIVERGENCE rows stay; `kind='DIVERGENCE'`
  remains a valid historical value; reports render them generically without crashing.
- **Persist every quote** — unchanged; divergences stay recomputable offline from `quotes`.
- **No Odds API credits in tests.**

## Deltas discovered by the audit (apply on top of the existing prompt's text)

1. The prompt predates the poll-kind dedup fix and Phase C/F work — when removing the
   divergence branches from `arb/tracker.py`/`arb/report.py`, do not disturb the poll-kind
   ownership logic or the markout/flicker hooks.
2. `REMOVE_DIVERGENCE_PROMPT.md` says to update
   `ArbModelV2_Plans/06 - Opus 4.8 Complete Build Guide.md` — still do that.
3. Also remove the DIVERGENCE card from any surface the UX prompt (11) hasn't consolidated
   yet, so 11 doesn't inherit dead panels.
4. The mean-reversion analysis (`Wave 0: DIVERGENCE mean-reversion`, committed Jun 23) is
   an offline reader of `quotes` — keep it working (it does not depend on live divergence
   detection; verify with its test).

## Acceptance

Exactly the existing prompt's acceptance list, plus: one fresh-DB day after restart shows
zero new DIVERGENCE opportunities/rows, and `pytest` green.
