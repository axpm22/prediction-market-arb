"""Kalshi adapter (SPEC §6.1). Public GETs only; decimal-dollar strings -> Decimal.

Verified facts (data/verified_facts.md): ticker-embedded YYMMMDDHHMM is the start
in America/New_York; occurrence_datetime is the expected END — never a start
source; close_time NEVER (bug D1). Volume field is volume_24h_fp. Orderbook is
orderbook_fp with [price, contracts] bid levels per side.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from arb.logutil import get_logger
from arb.models import Quote
from arb.venues.base import Deadline, PartialFetch, get_json, is_live
from arb.venues.fees import leg_fee_cost

log = get_logger("arb.kalshi", "logs/scan.log")

BASE = "https://api.elections.kalshi.com/trade-api/v2"
NY = ZoneInfo("America/New_York")
SERIES_CACHE = Path("data/kalshi_series.json")

# Series scanned per canonical sport: (series_ticker, market_type). Only FULL-GAME
# winner/total series belong here — they are the only ones whose settlement basis
# matches Polymarket's moneyline/totals. Deliberately excluded: KXMLBF5 (first 5
# innings), KX*1H*/KX*[1-4]Q* (period markets), KX*SPREAD (handicap). Pairing any
# of those against a full-game leg manufactures a fake arb.
# Series verified open or seasonally-dormant-but-real on 2026-07-29 (see the
# survey row in data/verified_facts.md).
SPORT_SERIES = {
    "baseball_mlb": [("KXMLBGAME", "WINNER"), ("KXMLBTOTAL", "TOTAL")],
    # KXNBA is the CHAMPIONSHIP-futures series (KXNBA-27-WAS, no date in ticker);
    # the game series is KXNBAGAME (last settled KXNBAGAME-26JUN13NYKSAS).
    "basketball_nba": [("KXNBAGAME", "WINNER"), ("KXNBATOTAL", "TOTAL")],
    "basketball_wnba": [("KXWNBAGAME", "WINNER"), ("KXWNBATOTAL", "TOTAL")],
    "tennis": [("KXATPMATCH", "WINNER"), ("KXWTAMATCH", "WINNER")],
    "mma": [("KXUFCFIGHT", "WINNER")],
    "americanfootball_nfl": [("KXNFLGAME", "WINNER"), ("KXNFLTOTAL", "TOTAL")],
    "icehockey_nhl": [("KXNHLGAME", "WINNER"), ("KXNHLTOTAL", "TOTAL")],
    # Soccer is one canonical sport spanning many competitions. The European
    # league series are dormant until mid-August and wake up on their own.
    "soccer": [("KXUCLGAME", "WINNER"), ("KXMLSGAME", "WINNER"),
               ("KXLIGAMXGAME", "WINNER"), ("KXBRASILEIROGAME", "WINNER"),
               ("KXCLUBFGAME", "WINNER"), ("KXEPLGAME", "WINNER"),
               ("KXLALIGAGAME", "WINNER"), ("KXSERIEAGAME", "WINNER"),
               ("KXBUNDESLIGAGAME", "WINNER"), ("KXLIGUE1GAME", "WINNER")],
    "cricket": [("KXODIMATCH", "WINNER"), ("KXHUNDREDMATCH", "WINNER"),
                ("KXT20MATCH", "WINNER")],
}

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
_START_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})[A-Z]")


def parse_start(ticker: str) -> datetime | None:
    """Ticker-embedded start, ET -> UTC. Unparseable -> None (start-unknown),
    NEVER close_time (bug D1)."""
    m = _START_RE.search(ticker)
    if not m or m.group(2) not in _MONTHS:
        return None
    yy, mon, dd, hh, mm = m.groups()
    try:
        local = datetime(2000 + int(yy), _MONTHS[mon], int(dd), int(hh), int(mm), tzinfo=NY)
    except ValueError:
        return None
    return local.astimezone(timezone.utc)


# Five title grammars ship across the sports series (surveyed 2026-07-29). All of
# them wrap the same "<away> vs <home>" core, so strip the wrapper, then split.
_LEAD = re.compile(r"^Will\s+.+?\s+win\s+the\s+", re.I)      # NFL/NCAAF/ATP/WTA/UFC
_TAIL = re.compile(
    r"\s+(?:Winner|Total\s+Runs|Total\s+Points)\b.*$"        # MLB/NBA/soccer
    r"|\s+(?:men's|women's)\s+.*$"                           # WNBA/cricket
    r"|\s+(?:Pro|professional|college)\s+.*$"                # NFL/UFC/NCAAF
    r"|:\s*.*$",                                             # "...: <outcome>"
    re.I)
_VS = re.compile(r"\s+vs\.?\s+", re.I)
# One-on-one sports: the pair identity comes from the two competitors' full
# names, not from the title. See _combat_names_by_event.
COMBAT_SPORTS = {"tennis", "mma"}


def _teams_from_title(title: str) -> tuple[str, str]:
    """(away, home). An uncovered grammar returns ('', '') — never a half-parsed
    team: matching requires an alias hit and must not be handed a wrapper (§8.1)."""
    base = _TAIL.sub("", _LEAD.sub("", title.strip())).strip().rstrip("?").strip()
    parts = _VS.split(base)
    if len(parts) == 2 and all(p.strip() for p in parts):
        return parts[0].strip(), parts[1].strip()
    return "", ""


def _empty_book(m: dict) -> bool:
    """Empty-book trap (§6.1): ask 1.0000 + bid 0.0000 + liquidity 0 is not a quote."""
    return (m.get("yes_ask_dollars") == "1.0000" and m.get("yes_bid_dollars") == "0.0000"
            and Decimal(m.get("liquidity_dollars") or "0") == 0)


def _combat_names_by_event(markets: list[dict]) -> dict[str, tuple[str, str]]:
    """Tennis/MMA titles carry surnames only ('Humbert vs Shelton'), and
    normalize_team's surname+initial rule needs two tokens — so a title-derived
    name never matches Polymarket's full names. Each event's two markets do
    carry the full names in yes_sub_title; join them. Order is irrelevant:
    matching keys the pair as a frozenset (§8.1)."""
    by_event: dict[str, list[str]] = {}
    for m in markets:
        sub = (m.get("yes_sub_title") or "").strip()
        if sub and m.get("event_ticker"):
            by_event.setdefault(m["event_ticker"], []).append(sub)
    # exactly two distinct competitors, or we do not claim to know the pair
    return {ev: (names[0], names[1]) for ev, names in by_event.items()
            if len(names) == 2 and names[0] != names[1]}


def parse_markets(cfg, markets: list[dict], sport: str, market_type: str) -> list[Quote]:
    quotes: list[Quote] = []
    now = datetime.now(timezone.utc)
    combat = _combat_names_by_event(markets) if sport in COMBAT_SPORTS else {}
    for m in markets:
        if _empty_book(m):
            continue
        start = parse_start(m["ticker"])
        # NB: default None, never ("", "") — a non-empty tuple is truthy and
        # would swallow the title fallback for every non-combat sport
        away, home = combat.get(m.get("event_ticker", "")) \
            or _teams_from_title(m.get("title", ""))
        vol = Decimal(m["volume_24h_fp"]) if m.get("volume_24h_fp") is not None else None
        if market_type == "TOTAL":
            line = str(Decimal(str(m["floor_strike"]))) if m.get("floor_strike") is not None else None
            outcome = "OVER"  # yes side buys Over floor_strike
        else:
            line = None
            outcome = m.get("yes_sub_title", "")
        common = dict(
            event_key="", venue="kalshi", book="kalshi", market_type=market_type,
            line=line, outcome_label=outcome, ladder_json=None,
            live=is_live(start, now),
            fetched_at=now, category="sports", sport=sport, volume_24h=vol,
            home=home, away=away, start_utc=start,
            start_grade="nominal" if start else "absent",
            venue_market_id=m["ticker"],
        )
        for side, ask_key, bid_key, size_key in (
                ("YES", "yes_ask_dollars", "yes_bid_dollars", "yes_ask_size_fp"),
                ("NO", "no_ask_dollars", "no_bid_dollars", "yes_bid_size_fp")):
            ask = Decimal(m[ask_key])
            size = m.get(size_key)
            depth = Decimal(size) * ask if size is not None else None
            quotes.append(Quote(
                side=side, venue_side=side, raw_price=ask, cost=ask,
                fee_cost=leg_fee_cost(cfg, "kalshi", "sports", ask),
                bid=Decimal(m[bid_key]), depth_usd=depth, **common))
    return quotes


def fetch_markets(cfg) -> list[Quote]:
    # serial by design: one host, few series — the deadline still bounds it (H7)
    deadline = Deadline(cfg["execution"]["venue_deadline_secs"])
    quotes = []
    for sport in cfg["sports"]:
        for series, mtype in SPORT_SERIES.get(sport, []):
            cursor, pages = "", 0
            while pages < 10:
                if deadline.expired():  # truncated -> partial, never full coverage
                    raise PartialFetch(quotes)
                url = f"{BASE}/markets?series_ticker={series}&status=open&limit=200"
                if cursor:
                    url += f"&cursor={cursor}"
                data = get_json(url, cfg, deadline=deadline)
                quotes.extend(parse_markets(cfg, data.get("markets", []), sport, mtype))
                cursor = data.get("cursor") or ""
                pages += 1
                if not cursor:
                    break
            else:
                if cursor:  # cap hit with pages pending — never truncate silently
                    log.warning("kalshi pagination cap hit for %s; coverage truncated",
                                series)
    return quotes


def fetch_orderbook(cfg, ticker: str) -> dict:
    """orderbook_fp: per-side BID levels [price_dollars, contracts]; the ask of one
    side derives as 1 - best opposite bid (verified vs market object 2026-07-08)."""
    return get_json(f"{BASE}/markets/{ticker}/orderbook", cfg).get("orderbook_fp", {})
