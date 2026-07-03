# 11 — UX/UI consistency (execute the existing prompt, post-predicate)

Executor: Opus 4.8. Requires prompts 09 and 10 (one predicate to consume; no dead
divergence panels to migrate). The full work order already exists in the repo:
**`UX_UI_CONSISTENCY_PROMPT.md` — read it in full and execute it as written.** Verified NOT
yet implemented as of 2026-07-02 (audit: no shared `arb/metrics.py`; the three surfaces
still compute numbers independently; `webserver.py` monolith intact).

## Hard rules (restated — the existing prompt binds them too)

- **Read-only surfaces:** no writes to `flat_bets` or any ledger table; no new
  dependencies; modules under ~300 lines.
- **No Odds API credits in tests.**

## Deltas discovered by the audit (apply on top of the existing prompt's text)

1. The shared metrics module must consume `arb/eligibility.py` (prompt 09) for every
   clean/review/invalid split — no surface re-derives "clean" with its own SQL. The lane
   labels come from the predicate's vocabulary.
2. New numbers exist since the prompt was written: `pnl_markout` (prompt 07), cadence
   p50/p95 and start-grades (prompt 08), the rolling Brier line (prompt 04), and the
   Data Quality / annotations exclusions. All flow through the same metrics module — one
   computation path each.
3. The agreement test (CLI = dashboard.json = /api/state per lane) should include an
   annotated-row fixture proving all three surfaces exclude annotations identically —
   the audit could not verify the report surfaces honor annotations uniformly
   (PURGE_LOOP_PNL's step 3 was marked unverified).

## Acceptance

Exactly the existing prompt's deliverables (discrepancy list first — show it before
rewriting), plus `pytest` green including the three-surface agreement test with annotated
rows.
