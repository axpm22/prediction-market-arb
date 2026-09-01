# Polynode assessment + depth-verdict audit — 2026-09-01

Read-only session. No writes to `flat_bets`, no venue credits spent, HEAD `d0a03de`.

## 1. What the Polynode key is

`pn_live_...`, tier **free** (verified: every v2/v3 path returns
`402 {"error":"paid plan required","tier":"free"}`). Auth is header `x-api-key`.

Free surface: **v1 REST only**, 100K req/month, 60 req/min nominal (a probe measured
~40/min sustainable before 429s), plus a **full-firehose WebSocket capped at 1 hour/day**
(measured ~47 fills/sec; each fill carries `taker`, `user`, `order_hash`, price, shares,
side, token/condition/slug, `block_number: null` while pre-chain).

Paywalled: all V3 historical REST, wallet P&L, leaderboards, backtesting, bulk trade
history, and **all credits endpoints** (maker rebates, liquidity rewards, taker rebates).

Corrections to the working brief, found by agents mid-session:
- `/v1/events/search` and `/v1/crypto/*` are **403/402**, not free.
- `/v1/markets/{slug}/positions` **is** free, resolves ~30 days back, and returns
  `currPrice ∈ {0,1}` on settled markets — i.e. free labelled outcome data.

## 2. Verdict on Polynode: it does not answer this project's question

- It is **Polymarket-only**. The repo's actual depth gap is **Kalshi** (§3).
- The repo already has better Polymarket access, free and keyless:
  `arb/venues/polymarket.py:3` — batched `POST /books`, full per-token ladders. Polynode's
  `/v1/orderbook` is one token per request at 60/min, and 100K/mo = **2.3 req/min
  sustained**: a throughput downgrade of roughly 1000x.
- Its headline product (3-5s pre-chain settlements) leads Polymarket's **chain-derived
  trade tape** by ~2.0s, but leads the **CLOB book channel by zero or less** — the book is
  off-chain and moves at match time. Price information lives in the book.
- For maker adverse selection specifically the lead is causally useless: the sequence is
  taker order -> CLOB match (you are already filled) -> mempool -> pending event. The
  warning arrives after the loss is booked.

**Do not buy a subscription on current evidence.**

## 3. The finding that matters: the depth verdict was never measured on ladders

Reproduced the committed 9-day result exactly (`covers --since 2026-07-31`):
81 distinct covers, **5 cleared the spread, 0 cleared the $50 depth floor**
(4,173 observations vs the committed 4,176 — three rows landed after the commit).

But the depth gate never reads a ladder. `arb/covers.py:53` `_depth()` takes `min(depth_usd)`
across legs, and `depth_usd` is **top-of-book only on both venues**:

- `arb/venues/polymarket.py:142` — `depth_usd = Decimal(asks[0]["size"]) * ask`
- `arb/venues/kalshi.py:161`     — `depth = Decimal(size) * ask`, from `yes_ask_size_fp`

### Ladder coverage in the archive

| venue | quotes | with ladder | pct |
|---|---:|---:|---:|
| kalshi | 26,990,797 | **0** | 0.00% |
| polymarket (sports) | 30,128,331 | 30,128,331 | 100.00% |
| polymarket (nonsports) | 7,927,360 | **0** | 0.00% |
| sx | 722,617 | 722,617 | 100.00% |

### The five spread survivors, re-examined against stored ladders

| cover | edge | ratio | min leg (top-of-book) | ladders present? |
|---|---:|---:|---|---|
| mlb royals:twins | 0.02535 | 0.99x | kalshi $0.43 | neither leg |
| mlb nationals:phillies | 0.01734 | 0.58x | polymarket $16.84 | **PM only** (27 levels, $45,238 notional) |
| nonsports kxeconstatu3-t4.5 | 0.01289 | 0.89x | kalshi $9.00 | neither leg |
| nonsports kxeconstatu3-t4.4 | 0.01309 | 0.42x | polymarket $0.26 | neither leg |
| nonsports kxcpicore-t0.0 | 0.02196 | 0.25x | kalshi $11.67 | neither leg |

**Not one of the five has ladders on both legs.** Three are `nonsports:`, where neither
venue stored one; the other two are killed or bounded by a Kalshi leg that never has one.
The single retrievable PM ladder shows **$16.84 at the touch against $45,238 of total
ladder notional** — not all of it inside the edge budget, but a difference of that order is
exactly what the gate cannot currently see.

### A gate that cannot fire

`arb/capacity.py:54` requires `all(len(lad) >= 2 for lad in ladders)`, and
`sizing.parse_ladder(None) -> []` (verified). Any cover with a Kalshi leg therefore fails
`has_ladders` **by construction**, so `capacity.py` has been emitting
`INSUFFICIENT_DEPTH_DATA` for structural reasons, not empirical ones.

**Honest status of "all 5 died on depth": unevaluable, not confirmed and not refuted.**
It is a statement about best-level size on legs whose ladders were never recorded.

## 4. The fix is available and free

`arb/venues/kalshi.py:194` already implements `fetch_orderbook()`; the scan path at
`kalshi.py:148` throws it away with `ladder_json=None`. The endpoint is public and
unauthenticated — verified live today on `KXMLBGAME-26SEP041410DETCLEG1-DET`:
**17 levels on the NO side, 20 on YES**, dollar-denominated `[price, size]` pairs matching
the shape in the docstring. No key, no credits, no vendor.

The nonsports Polymarket lane needs the same treatment — it uses a Gamma-based path that
never populates `ladder_json`, so 7.9M quotes carry none.

## 5. The five theses, scored

| | thesis | confidence | outcome |
|---|---|---:|---|
| A | Depth-aware execution model | 34 | Premise falsified in-repo (§3). Fix is a recompute, not a program. |
| C | Order-flow / smart money | 22 | Wallet-skill falsified on its sample; a size effect survived. See §6. |
| D | Pre-chain latency edge | 12 | Category error — leads the tape, not the book. |
| E | Rebates flip the maker lane | 12 | Short by 14x, and temporally confounded. |
| B | Neg-risk structural arb | 8 | Falsified on live data. |

**B**: on 10 liquid binary books, `YES_ask + NO_ask` equalled `1 + tick` exactly every time,
because **334/334 legs satisfied `YES_ask == 1 - NO_bid` exactly** — the CLOB mirrors
complements natively, so no residual exists at any latency. Across 38 neg-risk events the
total measured capacity of the whole structural class was **$0.067**.

**E**: rebate per share reduces to `rebate_pct x feeRate x p(1-p)`. Over the L12 band
(`config.yaml:470`, E[p(1-p)] = 0.1967) the best category yields **0.275c/fill vs the
3.86c loss** — 7.1%. Break-even needs `rebate_pct x feeRate = 0.196`; the best available is
0.014, short by 14x. Even a 100% rebate of the entire crypto taker fee reaches 36%.
Independently confirmed off-repo: rebate share is 15-25% by category and geopolitics is
fee-free, so the largest reward pots (four of the top ten are Iran/Hormuz) rebate exactly $0.
Also: maker rebates began 2026-01-07, before the 07-08 -> 08-09 measurement window, so
-0.0386 is already the post-subsidy equilibrium and adding the rebate back double-counts it.

Note `arb/venues/fees.py:39` returns `Decimal(0)` for the PM maker with the comment
"(+ taker-funded rebate pool, not modeled)". That unmodeled zero can now be replaced with a
measured upper bound, which strengthens the headline rather than weakening it.

## 6. The one thread worth pulling

Agent C measured, on 40 consecutive resolved **BTC 5-minute** markets (50,939 usable rows,
4,160 wallets), using `e_i = y_i - p_i` with empirical-Bayes shrinkage:

- **tau-hat^2 = -0.000319** — cross-wallet variance is smaller than sampling noise. No
  wallet-skill dispersion. Shrinkage lambda = 0.
- **A monotone size effect across all ten deciles**: d1 -0.0390 -> d10 +0.0421;
  d10-d1 = **+0.0811, cluster-bootstrap 95% CI [+0.0219, +0.1486]**.
- A clean demonstration of leaderboard bias: the API's default `sortBy=TOTAL_PNL`
  truncation reported pooled edge **+0.199** and tau = 0.20; the full census gives
  **-0.004** and tau = 0. **100% of the apparent skill was the sort.**

External literature points the other way on wallet skill, and the disagreement is
informative rather than contradictory. Mitts & Ofir screened all Polymarket contracts
Feb 2024 - Feb 2026 and found 210,000+ suspicious bets at a ~70% win rate, estimating
$143M as a lower bound on anomalous profits; their top signals include **cross-sectional
and within-trader bet size** — the same variable C found. Yale's Jensen et al. find a small
group of informed traders move prices toward outcomes and capture much of the profit.

The reconciliation: C tested for informed trading in **BTC 5-minute crypto markets**, the
one category where private information essentially cannot exist. The literature's informed
trading lives in event markets (FOMC, geopolitics, entertainment) with slower resolution
and real information asymmetry. **C's null is most likely a category artifact.**

C's own recommendation follows and is cheap: run the free census collector
(`/v1/markets/{slug}/positions`, ~2 requests per market, ~1,630 labelled rows each) across
**sports and politics**, where spreads are wider than one tick and resolution is slower.
Roughly 4.8 hours of wall clock and ~17% of the monthly free quota. It either kills the
size effect or locates a category where 8 points of edge outruns the spread.

Caveat that survives regardless: d10's +0.0421 is measured at *the informed trader's* entry
price. A follower crosses a tick and eats the print's own impact, leaving ~+0.022 central
and **+0.0045 at the lower CI bound** — the same shape of answer the arb lane got.

## 7. Recommendation

1. **Wire `kalshi.fetch_orderbook` into the quote path.** Endpoint verified public and
   working. One call site. Closes the last imputation in KNOWN_ISSUES #8.
2. **Populate `ladder_json` on the nonsports Polymarket path** — 7.9M quotes, 3 of the 5
   survivors, currently unmeasurable.
3. **Re-run `arb covers` over a fresh window** with both legs laddered, and recompute
   `capacity.py` now that its gate can actually fire.
4. **Amend, don't re-open.** `PREREGISTRATION.md` is frozen. Steps 1-3 are a defect fix to
   an existing result, not a new study; they should be recorded as a dated amendment.
5. **Do not buy Polynode.** Revisit only if step 3 moves a cover above the $50 floor.
6. Optional and cheap: the §6 census across sports/politics.

Nothing here overturns "neither side pays". The maker result remains replicated across two
venues by two methods (PM -0.0386 [-0.0693, -0.0079] n=126; Kalshi L7 -0.0164
[-0.0201, -0.0127] n=103,879). What changes is that the **taker** lane's cause of death is
currently unproven, and proving it costs a day of work and zero dollars.

## Sources

- https://docs.polynode.dev/llms-full.txt
- https://docs.polymarket.com/market-makers/maker-rebates (rebate formula, category rates)
- https://www.law.columbia.edu/news/archive/professor-joshua-mitts-prediction-markets-and-informed-trading
- https://insights.som.yale.edu/insights/wisdom-of-the-few-prediction-markets-are-driven-by-small-number-of-skilled-traders
- https://corpgov.law.harvard.edu/2026/03/25/from-iran-to-taylor-swift-informed-trading-in-prediction-markets/
- https://news.dropstab.com/research/kalshi-vs-polymarket (cross-venue fee/spread survey)
