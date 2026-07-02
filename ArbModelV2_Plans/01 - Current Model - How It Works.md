---
title: Current Model — How It Works
type: reference
project: ArbitrageModel
status: stable
tags: [arbmodel, v1, architecture, documentation]
created: 2026-07-01
---

# Current Model — How It Works

> [!abstract] One sentence
> A **paper-only** research system that watches the *same event* priced on Kalshi, Polymarket, and SX Bet, flags cross-venue arbitrage / divergence / +EV, and keeps an **append-only ledger** answering one question honestly: *"if I'd bet \$1 on every flag, where would I be — and how much of the edge survived contact with reality?"*

Related: [[_MOC - ArbModel V2]] · [[02 - Weaknesses and Lessons]] · [[03 - V2 Build Instructions]]

---

## 1. Goal & the three signal classes

The system converts every market price into one unit — **effective cost per \$1 of payout** (real order-book ask + that venue's fees) — so prices become directly comparable across venues. It then flags three classes:

| Class | Definition | Bet by tracker? |
|---|---|---|
| **TRUE ARB** | Outcome-covering asks across venues sum to `S < 1`; `ROI = 1/S − 1`, locked at entry. | Yes (\$1 total, split by stakes) |
| **DIVERGENCE** | Same outcome priced ≥ `divergence_threshold` apart, no locked profit. | No — recorded at \$0 stake (stat-arb fuel) |
| **+EV** | Venue cost below the de-vigged median of ≥3 sportsbooks. | Recorded when enabled; **currently disabled** (CLV unproven) |

Scope v1: game/match **winner (moneyline)** and **totals (over/under)**, exact-line match only. Sports: MLB, NBA, soccer, tennis, MMA (no NFL). Non-sports (politics/econ/crypto) are a **separate category**, **manual-match only**.

> [!warning] Hard rules that shape everything
> Paper only (public unauthenticated GETs). No magic numbers (all in `config.yaml`). **Money math in `Decimal`, never float.** Cross-venue is **REVIEW-only** until the venue-pair + market type is whitelisted in `trusted_pairs`. The `flat_bets` ledger is **append-only** — never edit or delete a row.

---

## 2. Data sources (verified June 2026)

- **Kalshi** — `api.elections.kalshi.com/trade-api/v2`, keyless GETs. Markets + orderbook. Fee `ceil_to_cent(rate·contracts·P·(1−P))`, quadratic curve.
- **Polymarket** — Gamma for discovery, **CLOB `/book` for executable prices** (Gamma `outcomePrices` are stale mids — never used for detection). Linear fee.
- **SX Bet** — `api.sx.bet`, keyless. Type 226 = moneyline, 28 = totals. **Quotes the maker's side**: taker cost `= 1 − percentageOdds/1e20`. Void outcomes differ → SX legs start REVIEW.
- **The Odds API** — sportsbook consensus only, key as query param, ~450/500 free credits/mo, abort < 50. Optional; gates the +EV class.

---

## 3. Matching with paranoia (`matching.py`)

Normalize names per sport via `aliases.json` → join on same sport + unordered pair + start within a 6-hour bucket. Totals match on the **exact line only** (8.5 ≠ 9.0). `rapidfuzz.token_set_ratio ≥ 92` → match but REVIEW. Two sanity gates suspend suspect matches: venues must agree on **start time within 30 min**, and the same outcome priced **>15 points apart** is treated as a matching error, not money. Non-sports are **never auto-matched** — a human promotes fuzzy candidates into `data/nonsports_map.yaml` after reading both rulebooks.

---

## 4. The evidence ladder — a flag is a claim, not a fact

Flagged edges die three ways: they were never simultaneous, they were stale, or the venues settle differently. Each has a defense, and every bet carries its grade.

```
depth floor  →  time-skew label  →  flash verify (~10s)  →  focus confirmation  →  trusted_pairs
 (dust)          (cross-venue        (VERIFIED/VANISHED,      (re-quote one          (settlement
                  ghosts)            atomic vs sequential)     game, all books)       mismatch)
```

> [!note] The killer no re-quote can see
> `trusted_pairs` is the only defense against **settlement-rule mismatch** (extra innings, postponement voids, divergent oracles). A re-quote confirms the *price* was real; it can never confirm the two venues will *settle the same way*. That is why cross-venue is REVIEW by default.

---

## 5. The ledger — append-only or it didn't happen (`tracker.py`)

Every flag becomes a \$1 paper bet in `flat_bets`. Rows are never edited or deleted; confirmations, closes, settlements, and corrections are all **new rows**. Known-bad bets are excluded only via an immutable annotations table requiring a written reason.

Each bet records **two fill columns**:
- `pnl_optimistic` — filled at first-sighting prices.
- `pnl_confirmed` — filled at the **second consecutive** sighting; NULL if never seen twice.

> [!tip] The single most important number
> **The optimistic − confirmed gap *is* the slippage estimate.** If confirmed ≈ 0 while optimistic looks great, the strategy does not survive reality.

Every quote ever observed (~945k) is persisted as backtest data. Daily reports are **recomputed from raw rows** (idempotent), never from stored aggregates. P&L attributes to the EST day the bet was placed, so past days get restated as pending bets settle (marked `*`).

---

## 6. Architecture — four cooperating processes, one ledger

| Process | Cadence | Role |
|---|---|---|
| `python -m arb daemon` | ~4.5 min sweep | discover markets, persist every quote, wide detection, daily settle + report |
| `arb.experimental focus` | ~1–2 min rotation | same bet, all books, at one time — simultaneous detection |
| `arb.experimental verify` | 10s poll | flash VERIFIED / VANISHED on fresh arbs |
| `arb.experimental closing` | 60s check | one closing-line EV snapshot per game (off while EV disabled) |

Core package `arb/` holds `models.py`, `store.py` (SQLite WAL), `matching.py`, `pricing.py`, `arbitrage.py`, `tracker`/`paper.py`, `settlement.py`, `negrisk.py`, `capacity.py`, `exposure.py`, venue adapters. Heavier/experimental strategies live under `arb/experimental/`.

---

## 7. What it found (see [[RESULTS]])

> [!success] The defensible headline
> **Cross-venue sports arbitrage is a mirage on confirmed fills.** 44 settled arbs → **+\$0.13 total** (≈\$0.003/bet), indistinguishable from zero. Flash verify explains why: many edges VANISH on re-quote; survivors are sub-1% and capped by thin depth; settlement-rule risk erases the rest.

- **+EV is contaminated** — raw ">5%" edges were tiny asks against empty books (largest raw "edge" ~54,500%, collapsing to 18.2% once contaminants were labeled). EV betting **stays disabled** until CLV clears its pre-registered bar.
- **Lower-risk theses** (neg-risk basket arb, favorite-longshot bias, lead-lag) are built and measuring; honest nulls so far.

**The lesson:** *a signal is worthless until you've proven the input isn't garbage.* Depth floors, one-sided-book rejection, and edge-sanity caps are the **precondition** for any sizing or scoring to mean anything.
