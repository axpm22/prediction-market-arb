"""Live BTC cross-venue arb sweep — Phase 4 / S3. Fixtures-first; records kind='ARB_BTC'.

Pairs Kalshi BTC threshold markets (settle on CF Benchmarks BRTI) with Polymarket BTC
markets (settle on Polymarket's UMA oracle). Because the two oracles are NOT identical
(verified 2026-06), every flag is recorded REVIEW and never auto-filled — the honest
gate. Matching requires the SAME strike and the SAME window end-time (encoded into the
asset key so find_btc_arb never pairs different strikes/windows).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

import requests

from arb.btc_arb import BtcLeg, find_btc_arb, find_btc_intramarket_arb, find_btc_ladder_arb
from arb.pricing import all_in_fee_cost, q6
from arb.store import Store

KB = "https://api.elections.kalshi.com/trade-api/v2"
GAMMA = "https://gamma-api.polymarket.com"
HEADERS = {"User-Agent": "arb-paper-scanner/0.1 (public read-only)"}
KALSHI_ORACLE = "cfbenchmarks_brti"
POLY_ORACLE = "polymarket_uma"
# Strike is a $-prefixed dollar amount (Polymarket questions) or a T-prefixed segment
# (Kalshi tickers, e.g. KXBTCD-26JUN2107-T72299.99). Never a bare number — that grabs dates.
DOLLAR_RE = re.compile(r"\$(\d[\d,]*(?:\.\d+)?)")
KALSHI_T_RE = re.compile(r"\bT(\d[\d,]*(?:\.\d+)?)")


def _strike(text: str) -> str | None:
    m = DOLLAR_RE.search(text or "") or KALSHI_T_RE.search(text or "")
    return m.group(1).replace(",", "") if m else None


def _asset_key(strike: str, symbol: str = "BTC") -> str:
    # B4: the asset is SYMBOL@STRIKE only (exact — a different strike, or a different underlying,
    # is a different event). The window is carried on the leg and matched by tolerance in
    # find_btc_arb, so a 1-minute representational gap between Kalshi close_time and Polymarket
    # endDate no longer misses a genuinely simultaneous window (the old minute-string key did).
    # B5: the symbol prefix lets ETH (and more) widen the universe without a BTC strike ever
    # matching an ETH strike of the same number.
    return f"{symbol}@{strike}"


def parse_kalshi_btc(
    payload: dict[str, Any],
    fee_rate: Decimal = Decimal("0"),
    taker_buffer: Decimal = Decimal("0"),
    stats: dict[str, int] | None = None,
) -> list[BtcLeg]:
    legs: list[BtcLeg] = []
    markets = payload.get("markets", [])
    strike_miss = 0
    for m in markets:
        strike = _strike(str(m.get("ticker", ""))) or _strike(str(m.get("yes_sub_title", "")))
        # B6: a strike-parse miss is the silent-format-drift signal — count it so a Kalshi ticker
        # format change can't quietly empty the lane (vs lumping it with missing-price skips).
        if strike is None:
            strike_miss += 1
            continue
        yes_ask = m.get("yes_ask_dollars")
        no_ask = m.get("no_ask_dollars")
        close = m.get("close_time")
        if not (yes_ask and no_ask and close):
            continue
        end = datetime.fromisoformat(str(close).replace("Z", "+00:00"))
        asset = _asset_key(strike, str(m.get("_symbol", "BTC")))  # B5: per-market underlying
        yes_price = Decimal(str(yes_ask))
        no_price = Decimal(str(no_ask))
        # B2: Kalshi's taker fee is the quadratic p(1-p) curve (matching arb/venues/kalshi.py),
        # NOT the linear default — pricing these legs linear over-charges the middle and
        # under-charges the tails, shifting every BTC total_cost/edge. Polymarket stays linear.
        legs.append(BtcLeg("kalshi", asset, "UP", end, q6(yes_price + all_in_fee_cost(yes_price, fee_rate, taker_buffer, basis="quadratic")), KALSHI_ORACLE))
        legs.append(BtcLeg("kalshi", asset, "DOWN", end, q6(no_price + all_in_fee_cost(no_price, fee_rate, taker_buffer, basis="quadratic")), KALSHI_ORACLE))
    if stats is not None:
        stats["kalshi_markets"] = stats.get("kalshi_markets", 0) + len(markets)
        stats["kalshi_strike_miss"] = stats.get("kalshi_strike_miss", 0) + strike_miss
    return legs


def parse_polymarket_btc(
    payload: dict[str, Any],
    fee_rate: Decimal = Decimal("0"),
    taker_buffer: Decimal = Decimal("0"),
    stats: dict[str, int] | None = None,
) -> list[BtcLeg]:
    legs: list[BtcLeg] = []
    markets = payload.get("markets", [])
    strike_miss = 0
    for m in markets:
        strike = _strike(str(m.get("question", ""))) or _strike(str(m.get("groupItemTitle", "")))
        if strike is None:  # B6: count format-drift misses (see parse_kalshi_btc).
            strike_miss += 1
            continue
        end_text = m.get("endDate")
        ask = m.get("bestAsk")
        bid = m.get("bestBid")
        if not (end_text and ask is not None and bid is not None):
            continue
        end = datetime.fromisoformat(str(end_text).replace("Z", "+00:00"))
        asset = _asset_key(strike, str(m.get("_symbol", "BTC")))  # B5: per-market underlying
        # YES "above strike" = UP at best ask; DOWN = buy NO = 1 - best bid.
        up_price = Decimal(str(ask))
        down_price = Decimal("1") - Decimal(str(bid))
        legs.append(BtcLeg("polymarket", asset, "UP", end, q6(up_price + all_in_fee_cost(up_price, fee_rate, taker_buffer)), POLY_ORACLE))
        legs.append(BtcLeg("polymarket", asset, "DOWN", end, q6(down_price + all_in_fee_cost(down_price, fee_rate, taker_buffer)), POLY_ORACLE))
    if stats is not None:
        stats["poly_markets"] = stats.get("poly_markets", 0) + len(markets)
        stats["poly_strike_miss"] = stats.get("poly_strike_miss", 0) + strike_miss
    return legs


def _log_parse_misses(stats: dict[str, int]) -> None:
    """B6: surface strike-parse misses so a silent venue format change doesn't quietly empty the
    lane. A high miss fraction means _strike's regex no longer matches the live ticker/question
    format — add the new format to DOLLAR_RE/KALSHI_T_RE and the fixtures."""
    for venue in ("kalshi", "poly"):
        miss = stats.get(f"{venue}_strike_miss", 0)
        total = stats.get(f"{venue}_markets", 0)
        if miss and total and miss / total >= 0.5:
            print(f"btc-sweep: WARNING {venue} strike-parse missed {miss}/{total} markets "
                  f"({miss / total:.0%}) — possible format drift; check _strike regex + fixtures.")


def run_btc_pass(store: Store, config,
                 kalshi_fetch: Callable[[], dict] | None = None,
                 poly_fetch: Callable[[], dict] | None = None) -> tuple[int, int]:
    kalshi_fetch = kalshi_fetch or _live_kalshi
    poly_fetch = poly_fetch or _live_poly
    stats: dict[str, int] = {}
    legs = (
        parse_kalshi_btc(kalshi_fetch(), config.kalshi_fee_rate, config.taker_price_buffer, stats=stats)
        + parse_polymarket_btc(poly_fetch(), config.polymarket_crypto_fee_rate, config.taker_price_buffer, stats=stats)
    )
    _log_parse_misses(stats)
    # Cross-venue (REVIEW, oracle mismatch) + B1 single-venue fillable locks (OK): intramarket
    # YES+NO, and the threshold-ladder inconsistency lock across strikes.
    arbs = (find_btc_arb(legs, config.btc_window_match_secs, config.btc_trusted)
            + find_btc_intramarket_arb(legs)
            + find_btc_ladder_arb(legs))
    poll_id = store.start_poll("btc")
    _persist(store, poll_id, arbs)
    store.finish_poll(poll_id, None, len(legs), len(arbs))
    return len(legs), len(arbs)


def _persist(store: Store, poll_id: int, arbs) -> None:
    with store.connect() as con:
        for a in arbs:
            fp = f"btc:{a.asset}:{a.up_leg.venue}-{a.down_leg.venue}"
            if con.execute("SELECT 1 FROM flat_bets WHERE fingerprint=? AND status IN ('OPEN','REVIEW') LIMIT 1", (fp,)).fetchone():
                continue
            legs = [
                {"venue": a.up_leg.venue, "book": a.asset, "outcome_label": "UP",
                 "cost": str(a.up_leg.effective_cost), "fee_cost": "0", "oracle": a.up_leg.oracle},
                {"venue": a.down_leg.venue, "book": a.asset, "outcome_label": "DOWN",
                 "cost": str(a.down_leg.effective_cost), "fee_cost": "0", "oracle": a.down_leg.oracle},
            ]
            con.execute(
                """INSERT INTO flat_bets(fingerprint, opened_poll_id, category, sport, kind, trusted, live,
                       edge, stake_usd, legs_json, optimistic_cost, status, placed_at, notes)
                   VALUES (?, ?, 'nonsports', 'btc', 'ARB_BTC', ?, 0, ?, '1.00', ?, ?, ?, ?, ?)""",
                (fp, poll_id, int(a.status == "OK"), str(a.edge), json.dumps(legs), str(a.total_cost),
                 a.status, datetime.now(timezone.utc).isoformat(),
                 f"{a.up_leg.venue}/UP + {a.down_leg.venue}/DOWN sum={a.total_cost} same_oracle={a.same_oracle}"),
            )


# B5: underlyings to widen the universe — more strikes/windows = more chances a cross-venue pair
# coincides. Per symbol: (Kalshi series ticker, Polymarket title keyword). Each market is tagged
# with its symbol so a BTC strike never matches an ETH strike of the same number. Still REVIEW
# (cross-oracle) until B3 proves parity; pure measurement upside, no Odds-API credits.
CRYPTO_SYMBOLS = {"BTC": ("KXBTCD", "bitcoin"), "ETH": ("KXETHD", "ethereum")}


def _live_kalshi() -> dict:
    markets: list[dict] = []
    for symbol, (series, _kw) in CRYPTO_SYMBOLS.items():
        try:
            r = requests.get(f"{KB}/markets?series_ticker={series}&status=open&limit=200", headers=HEADERS, timeout=30)
            r.raise_for_status()
        except requests.RequestException:
            continue  # one underlying's outage must not empty the whole lane
        for m in r.json().get("markets", []):
            m["_symbol"] = symbol
            markets.append(m)
    return {"markets": markets}


def _live_poly() -> dict:
    r = requests.get(f"{GAMMA}/events", params={"closed": "false", "limit": 100, "order": "volume24hr", "ascending": "false"}, headers=HEADERS, timeout=30)
    r.raise_for_status()
    keyword_to_symbol = {kw: sym for sym, (_series, kw) in CRYPTO_SYMBOLS.items()}
    markets = []
    for e in r.json():
        title = str(e.get("title", "")).lower()
        symbol = next((sym for kw, sym in keyword_to_symbol.items() if kw in title), None)
        if symbol is None:
            continue
        for m in e.get("markets", []):
            m["question"] = m.get("question") or e.get("title")
            m["_symbol"] = symbol
            markets.append(m)
    return {"markets": markets}
