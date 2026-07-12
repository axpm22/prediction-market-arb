"""The Odds API adapter (SPEC §6.4) — consensus + scores; credit-metered.

RULE 2: never called from tests or loops; live calls only from scan/settle.
Logs x-requests-remaining on every call (redacted sinks); warn <100, abort <50.
No key -> callers no-op gracefully. Fixture oddsapi_h2h.json is a REAL response
recorded 2026-07-08 (data/verified_facts.md)."""
from __future__ import annotations

import os
from decimal import Decimal

import requests

from arb.logutil import get_logger, redact
from arb.venues.base import VenueError

BASE = "https://api.the-odds-api.com/v4"
log = get_logger("arb.oddsapi", "logs/scan.log")
LAST_REMAINING: int | None = None  # last observed x-requests-remaining (this process)


class CreditsLow(Exception):
    pass


def api_key() -> str | None:
    return os.environ.get("ODDS_API_KEY") or None


def _get(cfg, path: str, params: dict) -> tuple[list, int | None]:
    params = dict(params, apiKey=api_key())
    resp = requests.get(f"{BASE}{path}", params=params,
                        timeout=float(cfg["execution"]["venue_fetch_timeout_secs"]))
    remaining = resp.headers.get("x-requests-remaining")
    remaining = int(float(remaining)) if remaining else None
    global LAST_REMAINING
    LAST_REMAINING = remaining
    log.info("oddsapi %s -> %s, credits remaining %s", path, resp.status_code, remaining)
    if remaining is not None:
        if remaining < int(cfg["odds_api"]["abort_below_credits"]):
            raise CreditsLow(f"{remaining} credits left — aborting (rule 2)")
        if remaining < int(cfg["odds_api"]["warn_below_credits"]):
            log.warning("odds api credits low: %s", remaining)
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        # requests embeds the full URL incl. apiKey in the message; an
        # uncaught traceback bypasses log redaction (M3)
        raise VenueError(redact(str(e))) from None
    return resp.json(), remaining


def parse_events(events: list[dict], sport_key: str) -> list[dict]:
    """Flatten to per-(book, market, outcome) rows with Decimal odds and string line.
    Lines go through matching.line_key so '11.0' and 11 key identically (§8.3)."""
    from arb.matching import line_key
    rows = []
    for ev in events:
        for bm in ev.get("bookmakers", []):
            for mk in bm.get("markets", []):
                for oc in mk.get("outcomes", []):
                    rows.append({
                        "sport_key": sport_key,
                        "commence_time": ev["commence_time"],
                        "home": ev["home_team"], "away": ev["away_team"],
                        "book": bm["key"], "market": mk["key"],
                        "outcome": oc["name"],
                        "line": line_key(oc["point"]) if "point" in oc else None,
                        "odds": Decimal(str(oc["price"])),
                    })
    return rows


def fetch_odds(cfg, sport_key: str) -> list[dict]:
    markets = cfg["odds_api_markets"].get(sport_key, cfg["odds_api_markets"]["default"])
    events, _ = _get(cfg, f"/sports/{sport_key}/odds/", {
        "regions": cfg["odds_api"]["region"], "markets": markets, "oddsFormat": "decimal"})
    return parse_events(events, sport_key)


def fetch_scores(cfg, sport_key: str) -> list[dict]:
    scores, _ = _get(cfg, f"/sports/{sport_key}/scores/",
                     {"daysFrom": int(cfg["settle_days_from"])})
    return scores
