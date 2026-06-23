# Where the ARB path is weak — full audit + fix logic (June 2026)

Scope: the arbitrage classes end to end — `arbitrage.py` (cross-venue detection),
`capacity.py` + `experimental/analysis/arb_capacity.py` (sizing/capacity), `settlement.py`
(ARB settle), `reconcile.py` (venue cross-check), `paper.py` (fill), `negrisk.py` (basket
arb), `btc_arb.py` (crypto arb), `exposure.py` (portfolio cap), and the venue adapters'
ladder/depth/fee capture (`venues/kalshi.py`, `polymarket.py`, `sx.py`). Companion to
`EV_WEAKNESSES.md`.

> **Status update (June 2026) — CRITICAL trio (A1/A2/A3) + B2 dedup fixed.**
> - **A1 fixed:** REVIEW (trusted=0) arbs now settle into a flagged lane (`quality_flag='REVIEW'`,
>   `settlement.py:_append_arb_resolved_row`) that every clean aggregation already excludes; the
>   sizing model (`arb_sizing.py`) and capacity report now filter `trusted=1`, so unverified
>   cross-venue locks no longer train sizing or inflate capacity. Only a whitelisted venue-pair
>   (`config.trusted_pairs` → status OK → trusted=1) settles into clean/headline P&L.
> - **A2 fixed:** `reconcile_settlements` now runs automatically after every live `settle`
>   (`cli.py`), matching the daemon's `run_settle_job`. The Kalshi resolver closes the loop;
>   Polymarket/SX legs are skipped until their resolvers land (still a known gap).
> - **A3 fixed:** `_append_arb_resolved_row` now applies the pre-event timing guard — an arb whose
>   row was placed at/after the event start is recorded `INVALID_IN_PLAY` and books no locked P&L
>   (same gate the EV path uses).
> - **B2 partly fixed:** the capacity report now dedups by fingerprint and filters `trusted=1`,
>   removing the re-observation (4–5×) and REVIEW inflation from the summed-profit figure.
> - **Still open:** B1/B3 (real multi-level ladder capture — needs a live re-capture pass; the
>   break-even/summed-profit numbers stay "top-of-book only, no slippage" until then) and the
>   C/D frictions (simultaneity haircut, portfolio cap, holding period, observability nits).
> Tests: `tests/test_settlement.py` (REVIEW lane, trusted-clean, in-play invalidation),
> `tests/test_capacity.py` (fingerprint dedup + trusted filter).

Every claim is checked against the live `data/arb.sqlite`. **Headline finding:** detection
is careful and honest, but the safeguards detection sets (`REVIEW`, `settlement-rule-review`,
depth/skew tags) are *not enforced by the path that books money*. The settled arb book is
exactly the population the project's hard rule #6 says must never be counted, and the one
module meant to verify it (`reconcile.py`) is a structural no-op for 100% of the book.

Evidence snapshot (ARB rows unless noted):

```
ARB status counts          : OPEN 347, CONFIRMED 5, SETTLED 314 (+ annotation rows)
distinct fingerprints / 661 OPEN+SETTLED rows : 148            # 4.5x re-observation inflation
distinct OPEN fingerprints : 145 cross-venue, 3 single-venue
trusted among distinct OPEN : 146 REVIEW (trusted=0), 2 trusted
venue pairs                : kalshi+polymarket 358, polymarket+sx 288, single 15
captured ladders           : 0 of 661       (capacity runs the single-level fallback)
SETTLED arb rows           : 314 — ALL trusted=0 (REVIEW)
placed AFTER event date    : 85 of 314
RECONCILED/CORRECTED rows  : 0   (and the only resolver is Kalshi-only — see A2)
other arb kinds present    : NEGRISK (rows present), ARB_BTC (0 rows here)
config: sx.fee_rate=0.0, devig=proportional, trusted_pairs=[], arb_size_by_ladder=false,
        slippage_model=flat, bankroll=1000, max_stake_pct=0.10
```

Each issue below has **Symptom → Cause (code) → Evidence → Fix logic**. The fix logic is
written to respect the project's hard rules: append-only ledger (#9), `Decimal` money math
(#5), public reads only (#1), and "cross-venue is REVIEW-only unless whitelisted" (#6).

---

## CRITICAL — the ledger books arbs it is forbidden to book

### A1. Settlement ignores REVIEW status — rule #6 enforced at fill, not at settle

**Symptom:** the entire settled arb book is cross-venue REVIEW. 314/314 settled arbs have
`trusted=0`.

**Cause:** `detect_true_arbs` correctly marks un-whitelisted cross-venue legs `REVIEW`
(`arbitrage.py:76`) and `fill_opportunity` refuses non-`OK` opportunities (`paper.py:15`).
But the tracker records every opportunity regardless of status, and the settle queries select
open arbs with **no trust/status filter**:

```sql
-- settlement.py:164  _open_arb_bets  (and _open_ev_bets, settle_backfill_mlb)
SELECT * FROM flat_bets b WHERE b.kind='ARB' AND b.status='OPEN' AND NOT EXISTS (…terminal…)
```

**Evidence:** 314 SETTLED, all `trusted=0`; venue pairs are 100% cross-venue.

**Fix logic:**
- Define one settle-eligibility predicate, shared by fill and settle:
  `eligible = (status row trusted=1) OR (venue_pair ∈ config.trusted_pairs)`.
- In `_open_arb_bets` / `_open_ev_bets`, add `AND b.trusted = 1` (or the whitelist join).
- REVIEW arbs still settle, but into a **separate lane**: write the SETTLED row with
  `result='RESOLVED_REVIEW'` and a `review-not-actionable` note; never let it reach clean /
  headline P&L. (Append-only: this is a new row, not a mutation.)
- Add a regression test: settle a fixture containing one OK and one REVIEW arb → assert only
  the OK arb produces an actionable SETTLED row.

### A2. Reconciliation has never run — and *cannot* run for this book

**Symptom:** every settled arb's realized P&L is the copied provisional lock; 0 reconciled.

**Cause (two layers):**
1. `reconcile_settlements` is a manual CLI command (`cli.py:101,785`); nothing in the
   daemon calls it, so it has produced **0 rows**.
2. Worse: the only resolver wired in is `kalshi_leg_result`, which returns `None` for any
   non-Kalshi leg (`kalshi.py:249`). `reconcile_settlements` breaks out and **skips the whole
   bet** if any leg is unresolved (`reconcile.py:91-97`). The entire book is kalshi+polymarket
   and polymarket+sx — **every arb has a Polymarket or SX leg with no resolver** — so even if
   run, reconcile would skip 100% of arbs. It is a structural no-op.

**Evidence:** 0 RECONCILED/CORRECTED rows; venue pairs all contain Polymarket/SX; no
`polymarket_leg_result` / `sx_leg_result` exists.

**Fix logic:**
- Write `polymarket_leg_result(leg)` (read the conditionId/UMA resolution, public GET) and
  `sx_leg_result(leg)` (SX `/markets` settled status + the leg's outcome side). Both return
  `WON|LOST|VOID|None`, mirroring `kalshi_leg_result`.
- Compose resolvers by venue: `resolver = RESOLVERS[leg['venue']]`; only mark a bet
  unresolved if *its* venue resolver returns None.
- Run reconcile automatically right after each `settle` in the daemon; until a bet is
  reconciled, label its realized P&L `provisional` everywhere it surfaces (report + capacity),
  not just in a note.
- Keep the append-only correction path (`_append_recon_row`) — it's already correct; it just
  never gets data.

### A3. No settlement-timing guard — arbs settle even when placed after the event

**Symptom:** 85 of 314 settled arbs were created on a date *after* the event date.

**Cause:** `settle_sports` books any OPEN arb whose event has a final score, ignoring
`placed_at` vs event start/final time. A quote captured post-game is "locked" retroactively —
fake realized P&L from a stale read. ARBs realize at entry, so they're the most exposed.

**Evidence:** 85/314 `placed_at` date > event_key date.

**Fix logic (guard-spec #7, applied to arbs):**
- At settle time, parse `event_start = leg.start_utc` (or the event_key date) and compare to
  `placed_at`:
  - `placed_at > event_final` (or > start when final unknown) → append an
    `INVALID_AFTER_FINAL` row, `result='INVALID'`, P&L excluded from clean.
  - otherwise settle normally.
- This is also a *detection*-time guard (see C5): a bet that should never have opened post-start
  shouldn't reach settlement at all. Do both — defense in depth.

---

## HIGH — the capacity / sizing numbers are structurally fantasy

This is the source of "659 feasible, 0 ladder, median break-even ~$770, summed profit
~$72,209." It is right to distrust it.

### B1. Zero captured ladders → single-level fallback removes all slippage

**Symptom:** every capacity row is `depth`-limited; break-even ≈ top-of-book size; no arb is
ever `edge`-limited.

**Cause:** with no `ladder`, `_leg_ladder` falls back to one flat rung `[(cost+fee, depth_usd)]`
(`capacity.py:120`). On a single flat level, profit(N) = N·(1 − Σcost) is linear-increasing and
never crosses zero, so the binding limit is always "book ran out," never "edge gone." The
report **cannot** model price impact. The module prints this warning itself
(`arb_capacity.py:84`).

**Evidence:** 0/661 legs carry a ladder.

**Fix logic:**
- Treat `n_with_ladder == 0` as "capacity UNKNOWN," not "$72k." Have `run_arb_capacity` return
  `None`/`INSUFFICIENT_DEPTH_DATA` for break-even and profit until real ladders exist.
- Interim sizing rule while data is thin: cap stake at `min(leg depth) × safety_frac` AND
  never above `bankroll × max_stake_pct`, and haircut displayed depth (see B3).

### B2. "Summed profit $72,209" stacks three independent overstatements

**Cause:** (1) no slippage (B1); (2) `run_arb_capacity` sums over OPEN+SETTLED **rows**
(`arb_capacity.py:34`), but 661 rows are only **148 fingerprints** — same arb counted 4–5×;
(3) no `trusted` filter — ~99% of the sum is non-actionable REVIEW arbs.

**Fix logic:**
- Dedup before any aggregate: collapse to the **position key** (C2), keep one row per
  position (the best executable venue), then sum.
- Filter to `trusted=1` (or whitelisted) before summing "profit."
- Report capturable profit as a **floor with a captured-ladder fraction** displayed
  (`n_with_ladder / n_feasible`); refuse to print a headline profit while that fraction is 0.

### B3. depth_usd is not on a common basis across venues, and is trusted as fully takeable

**Symptom:** capacity compares leg depths that are computed three different ways.

**Cause:**
- Kalshi: `_depth_at_ask` multiplies an order-book size whose **units are undocumented** by the
  ask, and the docstring admits this under-/over-states depth (`kalshi.py:227`).
- SX: depth is a derived **taker $-notional** = `maker_stake · c/(1−c)` (`sx.py:209`).
- Polymarket: `price · size` of the single best ask, or — in the fallback — the **whole event's
  liquidity** stands in for one outcome's depth (`polymarket.py:112`), wildly overstating it.

Capacity (`min` across legs) then compares apples to oranges and trusts displayed size as
100% fillable (no queue position, no partial fill).

**Fix logic:**
- Normalize every adapter to emit `depth_usd` = "taker $-notional executable at this level,"
  documented per venue, and unit-test each against a recorded book.
- Never fall back to event-level liquidity for a single outcome's depth — emit `None`
  (unknown) so `capturable_lower_bound` correctly returns 0 rather than a fiction.
- Apply a fillable-fraction haircut (config knob, e.g. take ≤ 50% of displayed top-of-book).

### B4. Polymarket legs are *permanently* single-level — so capacity is capped for the whole book

**Symptom:** even after ladder capture is "on," Polymarket arbs can never be edge-limited.

**Cause:** the batch markets endpoint exposes top-of-book only; the adapter builds a 1-level
ladder and notes a real multi-level book "would need per-token /book fetches"
(`polymarket.py:117-120`). Since **every** arb in the book has a Polymarket leg, at least one
leg is always single-level → the binding (thinnest) leg's slippage is unmodelable.

**Fix logic:**
- Add the per-token CLOB `/book` fetch for Polymarket arb legs (the adapter already hits
  `/book` for the live EV path in `_attach_clob_books`; reuse it to build a full ladder).
- Until then, mark any arb whose binding leg is Polymarket as `depth-unmodeled` and exclude it
  from capacity headline numbers.

---

## HIGH — fee / edge mis-pricing that manufactures fake arbs

### C0. Neg-risk basket uses the WRONG fee curve for Polymarket

**Symptom:** basket convert/overshoot edges are systematically overstated.

**Cause:** `find_negrisk_arb` charges `fee = fee_rate · ask · (1 − ask)` — the **quadratic
(Kalshi) curve** (`negrisk.py:68`). But neg-risk markets are **Polymarket**, whose fee is
**linear** (`pricing.linear_fee_cost`, and the adapter prices Polymarket with `basis="linear"`).
`pricing.py:262` explicitly warns that applying Kalshi's `p(1−p)` to Polymarket *under-charges*
fees. Under-charged fees shrink `ask_sum`, so `Σ asks < 1` triggers more often → **fake convert
arbs** and inflated ROI on the one arb class that's otherwise the safest (same-oracle, single
venue).

**Evidence:** NEGRISK rows present; fee expression is quadratic while the venue is linear.

**Fix logic:**
- Replace the inline fee with `all_in_fee_cost(price, fee_rate, taker_buffer, basis="linear")`
  for both ask and bid sides, so neg-risk uses the same fee model as the adapter.
- Add a unit vector: a basket that is break-even under linear fees must NOT flag as an arb.

### C1. Simultaneity: a 60s leg-skew window is not "simultaneous"

**Cause:** `max_leg_skew_secs: 60` lets legs fetched a minute apart count as one lock
(`arbitrage.py:158`); `arb_stale_warn: 0.04` flags but still records. On fast books the two
prices were never live together.

**Fix logic:** tighten the actionable window (e.g. ≤ 10s) and treat skew as a continuous
**edge haircut** (decay edge by observed line-move per second per sport), not a binary tag.
Re-quote both legs at settle/confirm and require the lock to still hold.

### C2. No position key — one opinion becomes many tracked bets

**Cause:** the fingerprint includes venue+book+line (`models.py:88`), so the same economic
position on two venue pairs, or re-quoted at a slightly different line, is a different
fingerprint = a separate bet. There is no event/outcome position key.

**Evidence:** 661 rows → 148 fingerprints; the duplication directly inflates B2 and C3.

**Fix logic (guard-spec #4/#8):**
- Define `position_key = (sport, event_key, normalized_outcome, market_type, line)`.
- Before recording an actionable arb, if an active bet shares the position key, treat the new
  sighting as an **improved quote** for that position (update best venue), not a new bet.
- Aggregate capacity and exposure by position key, choosing the single best executable venue.

### C3. No portfolio / capital constraint binds detection or capacity

**Cause:** `exposure.py` exists and is a CLI command (`cli.py:117`), but it's manual, reads the
flat `stake_usd` only, and nothing in detection/capacity consults it. Capacity sums locked
profit as if every arb is funded simultaneously; bankroll is `$1000` at 10%/bet.

**Fix logic:** run `compute_exposure` inside the scan; refuse to promote a new actionable arb
when total or per-venue committed stake would exceed the cap; feed per-venue remaining capital
as the upper bound on `ladder_scaled_stake`.

### C4. Capital charge uses event start, not settlement, as the holding horizon

**Cause:** `is_economic` charges time-value to **event start** (`arbitrage.py:170`,
`pricing.py:325`), but cross-venue capital is locked until **both venues settle** — hours to
days after the game, longer for disputes. Holding period (and the `uneconomic` tag) is
understated.

**Fix logic:** estimate settlement time per venue (game end + venue settlement lag config),
charge capital to the later of the two; surface `$-days` (already computed in `exposure.py`).

### C5. No started/final guard at detection — arbs open on in-play or finished events

**Cause:** detection has no `start_utc <= now` reject; the `live` flag is informational. A leg
with a stale/missing start (see C6) or a past start can still form an "arb."

**Fix logic (guard-spec #1/#2):** at detection, drop any leg whose `start_utc <= now`
(route to a live lane if one exists); require a non-null `start_utc` to be actionable; if a
resolver says the event is final, record a `FINAL_EVENT_SKIPPED` diagnostic instead of an arb.

---

## MEDIUM — matching / adapter correctness that can fake or corrupt arbs

### D1. Kalshi start-time fallback is `close_time` — days after the game

**Symptom:** a Kalshi market whose ticker doesn't match the time regex gets a start time days
late, corrupting its event_key date, the started/final guards, and settlement bucketing.

**Cause:** `_start_from_ticker` returns None on a regex miss, and the adapter falls back to
`close_time`, which the code itself documents as "days AFTER the game"
(`kalshi.py:23,119-121`).

**Fix logic:** never fall back to `close_time` for `start_utc`. If the ticker time can't be
parsed, mark the quote `start-unknown` and make it non-actionable (research-only) rather than
inventing a start days off.

### D2. Doubleheaders collapse to one event_key

**Symptom:** two different MLB games (same teams, same day) share one event_key, so a leg from
game 1 can match a leg from game 2 → a fake cross-venue arb.

**Cause:** `event_key` intentionally has no sub-day bucket (`matching.py:43-51`) to avoid the
00:00 seam; it relies on the start-drift gate (30 min) to suspend mismatches and exact-start
settlement to resolve. But within detection, both games' quotes hash to the same key before the
gate runs.

**Fix logic:** keep the date-only key for matching, but within a market group, **cluster legs
by start time** (≤ ~2h) before pairing outcomes, so game-1 and game-2 quotes never combine.
The start-drift gate stays as a backstop.

### D3. SX fee is 0 in config — winning-bet fee unmodeled

**Cause:** `sx.fee_rate: 0.0` (config) with the note "fees only on winning bets." SX charges on
winning bets, which an arb always has (exactly one leg wins). Modeling SX taker cost with zero
fee **understates** the cost of the winning leg → overstates every polymarket+sx arb edge.

**Fix logic:** model SX's winning-bet fee as an expected cost on the leg (fee × P(win) or, for a
locked arb where one leg always wins, the full winning-leg fee amortized across the basket),
and stop pricing SX legs at zero friction. Verify the live SX fee schedule before trusting any
SX arb edge.

### D4. Depth-unknown arbs are still recorded as feasible

**Cause:** `capturable_lower_bound` returns 0 when any leg depth is None (`arbitrage.py:28`),
but the arb is still emitted and counted as `feasible` by capacity via the single-level
fallback (which needs only `cost`+`depth_usd`). So "feasible" overcounts arbs whose depth is
actually unknown.

**Fix logic:** make `feasible` in the capacity report require a real (captured-ladder or
known-depth, non-fallback) basis; count fallback-only arbs separately as `depth-unmodeled`.

---

## LOWER — observability / nits worth a test

### E1. Cent-rounding can break the lock but the row is still recorded

`equalized_arb_stakes` can produce a negative `guaranteed_floor` on small bankrolls or 3-way
markets; it's flagged `rounding-broke-lock` (`arbitrage.py:152`) but still recorded. Ensure
nothing downstream treats a negative-floor arb as real locked profit.

### E2. Rejected arbs leave no audit trail

Same-outcome-spread and start-drift over threshold `continue` silently in `detect_true_arbs`
(`arbitrage.py:136-139`), unlike the divergence path which records match-suspects. You lose the
record of what was rejected and why. **Fix:** write a diagnostic (suspect) row.

### E3. BTC arb has no settlement path

`btc_arb` / `btc_sweep` record `kind='ARB_BTC'` with `pnl_optimistic` booked at open, but no
settler resolves `ARB_BTC` (the sports settler handles `ARB`/`EV` only). 0 rows here today, but
if enabled these become permanent OPENs tying up exposure. **Fix:** add a price-index resolver
and a terminal path before enabling the class.

---

## Priority order

1. **A1 + A2 + A3** — stop booking REVIEW arbs; build Polymarket/SX resolvers and auto-run
   reconcile; add the settle-time timing guard. These are why the settled arb book is not
   trustworthy today, and all three are small changes.
2. **C0** — fix the neg-risk fee curve (one-line model swap); it manufactures fake basket arbs
   in your *best* arb class.
3. **B1 + B3 + B4** — capture real multi-level ladders (Polymarket especially) and normalize
   depth units; until then make capacity report UNKNOWN instead of fantasy.
4. **B2 + C2** — dedup by position key and filter to actionable before any aggregate.
5. **C1/C3/C4/C5** — simultaneity haircut, portfolio cap binding detection, correct holding
   horizon, started/final guard at detection.
6. **D1–D4, E1–E3** — matching/adapter correctness and observability.

### On the original sizing question

"Bet more when more depth and more edge" is already the right idea and already built:
`ladder_scaled_stake` sizes off `profitmax_stake_usd`, which jointly encodes edge (margin below
$1) and depth (how far you can walk before the lock crosses). It's gated behind
`arb_size_by_ladder: false` and is degenerate until B1/B3/B4 land. Sequence: fix ladder capture
+ depth normalization → confirm the capacity report shows real `edge`-limited arbs → then turn
`arb_size_by_ladder` on with a conservative `safety_frac`. You cannot answer "how aggressive
before I lose" until the binding leg's ladder is real; right now the model literally cannot
represent the losing region.
