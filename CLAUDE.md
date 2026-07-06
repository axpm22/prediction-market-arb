# prediction-market-arb — Claude Code instructions

Read `docs/SPEC.md` fully before writing any code. It is the single source of truth. Implement phases in order; do not start a phase until the previous phase's acceptance checks pass.

## Hard rules (never violate)

1. **Paper trading only.** Never call authenticated or order-placement endpoints. Only public, unauthenticated GETs (plus The Odds API key as a query param for market data).
2. **Never spend Odds API credits in tests or loops.** The free tier is 500 credits/month. All tests run against recorded fixtures in `tests/fixtures/`. Live calls happen only in `scan`/`settle` commands. Log the `x-requests-remaining` response header on every live call and abort the scan if it drops below 50.
3. **Do not invent API endpoints or fields.** Use only the endpoints listed in docs/SPEC.md §3 (verified working June 2026). If one returns 404/4xx, stop and report — do not guess alternatives.
4. **No magic numbers.** Every threshold, fee rate, bankroll figure, and schedule time lives in `config.yaml`.
5. **Money math uses `Decimal`, never float**, in pricing, fees, stakes, and the ledger. Floats are fine for display only.
6. **Cross-venue opportunities are REVIEW-only by default.** They are displayed but never auto-"filled" in the paper ledger unless the venue-pair + market type is whitelisted in `config.yaml` (settlement rules can differ between venues; this is the main source of fake arbs).
7. **Persist every quote** observed during a scan to SQLite, even if no opportunity is found — this becomes backtest data.
8. The unit-test vectors in docs/SPEC.md §8 and §9 are non-negotiable: implement them exactly and make them pass before writing any venue adapter that uses the math.
9. **The flat-stake tracker (`flat_bets`) is append-only.** Never update, delete, or retro-edit rows; daily reports are always recomputed from the raw log. All tracker P&L is net of fees and spread crossing.
10. **SX Bet quotes the maker's side.** Taker cost = `1 − percentageOdds/1e20`. Unit-test this from a recorded fixture before the SX adapter touches the pipeline.
11. **Non-sports matching is manual-first, Haiku-second** (amendment, docs/nonsports_ai_matcher.md): with `ai_matcher.enabled`, Haiku decides pair equivalence from both venues' resolution rules. Single-market series use the one-shot path; multi-market series (weather/macro) go through the deterministic strike matcher (`arb/experimental/strike_match.py`) so only strike-aligned pairs reach Haiku. AI-matched events are always `status=REVIEW`, tagged `ai_matched`, and **displayed AND paper-tracked in a segregated lane** when `ai_matcher.paper_fill` — never in the headline, never mixed into the clean/trusted lane. They are still **never eligible for the trusted/whitelisted (real-money) path without a human `nonsports_map.yaml` entry**. Human map entries always win; verdicts cache append-only at market level with a per-day budget. If a paper-booked AI pair's two venues settle inconsistently, the row is flagged `settlement_mismatch`, the pair is written to a negative verdict, and it is never re-bet.

## Workflow

- Python 3.11+, dependencies pinned in `requirements.txt`. Use `requests`, `rich`, `rapidfuzz`, `schedule`, `python-dotenv`, `pyyaml`, `pytest`, `anthropic` (AI matcher only — rule 11; never for market data). Stdlib `sqlite3` (no ORM).
- After each phase: run `pytest`, then run the phase's acceptance command from docs/SPEC.md §9 and show the output.
- Keep modules under ~300 lines; split if larger.
- `ODDS_API_KEY` comes from `.env` (git-ignored). Create `.env.example`.
