# Verified facts register (SPEC rule 3 / Phase 3 `[VERIFY]` items)

All verified live **2026-07-08** against production endpoints; fixtures recorded in
`tests/fixtures/`. Facts here are dated like fees — re-verify on drift (weekly shape-check).

| Fact | Verdict | Evidence |
|---|---|---|
| Kalshi event-level `mutually_exclusive` field | **EXISTS** — `GET /events/{event_ticker}` returns it (observed `false` on a totals event, as expected; negRisk-style groups must show `true` to pass proof condition (a)) | `kalshi_event.json` |
| Kalshi trades endpoint (tape check) | **EXISTS** — `GET /markets/trades?ticker=&limit=` public; fields `yes_price_dollars`, `created_time`, `taker_side`, `count_fp` | `kalshi_trades.json` |
| Kalshi volume field for zombie guard | **`volume_24h_fp`** (decimal string) on the market object — spec's `volume_24h` name drifted; `_fp` variant pinned | `kalshi_markets_winner.json` |
| Kalshi orderbook shape / size units | `GET /markets/{t}/orderbook` → `orderbook_fp: {yes_dollars, no_dollars}`, each `[price_dollars_str, contracts_fp_str]`; levels are **bids** per side (ask derives as 1 − best opposite bid). Size unit = contracts (fractional); depth USD at a level = contracts × price. Cross-checked vs market `yes_bid_dollars`. | `kalshi_orderbook.json` |
| Kalshi start time | Ticker-embedded `YYMMMDDHHMM` is **start in America/New_York** (26JUL081910 = Mets 7:10pm ET; cross-checked vs MLB Stats 823202-family schedule); `occurrence_datetime` ≈ expected END/expiration, `close_time` days later. Parse ticker; never fall back to `close_time` (bug D1). | fixtures + `mlb_schedule.json` |
| Kalshi decimal-dollar price fields | `yes_ask_dollars` etc. confirmed as decimal strings | `kalshi_markets_winner.json` |
| Polymarket CLOB batch books | **EXISTS** — `POST https://clob.polymarket.com/books` body `[{"token_id": ...}]` → array of full books (200). Full-ladder capture at scale is feasible; no per-token budget fallback needed | `pm_clob_books_batch.json` |
| Polymarket volume field | **`volume24hr`** (number) on Gamma market objects (may be null on dormant markets → treat null as 0 for the zombie guard) | `pm_gamma_mlb_games.json` |
| Polymarket negRisk structural flag | **EXISTS** — event-level `negRisk: true` and market-level `negRisk: true` on politics events (63/100 sampled) | `pm_gamma_politics.json` |
| Polymarket trade history (tape check) | CLOB `GET /trades` = **401 auth-required** → unusable (rule 1). Public `GET https://data-api.polymarket.com/trades?market={conditionId}` = 200 with side/price/size/timestamp. **NOTE:** data-api is outside the spec's §6 verified list — adopted for tape_check only, recorded here per rule 3; it never feeds pricing | `pm_trades.json` |
| Polymarket game start | `gameStartTime` present on sports moneyline markets (format `YYYY-MM-DD HH:MM:SS+00`) | `pm_gamma_mlb_games.json` |
| Polymarket gamma pagination | `offset` param works; game events live beyond the first 100 (futures sort first). tag_slug=mlb&closed=false ≈ 200 events total | probe 2026-07-08 |
| SX market/order shapes | As documented: type 226/28/342, `percentageOdds` ×1e20 maker-side, `isMakerBettingOutcomeOne`, sizes USDC ×1e6, `outcomeVoidName` (`NO_CONTEST` observed) | `sx_markets.json`, `sx_orders.json` |
| SX settled-market endpoint | `GET /markets/find?marketHashes=` = **200**, returns market objects with `status` field (only `ACTIVE` observable at record time). **Settled-status semantics unverified** until the first live settlement is observed; until then SX legs cannot leave REVIEW (they are REVIEW anyway: `trusted_pairs` empty + per-sport SX whitelisting required). Verify the settled status value during the first live `settle` run and update this row. | `sx_find.json` |
| MLB Stats API | `GET statsapi.mlb.com/api/v1/schedule?sportId=1&date=` free/keyless; `gamePk`, `gameNumber`, `doubleHeader`, `gameDate`, `status.detailedState`, per-team `score` | `mlb_schedule.json` |
| The Odds API | **VERIFIED live 2026-07-08 with a real key.** `tests/fixtures/oddsapi_h2h.json` is now a REAL recorded response (25 MLB events; 3 kept). Header math confirmed: `x-requests-last: 2` for h2h+totals (= the §6.4 budget assumption); `x-requests-remaining` logged on every call, sinks redacted. First live `arb scan` wrote 86–114 consensus rows + 702 per-book de-vig rows with canonical event keys | `oddsapi_h2h.json` |
| Polymarket NegRiskAdapter NO-basket conversion (addendum discovery #3) | **NOT VERIFIED** — on-chain adapter mechanics not verifiable from public HTTP GETs alone; NO-basket `lockup_days` stays at resolution horizon (conservative) until verified | — |
| Kalshi candlestick history (markout backfill) | **NOT PROBED** — optional optimization; markouts run from stored quotes first | — |
| Withdrawal/gas fee cells (addendum §2) | **NOT VERIFIED** — `[VERIFY]` placeholders remain in projections layer; projections label them | — |
| Kalshi global open-markets listing | **VERIFIED live 2026-07-14.** `GET /trade-api/v2/markets?status=open&limit=200` works WITHOUT `series_ticker` — returns 200 markets + cursor for pagination (global discovery primitive for the Kalshi ME-events scan). Same fields as the per-series form | — |
