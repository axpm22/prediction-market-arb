# Parlays — the PARLAY bet class (SPEC §11)

This documents the parlay opportunity class as built: what it is, why it is **not** part of the
zero-risk book, how it is detected, recorded, and settled, and what is still required before a
live run.

## What a parlay is, and why it carries risk

A parlay pays **$1 iff every leg wins**. There is no static replication — you cannot assemble a
basket of single-leg positions that reproduces the payoff — so a parlay can **never be locked
risk-free** the way a TRUE ARB can. It is an EV-style bet with real outcome variance. Accordingly,
every parlay flag is:

- `status = REVIEW` and **never auto-filled** (paper or otherwise),
- recorded in its **own ledger lane** (`flat_bets.kind = 'PARLAY'`, `trusted = 0`), so it is
  excluded from every trusted/headline aggregation,
- settled as the **conjunction** of its legs.

If the goal is "profit at zero risk," parlays are out of scope by construction. This class exists
to capture *model-free mispricings* with an edge, accepting variance.

## The two signals

Math lives in `arb/experimental/parlay.py` (pure, unit-tested against the SPEC §11 vectors) and is
reused by the pipeline module `arb/parlay.py`.

**1. Fréchet violation (model-free — the strong signal).** For legs with single-leg win
probabilities `p₁…pₙ`, the probability the parlay wins is bounded under *any* correlation:

```
max(0, Σpᵢ − (n−1))  ≤  P(all win)  ≤  min(pᵢ)
```

- Ask below the lower bound → `frechet-underpriced` (cheap no matter how the legs correlate).
- Bid above the upper bound → `frechet-overpriced`.

This is the only signal valid for **same-game** legs, because positive correlation makes the
independence product invalid there.

**2. Independence (cross-game only).** For legs from *different* games, fair ≈ `Πpᵢ`. An ask below
that by ≥ `parlay.divergence_threshold` is `independence-cheap`. Same-game markets are **never**
signalled this way (the product is only a lower bound on their true fair value).

Worked vector (legs 0.60, 0.55): bounds `[0.15, 0.55]`, fair `0.33`. Ask 0.12 → underpriced; ask
0.25 cross-game → independence-cheap; ask 0.25 same-game → no signal.

## Frictions modeled

- **Winning-bet fee.** SX parlays (and others) charge a fee only on winnings. The basket ask is
  charged a conservative `fee_rate · (1 − ask)` before comparison, so a fee-eroded edge correctly
  drops out (mirrors the SX single-bet win-fee model, D3).
- **Liquidity.** `parlay.min_liquidity_usd` is enforced strictly — SPEC §11 observed
  zero-liquidity Kalshi MVE books, and a "signal" on an empty book is noise. Thin markets are
  flagged `thin-parlay-book`.
- **Pregame-only.** A parlay with any leg already started, or with no known leg start, is not
  actionable (`in-play-skipped` / `missing-start-skipped`).

## How it is represented (so it reuses the existing pipeline)

A PARLAY `Opportunity` carries:

- **leg[0] = synthetic basket quote** (`market_type='PARLAY'`, `outcome_label='PARLAY'`) holding
  the fee-adjusted ask — this is the bet's `optimistic_cost`.
- **leg[1…n] = the component single-leg quotes** (`WINNER`/`TOTAL`, real `event_key`/outcome/line/
  start) — these are what settlement resolves.

This let the parlay flow through the existing tracker and settlement with minimal new surface:

- `tracker.py`: `_opportunity_cost` (PARLAY → basket ask), `_opportunity_stake` ($1 flat),
  `_optimistic_pnl` (None — realizes at settlement, like EV), and `_actionability_rejection`
  (pregame-only, nonsports → manual). `record_tracker_poll(..., only_kinds=('PARLAY',))` scopes the
  lifetime/dedup bookkeeping so a parlay cadence never closes the `watch` stream's ARB/EV bets.
- `settlement.py`: `settle_parlays` resolves each component leg against final scores and ANDs them:
  all `WON` → **WON** (`pnl = (1 − ask)/ask` on the $1 stake); any `LOST` → **LOST** (−$1); no loss
  but a `VOID` leg → **VOID** ($0, conservative until per-venue void rules are verified); any leg
  unresolved → stays OPEN. Wired into `settle_sports`, so the existing `settle` command and the
  daemon pick it up. Append-only and idempotent.

## How to run it

```
# detect only (safe; never writes):
python -m arb parlay-scan --fixtures --dry-run

# record into the ledger (requires parlay.enabled: true in config.yaml):
python -m arb parlay-scan --fixtures
python -m arb parlay-scan --file path/to/markets.json

# settle (runs as part of the normal settle pass):
python -m arb settle
```

Config (`config.yaml`):

```yaml
parlay: { enabled: false, divergence_threshold: 0.05, min_liquidity_usd: 500 }
```

`enabled: false` forces dry-run. Flip it on only after the live field-mapping below is verified.

## Input contract (venue-agnostic)

`arb/parlay.py` consumes `ParlayMarket` / `ParlayLeg`. `load_parlay_markets` maps this JSON shape
(see `tests/fixtures/parlays/markets.json`):

```json
{
  "venue": "kalshi", "market_id": "MVE-...", "ask": "0.12", "bid": null,
  "fee_rate": "0.0", "depth_usd": "300", "liquidity_usd": "1200",
  "same_game": false, "category": "sports",
  "legs": [
    {"event_key": "...", "sport": "baseball_mlb", "market_type": "WINNER",
     "outcome_label": "...", "prob": "0.60", "line": null, "start_utc": "2026-07-01T23:05:00Z"}
  ]
}
```

`prob` is the **same venue's** de-vigged single-leg win probability — the Fréchet/independence
input. Sourcing it from a different venue would mix settlement rules into the bound and is wrong.

## The remaining gap before live use

The detection, ledger lane, and settlement are complete and tested. **Live venue adapters are
not** — Kalshi combos (MVE markets, `mve_selected_legs`), Polymarket CAOC parlays, and SX Daily
Parlays (`sportId 25`) each need their payload mapped onto `ParlayMarket`, and per hard rule #3 the
exact endpoint fields must be verified against a recorded response before trusting them. Until then
the class is exercised fixtures-first (`--fixtures`/`--file`). Two settlement details to confirm
per venue when wiring live:

1. **Leg-void rules** — does a void leg drop out and re-price the parlay, or void the whole ticket?
   The current settler conservatively voids on any leg void; adjust per venue once verified.
2. **Single-leg probability source** — the de-vigged prob must come from that venue's own singles.

## Tests

- `tests/test_parlay_class.py` — signal classes + same-game control, fee erosion, thin-book flag,
  fixture round-trip, own-lane recording, PARLAY-scoped poll isolation, pregame-only skip,
  AND-settlement (all-win pays `(1−ask)/ask`; one loss pays −$1).
- `tests/test_parlay.py` — the SPEC §11 Fréchet/independence math vectors.
