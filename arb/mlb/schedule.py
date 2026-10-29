"""2026 MLB schedule cache + the prereg's discovery/confirmation game sets.
MLB Stats API (keyless). Writes data/mlb/games.parquet."""
from __future__ import annotations

import json
import urllib.request

import numpy as np
import pandas as pd

BASE = "https://statsapi.mlb.com/api/v1"
OUT = "data/mlb/games.parquet"


def fetch(start="2026-03-20", end="2026-11-05") -> pd.DataFrame:
    url = (f"{BASE}/schedule?sportId=1&startDate={start}&endDate={end}"
           "&gameType=R,F,D,L,W&hydrate=team,linescore")
    payload = json.load(urllib.request.urlopen(url, timeout=60))
    rows = []
    for d in payload["dates"]:
        for g in d["games"]:
            ls = g.get("linescore") or {}
            rows.append({
                "gamePk": g["gamePk"], "game_type": g["gameType"],
                "official_date": g.get("officialDate"),
                "start_utc": pd.Timestamp(g["gameDate"]),
                "rescheduled_from": g.get("rescheduledFrom"),
                "resumed_from": g.get("resumedFrom"),
                "game_number": g.get("gameNumber", 1),
                "double_header": g.get("doubleHeader", "N"),
                "home": g["teams"]["home"]["team"]["abbreviation"],
                "away": g["teams"]["away"]["team"]["abbreviation"],
                "venue_id": g["venue"]["id"],
                "state": g["status"]["detailedState"],
                "coded_state": g["status"].get("codedGameState"),
                "innings": ls.get("currentInning"),
                "home_runs": g["teams"]["home"].get("score"),
                "away_runs": g["teams"]["away"].get("score"),
            })
    df = pd.DataFrame(rows).drop_duplicates("gamePk", keep="last")
    df["date_et"] = df.start_utc.dt.tz_convert("America/New_York").dt.strftime("%Y-%m-%d")
    return df


def game_sets(df: pd.DataFrame, cfg: dict) -> tuple[list[int], list[int]]:
    """Prereg §2: discovery = seeded uniform draw over sorted gamePks; confirmation = all."""
    reg = df[df.game_type == "R"]
    d, c = cfg["discovery"], cfg["confirmation"]
    pool = sorted(reg[(reg.date_et >= d["start"]) & (reg.date_et <= d["end"])].gamePk)
    disc = sorted(np.random.default_rng(d["seed"]).choice(pool, d["n"], replace=False).tolist())
    conf = sorted(reg[(reg.date_et >= c["start"]) & (reg.date_et <= c["end"])].gamePk)
    return disc, conf


if __name__ == "__main__":
    import yaml
    cfg = yaml.safe_load(open("config.yaml"))["mlb"]
    df = fetch()
    df.to_parquet(OUT)
    disc, conf = game_sets(df, cfg)
    json.dump({"discovery": disc, "confirmation": conf}, open("reports/mlb_corr/game_sets.json", "w"))
    print(len(df), df.game_type.value_counts().to_dict(), "disc", len(disc), "conf", len(conf))
    print(df.state.value_counts().to_dict())
