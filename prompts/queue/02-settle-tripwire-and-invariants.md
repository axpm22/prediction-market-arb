# 02 — Settle tripwire + `arb check-invariants` (the incident's missing insurance)

Read `CLAUDE.md`, `SPEC.md`, `FIX_SETTLED_REBOOK_LOOP_PROMPT.md` (steps 3 and "future-
mistake insurance"), and `prompts/incident_recovery_master.md` Step 2 first.
Executor: Opus 4.8. Requires prompt 01 complete (clean tree, green suite).

## Hard rules (restated, binding here)

- **Paper trading only.** No authenticated or order-placement endpoints.
- **`flat_bets` is append-only** — the tripwire annotates via the immutable annotations
  table; zero UPDATE/DELETE. If you find yourself writing one, stop.
- **No Odds API credits in tests**; fixtures only.
- **No magic numbers** — `settle_tripwire_secs` and `tracker.max_reappearances` live in
  `config.yaml`.
- **Money math `Decimal`.**
- `data/stop-all` stays in place.

## Context

On 2026-07-02 the tracker booked-and-instantly-settled the same finished game up to 24
times (fingerprint `c2f39d1b…`; `settled_at − placed_at` ≈ 0.4 s). The root causes are
fixed and committed (in-play refusal at the tracker boundary; settled fingerprints
terminal; settlement future-start guard; poll-kind dedup), and the junk rows are annotated
(`settled-rebook-loop`, `dedup-interleave-duplicate` — see the `annotations` table). What
was specified but **never built** is the generic runtime insurance that catches the *next*
variant of this family:

1. the **instant-settle tripwire** in the settle path, and
2. the **`arb check-invariants`** command.

The one-shot detector `arb/experimental/analysis/loop_audit.py` exists — reuse its
detection logic where sensible, but the tripwire must run inside every settle cycle, not as
a manual audit.

## Task

### 1. Instant-settle tripwire (FIX_SETTLED_REBOOK_LOOP #3, verbatim intent)

Any row where `settled_at − placed_at` < one poll interval means the outcome was knowable
at placement — the bet was never a forecast. In the settlement path: the settlement still
writes (append-only), but the row is auto-annotated `settle-tripwire` (annotations table,
same transaction), excluded from every P&L surface, logged loudly, and its fingerprint is
blacklisted from re-booking for the rest of its event's lifetime. Config:
`settle_tripwire_secs` (default = `watch_interval_secs`). This is a tripwire, not a filter
— it must catch stale venue books, backfilled scores, and clock skew regardless of which
upstream guard failed.

### 2. `python -m arb check-invariants`

Scans the ledger, prints violations, zero output = silent. The five checks (from
FIX_SETTLED_REBOOK_LOOP, unchanged):

1. no fingerprint has > 1 settled row;
2. no row has `placed_at >= event start_utc` (where start is known);
3. no row settled faster than the poll interval without a tripwire/`instant-settle`
   annotation;
4. no reappearance chain longer than `tracker.max_reappearances` (config, default 10)
   in 24 h;
5. every settled row's P&L is Decimal-clean (no float drift).

Run it at the end of every settle cycle; violations log CRITICAL but never block
settlement (observability, not a new failure mode).

## Tests (fixtures only)

1. A bet settling 1 s after placement → annotated `settle-tripwire`, excluded from report
   cells, fingerprint not re-bookable; a bet settling 2 h after placement → untouched.
2. `check-invariants` on a synthetic ledger reproducing the 2026-07-02 pattern reports
   violations 1–3; on a clean ledger reports nothing.
3. Winner-loops and loser-loops treated identically (integrity property).
4. SPEC §8/§9 vectors untouched and passing.

## Acceptance (show output)

1. `pytest` green.
2. `python -m arb check-invariants` against the live DB (read-only is fine): reports
   exactly the already-annotated 2026-07-02 rows as historical violations, zero new ones.
3. Grep proof there is no UPDATE/DELETE on `flat_bets` in the diff.

## Out of scope

- No restart (prompt 03). No DB reset. No changes to loop_audit's historical annotations.
