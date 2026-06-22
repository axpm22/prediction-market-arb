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

from arb.btc_arb import BtcLeg, find_btc_arb
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


def _asset_key(strike: str, window_end: datetime) -> str:
    # strike + window folded into the asset so matching requires both to align.
    return f"BTC@{strike}@{window_end.strftime('%Y%m%d%H%M')}"


def parse_kalshi_btc(
    payload: dict[str, Any],
    fee_rate: Decimal = Decimal("0"),
    taker_buffer: Decimal = Decimal("0"),
) -> list[BtcLeg]:
    legs: list[BtcLeg] = []
    for m in payload.get("markets", []):
        strike = _strike(str(m.get("ticker", ""))) or _strike(str(m.get("yes_sub_title", "")))
        yes_ask = m.get("yes_ask_dollars")
        no_ask = m.get("no_ask_dollars")
        close = m.get("close_time")
        if not (strike and yes_ask and no_ask and close):
            continue
        end = datetime.fromisoformat(str(close).replace("Z", "+00:00"))
        asset = _asset_key(strike, end)
        yes_price = Decimal(str(yes_ask))
        no_price = Decimal(str(no_ask))
        legs.append(BtcLeg("kalshi", asset, "UP", end, q6(yes_price + all_in_fee_cost(yes_price, fee_rate, taker_buffer)), KALSHI_ORACLE))
        legs.append(BtcLeg("kalshi", asset, "DOWN", end, q6(no_price + all_in_fee_cost(no_price, fee_rate, taker_buffer)), KALSHI_ORACLE))
    return legs


def parse_polymarket_btc(
    payload: dict[str, Any],
    fee_rate: Decimal = Decimal("0"),
    taker_buffer: Decimal = Decimal("0"),
) -> list[BtcLeg]:
    legs: list[BtcLeg] = []
    for m in payload.get("markets", []):
        strike = _strike(str(m.get("question", ""))) or _strike(str(m.get("groupItemTitle", "")))
        end_text = m.get("endDate")
        ask = m.get("bestAsk")
        bid = m.get("bestBid")
        if not (strike and end_text and ask is not None and bid is not None):
            continue
        end = datetime.fromisoformat(str(end_text).replace("Z", "+00:00"))
        asset = _asset_key(strike, end)
        # YES "above strike" = UP at best ask; DOWN = buy NO = 1 - best bid.
        up_price = Decimal(str(ask))
        down_price = Decimal("1") - Decimal(str(bid))
        legs.append(BtcLeg("polymarket", asset, "UP", end, q6(up_price + all_in_fee_cost(up_price, fee_rate, taker_buffer)), POLY_ORACLE))
        legs.append(BtcLeg("polymarket", asset, "DOWN", end, q6(down_price + all_in_fee_cost(down_price, fee_rate, taker_buffer)), POLY_ORACLE))
    return legs


def run_btc_pass(store: Store, config,
                 kalshi_fetch: Callable[[], dict] | None = None,
                 poly_fetch: Callable[[], dict] | None = None) -> tuple[int, int]:
    kalshi_fetch = kalshi_fetch or _live_kalshi
    poly_fetch = poly_fetch or _live_poly
    legs = (
        parse_kalshi_btc(kalshi_fetch(), config.kalshi_fee_rate, config.taker_price_buffer)
        + parse_polymarket_btc(poly_fetch(), config.polymarket_crypto_fee_rate, config.taker_price_buffer)
    )
    arbs = find_btc_arb(legs, config.btc_window_match_secs, config.btc_trusted)
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


def _live_kalshi() -> dict:
    r = requests.get(f"{KB}/markets?series_ticker=KXBTCD&status=open&limit=200", headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def _live_poly() -> dict:
    r = requests.get(f"{GAMMA}/events", params={"closed": "false", "limit": 100, "order": "volume24hr", "ascending": "false"}, headers=HEADERS, timeout=30)
    r.raise_for_status()
    markets = []
    for e in r.json():
        if "bitcoin" in str(e.get("title", "")).lower():
            for m in e.get("markets", []):
                m["question"] = m.get("question") or e.get("title")
                markets.append(m)
    return {"markets": markets}
