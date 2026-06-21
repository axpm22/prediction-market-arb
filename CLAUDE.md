# ArbitrageProject — Claude Code instructions

Read `SPEC.md` fully before writing any code. It is the single source of truth. Implement phases in order; do not start a phase until the previous phase's acceptance checks pass.

## Hard rules (never violate)

1. **Paper trading only.** Never call authenticated or order-placement endpoints. Only public, unauthenticated GETs (plus The Odds API key as a query param for market data).
2. **Never spend Odds API credits in tests or loops.** The free tier is 500 credits/month. All tests run against recorded fixtures in `tests/fixtures/`. Live calls happen only in `scan`/`settle` commands. Log the `x-requests-remaining` response header on every live call and abort the scan if it drops below 50.
3. **Do not invent API endpoints or fields.** Use only the endpoints listed in SPEC.md §3 (verified working June 2026). If one returns 404/4xx, stop and report — do not guess alternatives.
4. **No magic numbers.** Every threshold, fee rate, bankroll figure, and schedule time lives in `config.yaml`.
5. **Money math uses `Decimal`, never float**, in pricing, fees, stakes, and the ledger. Floats are fine for display only.
6. **Cross-venue opportunities are REVIEW-only by default.** They are displayed but never auto-"filled" in the paper ledger unless the venue-pair + market type is whitelisted in `config.yaml` (settlement rules can differ between venues; this is the main source of fake arbs).
7. **Persist every quote** observed during a scan to SQLite, even if no opportunity is found — this becomes backtest data.
8. The unit-test vectors in SPEC.md §8 and §9 are non-negotiable: implement them exactly and make them pass before writing any venue adapter that uses the math.
9. **The flat-stake tracker (`flat_bets`) is append-only.** Never update, delete, or retro-edit rows; daily reports are always recomputed from the raw log. All tracker P&L is net of fees and spread crossing.
10. **SX Bet quotes the maker's side.** Taker cost = `1 − percentageOdds/1e20`. Unit-test this from a recorded fixture before the SX adapter touches the pipeline.

## Workflow

- Python 3.11+, dependencies pinned in `requirements.txt`. Use `requests`, `rich`, `rapidfuzz`, `schedule`, `python-dotenv`, `pyyaml`, `pytest`. Stdlib `sqlite3` (no ORM).
- After each phase: run `pytest`, then run the phase's acceptance command from SPEC.md §9 and show the output.
- Keep modules under ~300 lines; split if larger.
- `ODDS_API_KEY` comes from `.env` (git-ignored). Create `.env.example`.
