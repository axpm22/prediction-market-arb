# Non-sports inventory survey — plan Phase 1.1 (run 2026-07-11)

Free public GETs only (Kalshi series/markets, PM Gamma). Zero Odds API credits.
First-page fixtures recorded under tests/fixtures/ (kalshi_series_*.json,
pm_gamma_{politics,economy,weather}.json — element-truncated, valid JSON).

## Verdict up front

The lane has more than enough surface. Polymarket alone lists **829 open
events** across the three tags, **513 of them (62%) carrying the structural
negRisk flag** — the exact precondition of the exhaustiveness proof. Kalshi
lists **~2,977 non-sports series** (2,083 Politics, 607 Economics, 287
Climate/Weather); the econ and weather series that matter are **numeric-strike
templated** (KXU3 unemployment: 70 open strike markets; KXUSRETAIL,
KXUSEDCARCPI, city-temperature series at 12 strikes each), i.e. matchable
deterministically on (series-concept, date, strike) with no AI.

## Polymarket (Gamma, 5 pages/tag cap)

| tag | events | negRisk | markets | median liq | liq top-5 | resolves <7d |
|---|---|---|---|---|---|---|
| politics | 500 (cap hit — more exist) | 265 | 5,385 | $32k | $70.7M, $47.1M, $37.5M, $12.1M, $9.6M | 43 |
| economy | 148 | 91 | 923 | $17k | $3.6M, $3.1M, $1.0M, $875k, $644k | 12 |
| weather | 181 | 157 (87%) | 1,714 | $39k | $498k, $267k, $262k, $222k, $214k | **143** |

Notes:
- **Weather is the measurement gift**: 143 of 181 events resolve inside 7 days
  — many full settlement cycles inside the 6-week window, and 87% negRisk.
- Politics liquidity is deep at the head (eight-figure books) and long-dated in
  the tail (376 events ≥90d out — the political-lane lockup exemption and
  capital-charge accounting will matter).
- Median event liquidity $17–39k: thin but far above the $5 negrisk
  min_executable floor; depth per outcome to be measured by the scans
  themselves (Phase 2).

## Kalshi (series catalog)

| category | series | shape of the tradable core |
|---|---|---|
| Politics | 2,083 | mostly one-off binary questions; many stale (0 open markets); matching needs entity tokens + the map |
| Economics | 607 | templated numeric-strike series: KXU3 (70 open), KXUSRETAIL (8), KXUSEDCARCPI (7), KXTEMPHELP (5) — deterministic (concept, month, strike) keys |
| Climate/Weather | 287 | city-temperature series with 12-strike ladders (KXLOWTDC, KXHIGHTLV, KXLOWAUS…), storm/hurricane events |

Notes:
- The catalog is littered with dead series (sampled Politics series: 9/12 had
  zero open markets). The matcher must key off **open markets**, not the raw
  catalog.
- Cross-venue overlap candidates visible already: US CPI/unemployment/retail
  (both venues), city temperatures (both venues), named storms, Fed decisions.

## What this changes in the plan

Nothing structural — it confirms Phase 1's design assumptions:
1. Deterministic blocking will cover econ + weather (strikes and dates on both
   sides); the Haiku stage is mainly for politics one-offs.
2. Weather-first for the measurement program (fast resolution cycles → most
   settled bets per calendar week).
3. PM politics pagination needs more than 5 pages (cap hit at 500 events);
   negrisk.scan_pages=10 covers ~1,000 — verify sufficiency when the lane runs.
