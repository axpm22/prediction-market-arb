"""Load the discovered political/econ/weather series cache.

data/politics_series.json is produced by a one-time discovery probe (which Kalshi series
in those categories currently have open markets). Re-probe when a category goes quiet —
same maintenance pattern as the sports series cache.
"""
from __future__ import annotations

import json
from pathlib import Path

CACHE_PATH = Path("data/politics_series.json")


def load_series(categories: list[str], path: Path = CACHE_PATH) -> list[dict]:
    if not path.exists():
        raise RuntimeError(
            f"Missing {path}; run the political discovery probe first "
            "(GET /series?category=Politics|Economics|Climate and Weather)."
        )
    data = json.loads(path.read_text())
    wanted = set(categories)
    return [s for s in data.get("series", []) if s.get("category") in wanted]
