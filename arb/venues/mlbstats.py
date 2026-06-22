"""MLB Stats API settlement source — free, keyless, unlimited, no lookback window.

Used by `settle-backfill` to resolve EV/ARB bets the Odds API scores endpoint can no
longer reach (its `daysFrom` caps at 3 days, so games older than that are orphaned).
This source has all of history, so it rescues the backlog and removes the credit
pressure from routine settlement.

Verified June 2026 against the live endpoint (CLAUDE.md rule #5):
  GET https://statsapi.mlb.com/api/v1/schedule?sportId=1&date=YYYY-MM-DD
Fields used (confirmed present): dates[].games[] with
  status.abstractGameState == "Final",  gameNumber (doubleheaders),
  teams.{home,away}.team.name,  teams.{home,away}.score.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover
    requests = None

BASE_URL = "https://statsapi.mlb.com/api/v1/schedule"
SPORT_ID_MLB = 1


class MlbStatsAdapter:
    """Returns final-score records for a date, or reads a recorded fixture offline."""

    def __init__(self, fixture_dir: Path | None = None, timeout: int = 20):
        self.fixture_dir = fixture_dir
        self.timeout = timeout

    def _raw_for_date(self, date_iso: str) -> dict[str, Any]:
        if self.fixture_dir is not None:
            path = self.fixture_dir / f"mlbstats_schedule_{date_iso}.json"
            return json.loads(path.read_text())
        if requests is None:  # pragma: no cover
            raise RuntimeError("requests is required for live MLB Stats settlement.")
        resp = requests.get(
            BASE_URL,
            params={"sportId": SPORT_ID_MLB, "date": date_iso},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def final_games(self, date_iso: str) -> list[dict[str, Any]]:
        """Completed games on a UTC date as
        {home, away, home_score, away_score, game_number}. Non-final games are skipped."""
        raw = self._raw_for_date(date_iso)
        games: list[dict[str, Any]] = []
        for day in raw.get("dates", []):
            for game in day.get("games", []):
                status = (game.get("status") or {}).get("abstractGameState")
                if status != "Final":
                    continue
                teams = game.get("teams") or {}
                home, away = teams.get("home") or {}, teams.get("away") or {}
                if home.get("score") is None or away.get("score") is None:
                    continue
                games.append(
                    dict(
                        home=str((home.get("team") or {}).get("name", "")),
                        away=str((away.get("team") or {}).get("name", "")),
                        home_score=home["score"],
                        away_score=away["score"],
                        game_number=game.get("gameNumber", 1),
                        game_date=game.get("gameDate"),  # ISO UTC, e.g. 2026-06-17T23:41:00Z
                    )
                )
        return games
