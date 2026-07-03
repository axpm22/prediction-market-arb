# 05 — Fee-model corrections: Polymarket quadratic, Kalshi re-verify, SX parlay fee

Read `CLAUDE.md`, `SPEC.md` §3, `MASTER_PLAN.md` §1.4 (items 2–4), and
`arb/venues/slippage.py` (`leg_fee_cost`, `FEE_BASIS`) first. Executor: Opus 4.8.
Requires prompt 01. Independent of 04 (parallel worktree OK).

## Hard rules (restated, binding here)

- **No invented endpoints or fields.** Fee facts come from the venues' published pages,
  cited by URL and read-date in config comments.
- **No magic numbers** — every rate is a `config.yaml` key.
- **Money math `Decimal`.**
- **Append-only comparability:** changing `effective_cost` re-freezes cost vectors. This is
  a **versioned change**: stored historical quotes keep their as-observed `fee_cost`; only
  newly fetched quotes use the new model; the change is recorded as a dated amendment in
  `PREREGISTRATION_V2.md` and a config comment. Never retro-edit stored rows.
- **No Odds API credits in tests.** The (optional) live verification reads are free venue
  GETs only.

## Context — what the audit found (2026-07-02, cited)

1. **Polymarket is now quadratic, per category.** The repo prices Polymarket **linear**
   (`FEE_BASIS = {"polymarket": "linear"}`; SPEC_V2 §8.6/C0 mandate linear). Polymarket's
   current docs (https://docs.polymarket.com/trading/fees, read 2026-07-02; structure
   effective 2026-03-30) publish `fee = C × feeRate × p × (1−p)` with taker rates:
   sports 0.03, crypto 0.07, finance/politics/tech/mentions 0.04,
   economics/culture/weather/other 0.05, **geopolitics 0**; makers never pay. At p=0.50 the
   linear model charges 1.5¢/$1-payout where the true fee is 0.75¢ — the model
   **double-charges Polymarket sports legs at mid prices**, understating every
   Polymarket-leg edge by more than most real arb edges. Fee parameters are also queryable
   per market (`feesEnabled`, `getClobMarketInfo` → `fd = {r, e, to}`).
2. **Kalshi:** 7¢ × C × P(1−P) taker confirmed, maker = 25% of taker on fee-enabled
   markets, but a **new fee schedule took effect 2026-06-29**
   (https://kalshi.com/docs/kalshi-fee-schedule.pdf) with per-market exceptions. Config's
   `kalshi.fee_rate: 0.07` has `confirmed_on: 2026-06-10` — the X2 staleness gate will
   quarantine all Kalshi quotes around Jul 10 unless re-confirmed.
3. **SX:** published schedule (https://help.sx.bet/en/articles/2798017-sx-bet-fees,
   https://learn.sx.bet/trading/fees, read 2026-07-02): **0% on single bets; 5% only on
   winning parlays**. This supports the shipped `sx.fee_rate: 0.0` for singles and
   supersedes SPEC_V2 §6's `win_fee_rate: 0.05` for singles — but the 5% winning-parlay fee
   MUST be modeled in the PARLAY lane before that lane ever activates.

## Task

1. **Polymarket quadratic per-category fees.** Change `FEE_BASIS["polymarket"]` to
   `"quadratic"` and plumb per-category rates (config:
   `polymarket: {sports_fee_rate: 0.03, crypto_fee_rate: 0.07, politics_fee_rate: 0.04,
   econ_weather_fee_rate: 0.05, geopolitics_fee_rate: 0.0, fee_rate: 0.05}` — keep existing
   key names where they exist) through the single `leg_fee_cost` implementation so the
   adapters AND the flash verifier stay identical (K1/K2 rule: there is exactly one
   implementation of "what does this leg cost"). The nonsports/weather lane must pick the
   weather rate; negrisk baskets use the same quadratic model **both sides** — update the
   C0 test's expected values accordingly and note in the test docstring that C0's
   "linear" rule was correct for the pre-2026-03-30 schedule and is superseded by the
   published quadratic schedule (cite URL + date).
2. **Where a market carries `feesEnabled`/fee params in the payload the adapter already
   fetches**, prefer the per-market value over the category default (no new endpoints; use
   only fields present in recorded fixtures — record one fresh fixture to prove the field).
3. **Kalshi:** fetch the 2026-06-29 fee-schedule PDF once (public), confirm or amend
   `kalshi.fee_rate`, bump `confirmed_on`, and note any per-series exceptions relevant to
   the series we scan in a config comment. If the PDF contradicts 0.07 for our series, the
   new value is the versioned change.
4. **SX:** keep singles at 0 with the citation + `confirmed_on` bump; add
   `parlay.sx_win_fee_rate: 0.05` consumed by the (dormant) parlay lane with a test that a
   winning parlay's P&L is net of it.
5. **Offline re-read of history under corrected fees** (read-only, archive): re-price the
   archived NEGRISK baskets and a recorded day of cross-venue arb detection under the new
   Polymarket curve. Report the deltas (edges that flip sign, by class and venue-pair).

## Decision rule (pre-committed)

If corrected fees flip any historical class from null to positive (most plausible:
Polymarket-heavy negrisk baskets), that class gets a **fresh pre-registered forward test**
(append the hypothesis to `PREREGISTRATION_V2.md` with n-threshold per its §4 before any
new data counts). It does NOT retroactively claim the old data proved edge. If nothing
flips, record the honest null: fees were not the binding constraint.

## Tests (fixtures only)

1. Fee vectors: Polymarket sports p=0.50 → fee 0.0075/$1-payout (100-share table: $0.75);
   crypto p=0.50 → 0.0175; politics p=0.25 → 0.0075; geopolitics → 0. Kalshi vector
   unchanged (0.017500 at P=0.50). SX single → 0; SX parlay win → 5% of winnings.
2. Verifier/adapter parity (K1/K2): the flash verifier's effective cost equals the
   adapter's at the same raw price, per venue, from fixtures.
3. Negrisk break-even basket under the new curve does not flag (updated C0 test).
4. SPEC §8/§9 vectors: any that hard-code the old Polymarket linear fee are updated as part
   of the versioned change — with the SPEC.md amendment in the same commit (SPEC stays the
   single source of truth).

## Acceptance (show output)

1. `pytest` green.
2. The archive re-read report (step 5) pasted, with the decision-rule outcome recorded.
3. `PREREGISTRATION_V2.md` + `SPEC.md` amendments in the diff; config citations present.

## Out of scope

- No parlay-lane activation; no EV-lane changes (prompt 04 owns that verdict); no maker-fee
  modeling (improvement lane, prompt 12); no live scanning.

**STOP for review after this prompt** (it re-freezes cost vectors — the user should see the
deltas before new data accumulates on them).
