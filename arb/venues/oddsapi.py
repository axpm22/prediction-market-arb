from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import requests

from arb.config import AppConfig
from arb.matching import canonical_sport, event_key, load_aliases, normalize_name
from arb.models import Quote
from arb.pricing import implied_from_decimal_odds, q6

BASE_URL = "https://api.the-odds-api.com/v4"
# SPEC §3.4: the only consensus markets in scope.
MARKETS = "h2h,totals"


class OddsApiCreditsLow(RuntimeError):
    """Raised when x-requests-remaining drops below the configured abort threshold."""


class OddsApiAdapter:
    name = "oddsapi"

    def __init__(self, config: AppConfig, fixture_dir: Path | None = None) -> None:
        self.config = config
        self.fixture_dir = fixture_dir
        self.credits_remaining: int | None = None

    def fetch_quotes(self, sport: str) -> list[Quote]:
        aliases = self.config.odds_api_sport_aliases
        if self.fixture_dir is not None:
            data = json.loads((self.fixture_dir / f"oddsapi_{sport}.json").read_text())
            return parse_odds_events(data, sport, aliases)
        if not self.config.odds_api_key:
            raise RuntimeError("ODDS_API_KEY is required for live Odds API scans.")
        # The fetch URL uses the real Odds-API key; only the event_key prefix is canonicalized.
        url = f"{BASE_URL}/sports/{sport}/odds/"
        response = requests.get(
            url,
            params={
                "apiKey": self.config.odds_api_key,
                "regions": self.config.odds_api_region,
                # Per-key markets: venues quote tennis WINNER-only, so a totals fetch
                # there can never join a venue price and only burns a credit.
                "markets": self.config.odds_api_markets.get(
                    sport, self.config.odds_api_markets.get("default", MARKETS)),
                "oddsFormat": "decimal",
            },
            timeout=30,
        )
        response.raise_for_status()
        self._track_credits(response.headers.get("x-requests-remaining"))
        return parse_odds_events(response.json(), sport, aliases)

    def fetch_scores(self, sport: str, days_from: int) -> list[dict[str, Any]]:
        # SPEC §9: EV settlement comes from Odds API scores, fetched daily by `settle`.
        if self.fixture_dir is not None:
            return json.loads((self.fixture_dir / f"oddsapi_scores_{sport}.json").read_text())
        if not self.config.odds_api_key:
            raise RuntimeError("ODDS_API_KEY is required to fetch scores for settlement.")
        response = requests.get(
            f"{BASE_URL}/sports/{sport}/scores/",
            params={"apiKey": self.config.odds_api_key, "daysFrom": days_from},
            timeout=30,
        )
        response.raise_for_status()
        self._track_credits(response.headers.get("x-requests-remaining"))
        return response.json()

    def _track_credits(self, remaining_header: str | None) -> None:
        # CLAUDE.md rule 2: log x-requests-remaining on every live call.
        print(f"oddsapi: x-requests-remaining={remaining_header}")
        self.credits_remaining = (
            int(remaining_header) if remaining_header and remaining_header.isdigit() else None
        )
        if self.credits_remaining is None:
            return
        if self.credits_remaining < self.config.odds_api_abort_below_credits:
            raise OddsApiCreditsLow(
                f"Odds API credits below abort threshold "
                f"({self.credits_remaining} < {self.config.odds_api_abort_below_credits}); aborting scan."
            )
        if self.credits_remaining < self.config.odds_api_warn_below_credits:
            print(
                f"oddsapi: WARNING credits low "
                f"({self.credits_remaining} < {self.config.odds_api_warn_below_credits})"
            )


def parse_odds_events(events: list[dict[str, Any]], sport: str,
                      sport_aliases: dict[str, str] | None = None) -> list[Quote]:
    fetched_at = datetime.now(timezone.utc)
    aliases = load_aliases()
    # Normalize the Odds-API sport key to the venue-canonical prefix so consensus event_keys
    # join venue quotes (e.g. soccer_fifa_world_cup -> soccer). Identity when unmapped.
    canonical = canonical_sport(sport, sport_aliases)
    quotes: list[Quote] = []
    for event in events:
        start = datetime.fromisoformat(event["commence_time"].replace("Z", "+00:00"))
        home = event["home_team"]
        away = event["away_team"]
        key = event_key(canonical, home, away, start, aliases)
        for bookmaker in event.get("bookmakers", []):
            for market in bookmaker.get("markets", []):
                if market.get("key") not in {"h2h", "totals"}:
                    continue
                # EV#6: the book's own last line-move time. The Odds API reports it per market
                # (newer responses) and per bookmaker (older). Prefer the market's.
                last_update = market.get("last_update") or bookmaker.get("last_update")
                for outcome in market.get("outcomes", []):
                    quotes.append(
                        _quote_from_outcome(
                            market_key=str(market["key"]),
                            outcome=outcome,
                            sport=canonical,
                            key=key,
                            book=str(bookmaker["key"]),
                            start=start,
                            home=home,
                            away=away,
                            aliases=aliases,
                            fetched_at=fetched_at,
                            last_update=last_update,
                        )
                    )
    return quotes


def _quote_from_outcome(
    market_key: str,
    outcome: dict[str, Any],
    sport: str,
    key: str,
    book: str,
    start: datetime,
    home: str,
    away: str,
    aliases: dict[str, dict[str, str]],
    fetched_at: datetime,
    last_update: str | None = None,
) -> Quote:
    price = Decimal(str(outcome["price"]))
    source_updated_at = (
        datetime.fromisoformat(last_update.replace("Z", "+00:00")) if last_update else None
    )
    if market_key == "totals":
        market_type = "TOTAL"
        label = str(outcome["name"]).upper()
        line = Decimal(str(outcome["point"]))
    else:
        market_type = "WINNER"
        label = normalize_name(str(outcome["name"]), sport, aliases)
        line = None
    return Quote(
        event_key=key,
        venue="oddsapi",
        book=book,
        market_type=market_type,
        line=line,
        outcome_label=label,
        raw_price=price,
        cost=q6(implied_from_decimal_odds(price)),
        fee_cost=Decimal("0"),
        depth_usd=None,
        live=False,
        fetched_at=fetched_at,
        start_utc=start,
        home=normalize_name(home, sport, aliases),
        away=normalize_name(away, sport, aliases),
        source_updated_at=source_updated_at,
    )
