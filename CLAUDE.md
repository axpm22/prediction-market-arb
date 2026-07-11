# ArbitrageModel — hard rules (never violate)

These rules bind every phase, every module, every prompt. Copied verbatim from SPEC.md §4.

1. **Paper trading only.** Never call authenticated or order-placement endpoints. Only public, unauthenticated GETs/POSTs — POST solely for read-only batch quote endpoints verified in §6 (Polymarket `POST /books`) — plus The Odds API key as a query param for market data. (Wording amended 2026-07-10; substance unchanged — base.py enforced this reading since H7.)
2. **Never spend Odds API credits in tests or loops.** The free tier is 500 credits/month. All tests run against recorded fixtures in `tests/fixtures/`. Live calls happen only in `scan`/`settle` commands. Log the `x-requests-remaining` response header on every live call; warn below 100, abort the scan below 50.
3. **Do not invent API endpoints or fields.** Use only the endpoints listed in SPEC.md §6 (verified working June–July 2026). If one returns 404/4xx, stop and report — do not guess alternatives.
4. **No magic numbers.** Every threshold, fee rate, bankroll figure, and schedule time lives in `config.yaml`.
5. **Money math uses `Decimal`, never float**, in pricing, fees, stakes, and the ledger. Floats are permitted only for display and for statistical root-finding (de-vig power/Shin solvers) whose results are immediately re-quantized to 6-decimal `Decimal`.
6. **Cross-venue opportunities are REVIEW-only by default.** Displayed but never auto-"filled" into the clean lane unless the venue-pair + market type is whitelisted in `config.yaml` after the §18.2 gate. Settlement rules differing between venues is the main source of fake arbs.
7. **Persist every quote** observed during a scan to SQLite, even when no opportunity is found. This is the backtest data.
8. **The math test vectors (§7) are non-negotiable.** Implement them exactly and make them pass before writing any venue adapter that uses the math.
9. **`flat_bets` is append-only.** Never UPDATE, DELETE, or retro-edit rows — *including label/flag columns* (the V1 backfill script that mutated only `quality_flag` was still a critical violation, because it could un-quarantine rows). Corrections are annotation rows in an immutable `annotations` table or new bet rows. **Mechanism (v3.3):** `flat_bets` holds entry-time facts only; every fact that arrives after entry — confirmation price, lifetime close, settlement result, reconciliation — is an append-only `bet_events` row (§9, §10.3), and `metrics.py` derives `pnl_confirmed`/`result`/`settled_at` at read time. Nothing ever needs to write to an existing row, so the rule has no tension with settlement. Daily reports are always recomputed from the raw log. A CI grep-test asserts no `UPDATE flat_bets` / `DELETE FROM flat_bets` — nor `UPDATE bet_events` / `DELETE FROM bet_events` — string exists under `arb/`.
10. **SX Bet quotes the maker's side.** Taker cost = `1 - percentageOdds/1e20`. Unit-test this from a recorded fixture before the SX adapter touches the pipeline; inverting it silently flips every SX price.
11. **Non-sports matching is manual-first, Haiku-second.** AI-matched events are always `status=REVIEW`, tagged `ai_matched`, displayed and paper-tracked in a segregated lane, never in the headline, and never eligible for the trusted path without a human `nonsports_map.yaml` entry. Verdicts cache append-only with a per-day budget; a settlement mismatch writes a negative verdict and permanently bans the pair.
12. **One eligibility predicate.** A single module (`arb/eligibility.py`) decides `(eligible, reason, lane)` for every bet-shaped object; detection, tracker, settle, reconcile, capacity, exposure, and report all import it. A grep-test proves the logic exists in exactly one place. (New in v3 — the V1 dual-track foot-gun produced 314/314 forbidden settled arbs.)
13. **Nothing restarts on a dirty tree.** Loops run only under the supervisor, from a committed HEAD, after a green `pytest`. A fix that is "in the tree" but uncommitted does not exist.

## Workflow (binding, SPEC.md §16)

Reproduce first (quote the file:line or query output) → write the failing test → minimal implementation → full `pytest -q` → `arb report --daily` unchanged except where the change explicitly says otherwise → commit with evidence.

Implement phases strictly in order; each phase ends with `pytest` green plus its named acceptance command. Modules stay under ~300 lines. Money is `Decimal`, quantized ROUND_HALF_EVEN to 6 places at store/compare boundaries only.
