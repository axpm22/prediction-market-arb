from __future__ import annotations

import json
import re
import string
from datetime import datetime, timezone
from pathlib import Path

try:
    from rapidfuzz import fuzz
except ModuleNotFoundError:  # pragma: no cover
    fuzz = None

ALIAS_PATH = Path("data/aliases.json")
PUNCT_TABLE = str.maketrans("", "", string.punctuation)


def load_aliases(path: Path = ALIAS_PATH) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _basic_normalize(name: str) -> str:
    name = name.lower().translate(PUNCT_TABLE)
    name = re.sub(r"\b(fc|sc|cf|the)\b", " ", name)
    return re.sub(r"\s+", " ", name).strip()


def normalize_name(name: str, sport: str, aliases: dict[str, dict[str, str]] | None = None) -> str:
    raw = _basic_normalize(name)
    aliases = aliases if aliases is not None else load_aliases()
    sport_aliases = aliases.get(sport, {})
    return sport_aliases.get(raw, raw)


def event_key(sport: str, home: str, away: str, start_utc: datetime, aliases: dict[str, dict[str, str]] | None = None) -> str:
    # #4: NO sub-day bucket. A 6h floor bucket split two venues listing the same game at
    # 17:55 vs 18:05 (and any pair straddling a 6h boundary — evening games cluster at the
    # 18:00 UTC line), silently dropping real cross-venue arbs. Key on sport+date+pair only;
    # doubleheaders (same teams, same day, hours apart) are caught by the start-drift gate
    # (suspended, not mis-bet) and settled by each leg's exact stored start time.
    names = sorted([normalize_name(home, sport, aliases), normalize_name(away, sport, aliases)])
    start = start_utc.astimezone(timezone.utc)
    return f"{sport}:{start.date().isoformat()}:{names[0]}:{names[1]}"


def token_ratio(left: str, right: str) -> int:
    if fuzz is not None:
        return int(fuzz.token_set_ratio(left, right))
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    if not left_tokens or not right_tokens:
        return 0
    overlap = len(left_tokens & right_tokens)
    return int(200 * overlap / (len(left_tokens) + len(right_tokens)))


def fuzzy_same_team(left: str, right: str, threshold: int) -> bool:
    # Threshold comes from config.yaml (fuzzy_match_threshold); no inline default.
    return token_ratio(left, right) >= threshold

