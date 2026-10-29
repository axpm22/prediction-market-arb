# Phase 0: Kalshi MLB correlated-market feasibility check (2026-10-29 ET)

Read-only. Public endpoints only, no credentials. **230 Kalshi trade-API calls** (cap was 400). Zero 429s from the API. All calls were made 16:32–16:55 ET, outside the quiet windows. Log: `phase0/requests.log`. It also lists docs, PDF and asset fetches, marked `(docs)`/`(asset)`. Raw JSON is in `phase0/raw/`, rulebook PDFs and text in `phase0/pdf/`, scripts are `k.py` (limiter/logger), `count_events.py`, `summarize.py`, `sample.py` and `mvec_scan.py`.

**Season state:** MLB 2026 is over. `KXMLB-26` (champion) is finalized with MIL = yes, and the last game event is `KXMLBGAME-26OCT282000MILTB` (WS Game 5, Oct 28). No MLB game markets are open, so live depth for MLB is not observable until 2027.

## A. API surface (docs.kalshi.com, openapi.yaml v3.35.0)
Public means no `security` in the OpenAPI spec.

| Endpoint | Auth | Notes |
|---|---|---|
| GET /series, /series/{s} | public | carries fee_type, fee_multiplier, contract_url, contract_terms_url, settlement_sources |
| GET /series/fee_changes?series_ticker&show_historical | public | fee history per series |
| GET /events, /events/{e}, /events/multivariate | public | old Events/Series are never archived |
| GET /markets, /markets/{t} | public | settled before `market_settled_ts` → only in /historical/markets |
| GET /markets/{t}/orderbook, /markets/orderbooks | public | **params: ticker, depth only. No timestamp, so current book only** |
| GET /markets/trades | public | ticker, min_ts, max_ts, cursor, limit≤1000 |
| GET /series/{s}/markets/{t}/candlesticks, /markets/candlesticks | public | period_interval 1/60/1440 |
| GET /historical/cutoff, /historical/markets[/{t}], /historical/markets/{t}/candlesticks, /historical/trades | public | archive tier |
| GET /historical/fills, /orders, /positions | AUTH | user-scoped |
| GET /multivariate_event_collections[/{c}] | public | combo templates |
| GET /communications/rfqs, /quotes, /rfq_batches… | **AUTH** | RFQ/quote data is not public |

Current cutoff (`GET /historical/cutoff`): `market_settled_ts=2026-08-30T00:00:00Z`, `trades_created_ts=2026-08-30T00:00:00Z`, `orders_updated_ts=2026-10-15`. The docs say: "Old **Events** and **Series** will always still be available through their original endpoints". Partitioning covers "**markets**, **market_candlesticks**, **trades**, **orders**, and **market_positions**".

Historical orderbook snapshots **do not exist** in the REST API. The orderbook endpoint "shows all active bid orders". The only depth-like history is the candlestick `yes_bid`/`yes_ask` OHLC, which is top-of-book **price only, no size**. A candle has `price{open,high,low,close,mean,previous}`, `yes_bid{ohlc}`, `yes_ask{ohlc}`, `volume` and `open_interest`. Depth history requires running your own WebSocket `orderbook_delta` recorder going forward.

RFQ/combos (docs `getting_started/rfqs`): "RFQs are available on any market, including combo (multivariate event) markets. Combo markets are classified as High Volatility Markets (HVM)". "Each quote is private between the requester and the individual maker." The `communications` WS channel requires auth. Combo RFQs carry `mve_collection_ticker` + `mve_selected_legs`.

## B. MLB inventory (GET /series?category=Sports: 4,073 series, 243 match baseball/MLB terms; full list in raw/series_sports.json)
Game-level and prop series. Current fee is from the series object, and fee history is from /series/fee_changes.

| Series | Title | Rulebook (contract_terms_url) | settlement_sources | fee_type / mult (now) | Events (all) |
|---|---|---|---|---|---|
| KXMLBGAME | Professional Baseball Game | BASEBALLGAMEWIN | ESPN, Fox Sports, Governing League | quadratic_with_maker_fees / 0.5 | 4,684 |
| KXMLBTOTAL | Total runs | BASEBALLTOTALS | MLB | quadratic / 0.5 | 2,521 |
| KXMLBSPREAD | Spread | BASEBALLSPREAD | Governing League | quadratic / 0.5 | 2,521 |
| KXMLBTEAMTOTAL | Team total | BASEBALLENTITYSTAT | ESPN, Fox, League | quadratic / 0.5 | 2,419 |
| KXMLBF5 | First 5 winner | BASEBALLGAMEWIN | CBS, ESPN, League | quadratic / 0.5 | 2,481 |
| KXMLBRFI | Run in 1st inning | BASEBALLRFI | League | quadratic / 0.5 | 2,493 |
| KXMLBHR / HIT / TB / HRR / KS / RBI | player props | BASEBALLENTITYSTAT | ESPN, Fox, League | quadratic / 0.5 | 2,475 / 2,476 / 2,476 / 2,476 / 2,462 / 1,305 |
| Also listed, not paged | KXMLBF5TOTAL, F5SPREAD, F3, F7, EXTRAS, SB, OUTS, HA, WALK, ERA, INNINGWIN/TOTAL, NEXTHR | n/a | n/a | mostly quadratic / 0.5 | n/a |
| Futures | KXMLB (champion), KXMLBWS, KXMLBAL/NL, KXMLBSERIES, division and award series | n/a | n/a | KXMLB, KXMLBAL/NL, KXMLBSERIES: quadratic_with_maker_fees / 1 | n/a |

Fee history: `KXMLBGAME` changed to quadratic_with_maker_fees ×1 on 2025-10-04 and to ×0.5 on **2026-08-07**. `KXMLBTOTAL` and `KXMLBHR` changed to quadratic ×0.5 on 2026-08-07, with no earlier change listed. A backtest must apply the multiplier in force on each trade date.

Markets per event in the samples: GAME 2, TOTAL 11 strikes, HR 13–33, KS 14.

**Combos.** There is **no MLB-specific collection** (`series_ticker=KXMVEMLBSINGLEGAME` returns 0). The scanned collections are NBA/NFL single-game, multi-game, cross-category and weather. Currently open: `KXMVESPORTSMULTIGAMEEXTENDED-R` (3,100 associated events, no MLB because the season is over), `KXMVECROSSCATEGORY` and `-SHARD1`. MLB legs appeared in **KXMVECROSSCATEGORY-SHARD1-R** combos. Settled-MVE sample, Sep 15–16 2026, first 1,000 of more: 63 combos had MLB legs, 0 were all-MLB and only 4 had ≥2 legs from the same MLB game. Leg series: GAME 243, TOTAL 120, HIT 8, KS 3, HR 3, TEAMTOTAL 3, SPREAD 1. Combos are 2–38 legs. **Quoting is RFQ, not a book.** Combo markets show `yes_bid 0 / yes_ask 1` and an open combo's orderbook is `{"yes_dollars":[],"no_dollars":[]}`. Executed combo **trades are public** through /markets/trades: the highest-volume MLB-leg combo in the sample, 18 legs with volume 38,333, returned 2 trades, both at yes 0.0005 for 16.7k and 21.7k contracts.

## C. History depth (event tickers from GET /events, paged to completion; season split by ticker date)
The 2025 "reg" count of 2 for TOTAL/SPREAD is Sep-30 Wild Card games, so they are postseason. Counts are all listed events; postponed games resolve at fair price and are included.

| Series | Earliest event | 2025 reg | 2025 post | 2026 reg | 2026 post |
|---|---|---|---|---|---|
| KXMLBGAME | 25APR16 ATL-TOR | 2,167 | 36 | 2,449 | 32 |
| KXMLBTOTAL | 25SEP30 BOS-NYY | 0 | 40 | 2,449 | 32 |
| KXMLBSPREAD | 25SEP30 BOS-NYY | 0 | 40 | 2,449 | 32 |
| KXMLBRFI | 25OCT17 TOR-SEA | 0 | 10 | 2,451 | 32 |
| KXMLBF5 / HR / HIT / TB / HRR / KS | 26MAR25 NYY-SF | 0 | 0 | ~2,431–2,449 | 31–32 |
| KXMLBTEAMTOTAL | 26MAR31 TEX-BAL | 0 | 0 | 2,387 | 32 |
| KXMLBRBI | 26JUN22 HOU-TOR | 0 | 0 | 1,273 | 32 |

Trades and candles were sampled on the highest-volume market of each event. All returned HTTP 200.

| Market | Tier | Volume | Trades (page 1, limit 1000) | Candles 1m (last 3 d) / 60m / 1440m |
|---|---|---|---|---|
| KXMLBGAME-25JUL20DETTEX-TEX | historical | 315,749 | 1000, more | 573 / 15 / 0 |
| KXMLBGAME-26APR152210NYMLAD-LAD | historical | 2.89M | 1000, more | 2285 / 75 / 4 |
| KXMLBGAME-26JUL202210STLLAA-STL | historical | 3.84M | 1000, more | 2206 / 75 / 4 |
| KXMLBGAME-26SEP152140MIAAZ-MIA | live | 7.52M | 1000, more | 1197 / 65 / 4 |
| KXMLBTOTAL-26JUL202210STLLAA-9 | historical | 419,618 | 1000, more | 953 / 33 / 2 |
| KXMLBHR-26APR152210NYMLAD-LADFFREEMAN5-1 | historical | 32,193 | 391 (all) | 442 / 21 / 1 |
| KXMLBHR-26SEP152140MIAAZ-AZKMARTE4-1 | live | 61,545 | 505 (all) | 429 / 25 / 1 |
| KXMLBKS-26JUL202210STLLAA-STLKLEAHY62-5 | historical | 35,195 | 630 (all) | 529 / 28 / 2 |

Markets settled before 2026-08-30 come only from `/historical/markets?event_ticker=`, `/historical/trades?ticker=` and `/historical/markets/{t}/candlesticks`. The live endpoints return 0 for them. Trades carry `taker_outcome_side`/`taker_book_side`, price, count and created_time. They give an executed-price tape with aggressor side, not resting depth.

## D. Settlement rules (verbatim, assets.kalshi.com contract_terms PDFs + market rules_secondary)
Contract-terms PDF creation dates: GAMEWIN 2026-02-12, TOTALS **2026-08-17**, SPREAD **2026-08-24**, ENTITYSTAT **2026-09-08**. The rules changed mid-season. The regulatory product-certification versions of TOTALS/SPREAD (dated 2025-09-30) differ materially on shortened games (see the shortened-game row).

| Topic | Game winner (BASEBALLGAMEWIN) | Total (BASEBALLTOTALS) | Spread (BASEBALLSPREAD) | Player props (BASEBALLENTITYSTAT + market text) |
|---|---|---|---|---|
| Not in lineup / DNP | n/a | n/a | "All Contracts remain valid regardless of starting pitcher changes" | "If a given <entity> or <player> is scratched from the starting lineup or does not appear in the starting lineup for <baseball game> at the start of the first pitch, the markets … will resolve to the last fair price". KXMLBHR market: "If Ketel Marte is not in the starting lineup but later enters the game, the market will resolve to the fair market price, pinch hit at bats will not count". KXMLBKS: "If Kyle Leahy is not a starting pitcher but later enters the game, … relief appearances will not count" |
| Removed mid-game | n/a | n/a | n/a | "removed … due to ejection, injury, or substitution after appearing in the game, the markets … will settle based on the end-of-game statistics if <entity> or <player> have recorded at least one plate appearance (for position players) or faced at least one batter (for pitchers)." The HR market adds: "starts the game but does not record a plate appearance, the market will resolve to the fair market price". KXMLBKS market: "is a starting pitcher and records at least one pitch the market will settle based on strikeouts" (pitch vs batter wording differs) |
| Postponement | "postponed … but is rescheduled and started within 48 hours … resolve based on the official final result of the rescheduled game. If … not started within that 48-hour period, … last fair market price" | "suspended or postponed and not resumed within 48 hours …, all Contracts settle to the last fair market price prior to suspension unless the outcome has been unequivocally determined" | same as Total | "cancelled, postponed, or suspended prior to becoming an official game … and does not resume within 48 hours … last fair price" |
| Suspended | "suspended during play and resumed within 48 hours … resolve based on the official final result of the completed game" | (as above) | (as above) | "delayed but resumes and completes within 48 hours … settle based on the final official statistics" |
| Shortened / rain | "shortened, called … and the governing body declares the game official and records a winner, the market for the full game will resolve based on that official result" | Contract terms (Aug 2026): same text as game winner. **Product-cert (Sep 2025): "called/terminated before 9 innings are completed (8.5 if home team leading), all Contracts settle to the last fair market price unless the outcome was already unequivocally determined"** | Contract terms: same text as game winner. Product-cert has the 9/8.5-inning rule | Full game uses the official result. "statistics that could not be definitively determined before the game was called will resolve at the last fair market price" |
| Extra innings | "all extra innings … are included in determining the winner" | "All runs scored in extra innings count toward the total … There is no maximum number of extra innings" | "All scoring in extra innings counts toward the final margin" | Default `<time period>` is "the entire game including extra innings" |
| Stat corrections | "Subsequent corrections or revisions after Expiration will not be considered." | "Post-game statistical corrections after Expiration do not affect settlement" | "Any subsequent statistical changes after Expiration will not be taken into consideration" | "Statistics are considered final at the conclusion of <baseball game>. Corrections made afterwards will not affect the value of the Contract." |
| Doubleheaders | `<baseball game>` may be defined by "game number within a doubleheader (e.g., "Game 1" or "Game 2")". Make-ups: "the first makeup game played shall resolve the Contract for the first postponed game chronologically" | Product-cert: "For 7-inning doubleheaders, the minimum requirement is 7 innings (6.5 if home team leading)" | same as Total | "For doubleheader games, each game is treated as a separate <baseball game> unless explicitly specified otherwise" |

Rule drift inside the data: the 2025 KXMLBGAME `rules_secondary` reads "(within two years) … If the cancelled game is not played, rescheduled, or declared a tie, the market will resolve to 50/50". The 2026 text reads "(within two days) … resolve to a fair price". Fair-price resolutions are discretionary, so a backtest should drop those markets rather than model them.

## E. Fees
The official fee schedule PDF (`https://kalshi.com/docs/kalshi-fee-schedule.pdf`, referenced by the OpenAPI FeeType description) returned **HTTP 429** to both curl and WebFetch. Not retried, so it is **NOT verified first-hand**.

The OpenAPI FeeType description (first-hand) says: "'quadratic' is described by the General Trading Fees Table, 'quadratic_with_maker_fees' is described by the General Trading Fees Table with maker fees described in the Maker Fees section, 'quadratic_with_combo_maker_fees' is the same maker-fee structure with a 0.5 maker multiplier instead of 0.25". FeeMultiplier is "a floating point multiplier applied to the fee calculations".

Secondary sources (WebSearch summaries of the PDF, not first-hand) give taker = ceil(0.07 × C × P × (1−P)) and maker = ceil(0.0175 × C × P × (1−P)) on maker-fee series. The 0.0175 = 0.25 × 0.07 matches the OpenAPI wording. Applied to MLB now:
- **KXMLBGAME:** taker 0.5 × 0.07 = 0.035·C·P(1−P), maker 0.5 × 0.0175 = 0.00875·C·P(1−P).
- **Every other MLB game/prop series** (quadratic ×0.5): taker 0.035·C·P(1−P), maker 0.
- **Before 2026-08-07:** multiplier 1.
- **Rounding:** "Trade fee: Fee from the fee model, rounded up to the nearest `$0.000001`" (docs/fee_rounding), plus a rounding fee to the $0.01 balance grid for non-direct members.

Confirm the PDF manually before freezing any prereg.

## Verdict
- **Price history: yes, enough for a price/tape backtest, not for an executable-depth backtest.** KXMLBGAME has two seasons, about 4.6k games. TOTAL, SPREAD, F5, RFI, TEAMTOTAL and the props cover one season, about 2.4k games each (RBI about 1.3k). Every sampled market has full trade tapes with aggressor side, plus 1-minute candles that include top-of-book bid/ask OHLC, all public. **No historical orderbook depth exists anywhere in Kalshi's public API.** Size at the touch and beyond is unrecoverable, so fills can only be bounded with trade prints and bid/ask candles. Depth needs a forward WS recorder, which can only start in 2027 when MLB markets reopen.
- **Combos exist but cannot be priced from the public API.** MLB legs only appear inside cross-category combos (KXMVECROSSCATEGORY*). There is no MLB same-game collection, and same-game MLB combos were rare (4/63 in the sample). Combos have no resting book; quotes come by RFQ, which is auth-only and private. Only post-trade combo prints are public, and they are sparse and lopsided (e.g. 0.0005 longshots). A correlated-leg mispricing model can be scored against single-leg prices, but combo-side "executable price" history does not exist publicly.
