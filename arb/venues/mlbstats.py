"""MLB Stats API (SPEC §6.4, §11): free, keyless, unlimited history. Primary MLB
settlement oracle AND start-time truth (start_grade=verified outranks venues).
gamePk/gameNumber disambiguate doubleheaders deterministically (§8.2)."""
from __future__ import annotations

from datetime import datetime, timezone

from arb.venues.base import get_json

BASE = "https://statsapi.mlb.com/api/v1"
FINAL_STATES = {"Final", "Game Over", "Completed Early"}


def parse_schedule(payload: dict) -> list[dict]:
    games = []
    for d in payload.get("dates", []):
        for g in d.get("games", []):
            games.append({
                "gamePk": g["gamePk"],
                "gameNumber": g.get("gameNumber", 1),
                "doubleHeader": g.get("doubleHeader", "N"),
                "start_utc": datetime.fromisoformat(
                    g["gameDate"].replace("Z", "+00:00")).astimezone(timezone.utc),
                "home": g["teams"]["home"]["team"]["name"],
                "away": g["teams"]["away"]["team"]["name"],
                "state": g["status"].get("detailedState", ""),
                "home_score": g["teams"]["home"].get("score"),
                "away_score": g["teams"]["away"].get("score"),
            })
    return games


def fetch_schedule(cfg, date_str: str) -> list[dict]:
    return parse_schedule(get_json(f"{BASE}/schedule?sportId=1&date={date_str}", cfg))


def is_final(game: dict) -> bool:
    return game["state"] in FINAL_STATES
