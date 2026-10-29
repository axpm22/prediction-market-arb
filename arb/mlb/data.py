"""MLB data layer for the paper-only correlated-market study (PREREG_MLB_CORR_v1 §4/§5).

Steps (python -m arb.mlb.data <pa|box|park|all>):
  pa    Statcast PAs, 2025 + 2026 regular seasons -> data/mlb/pa.parquet
  box   MLB Stats API boxscores for every 2026 gamePk -> lineups / player_game parquet
  park  2025 park factors (by home-team abbrev) -> data/mlb/park.parquet

inputs_asof(gamePk) is the sim's input contract; it uses only PAs dated strictly
before the game's ET date (2025 full + 2026 through D-1).

Outcome mapping (Statcast `events` on PA-ending rows), order K, BB, 1B, 2B, 3B, HR, OUT:
  K   strikeout, strikeout_double_play
  BB  walk, intent_walk, hit_by_pitch, catcher_interf (batter awarded first)
  1B/2B/3B/HR  single, double, triple, home_run
  OUT everything else that ends a PA: field_out, force_out, grounded_into_double_play,
      double_play, triple_play, fielders_choice(_out), field_error, sac_fly(_double_play),
      sac_bunt(_double_play), other_out ...
  Dropped (no PA ends): truncated_pa, and non-PA events (caught_stealing_*, pickoff_*,
  wild_pitch, passed_ball, stolen_base_*, other_advance, game_advisory) if they appear.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

import numpy as np
import pandas as pd

D = "data/mlb"
OUTCOMES = ["K", "BB", "1B", "2B", "3B", "HR", "OUT"]
SEASONS = {2025: ("2025-03-18", "2025-09-28"), 2026: ("2026-03-25", "2026-09-27")}
EVENT_MAP = {"strikeout": "K", "strikeout_double_play": "K",
             "walk": "BB", "intent_walk": "BB", "hit_by_pitch": "BB", "catcher_interf": "BB",
             "single": "1B", "double": "2B", "triple": "3B", "home_run": "HR"}
NON_PA = {"truncated_pa", "caught_stealing_2b", "caught_stealing_3b", "caught_stealing_home",
          "pickoff_1b", "pickoff_2b", "pickoff_3b", "pickoff_caught_stealing_2b",
          "pickoff_caught_stealing_3b", "pickoff_caught_stealing_home", "wild_pitch",
          "passed_ball", "stolen_base_2b", "stolen_base_3b", "stolen_base_home",
          "other_advance", "game_advisory", "pickoff_error_1b", "pickoff_error_2b",
          "pickoff_error_3b", "balk"}
KEEP = ["game_pk", "game_date", "game_type", "batter", "pitcher", "events", "home_team",
        "away_team", "inning", "inning_topbot", "at_bat_number"]


def map_outcome(events: pd.Series) -> pd.Series:
    """Statcast events -> outcome label; None for non-PA events (dropped)."""
    out = events.map(EVENT_MAP).fillna("OUT")
    return out.where(events.notna() & ~events.isin(NON_PA))


# ---------------------------------------------------------------- step 1: Statcast
def _chunks(start: str, end: str, days: int = 6):
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    while s <= e:
        t = min(s + pd.Timedelta(days=days - 1), e)
        yield s.strftime("%Y-%m-%d"), t.strftime("%Y-%m-%d")
        s = t + pd.Timedelta(days=1)


def _fetch_chunk(a: str, b: str) -> pd.DataFrame:
    """One cached Statcast chunk: PA-ending rows + is_sp flag (from all pitches)."""
    path = f"{D}/statcast/{a}_{b}.parquet"
    if os.path.exists(path):
        return pd.read_parquet(path)
    from pybaseball import statcast
    raw = statcast(a, b, verbose=False)
    if raw is None or raw.empty:
        df = pd.DataFrame(columns=KEEP + ["is_sp"])
    else:
        raw = raw[raw.game_type == "R"]
        # starter = pitcher of the earliest pitch thrown by each fielding side
        raw = raw.assign(fld=np.where(raw.inning_topbot == "Top", "home", "away"))
        first = (raw.sort_values(["at_bat_number", "pitch_number"])
                 .groupby(["game_pk", "fld"]).pitcher.first().rename("sp").reset_index())
        df = raw[raw.events.notna()].merge(first, on=["game_pk", "fld"], how="left")
        df["is_sp"] = df.pitcher == df.sp
        df = df[KEEP + ["is_sp"]].copy()
    os.makedirs(f"{D}/statcast", exist_ok=True)
    df.to_parquet(path)
    return df


def build_pa() -> pd.DataFrame:
    parts = []
    for yr, (a, b) in SEASONS.items():
        for x, y in _chunks(a, b):
            t0 = time.time()
            parts.append(_fetch_chunk(x, y))
            print(f"{x}..{y}: {len(parts[-1])} PA rows ({time.time() - t0:.0f}s)", flush=True)
    pa = pd.concat(parts, ignore_index=True)
    pa["outcome"] = map_outcome(pa.events)
    unknown = sorted(set(pa.events.dropna()) - set(EVENT_MAP) - NON_PA - {
        "field_out", "force_out", "grounded_into_double_play", "double_play", "triple_play",
        "fielders_choice", "fielders_choice_out", "field_error", "sac_fly",
        "sac_fly_double_play", "sac_bunt", "sac_bunt_double_play", "other_out"})
    if unknown:
        print("WARNING events mapped to OUT by default:", unknown)
    pa = pa[pa.outcome.notna()].drop_duplicates(["game_pk", "at_bat_number"])
    pa["game_date"] = pd.to_datetime(pa.game_date).dt.strftime("%Y-%m-%d")
    pa[["game_pk", "batter", "pitcher", "inning"]] = pa[["game_pk", "batter", "pitcher", "inning"]].astype("int64")
    pa["is_sp"] = pa.is_sp.astype(bool)
    pa.to_parquet(f"{D}/pa.parquet")
    pa["season"] = pa.game_date.str[:4]
    print(pa.groupby("season").outcome.value_counts(normalize=True).unstack()[OUTCOMES].round(4))
    print(pa.groupby("season").size())
    return pa


# ---------------------------------------------------------------- step 2: boxscores
def _box(pk: int) -> dict:
    path = f"{D}/box/{pk}.json"
    if not os.path.exists(path):
        os.makedirs(f"{D}/box", exist_ok=True)
        url = f"https://statsapi.mlb.com/api/v1/game/{pk}/boxscore"
        body = urllib.request.urlopen(url, timeout=30).read()
        open(path, "wb").write(body)
        time.sleep(0.2)
    return json.load(open(path))


def build_box() -> None:
    """lineups.parquet: slot 1-9 = starting order, slot 0 = starting pitcher."""
    games = pd.read_parquet(f"{D}/games.parquet")
    games = games[games.state != "Cancelled"]
    lineup, pg, failed = [], [], []
    for pk in games.gamePk:
        try:
            b = _box(int(pk))
        except Exception as e:  # noqa: BLE001 - report and carry on
            failed.append((int(pk), repr(e)))
            continue
        for side in ("home", "away"):
            t = b["teams"][side]
            abbr = t["team"].get("abbreviation")
            sp = t["pitchers"][0] if t["pitchers"] else None
            for p in t["players"].values():
                pid, name = p["person"]["id"], p["person"]["fullName"]
                bo = int(p.get("battingOrder") or 0)
                bat, pit = p["stats"].get("batting", {}), p["stats"].get("pitching", {})
                if bo and bo % 100 == 0:
                    lineup.append((int(pk), side, abbr, bo // 100, pid, name))
                if pid == sp:
                    lineup.append((int(pk), side, abbr, 0, pid, name))
                if not bat and not pit:
                    continue
                pg.append({"gamePk": int(pk), "id": pid, "name": name, "side": side, "team": abbr,
                           "started": bool(bo and bo % 100 == 0) or pid == sp,
                           "pa": bat.get("plateAppearances", 0), "h": bat.get("hits", 0),
                           "hr": bat.get("homeRuns", 0), "rbi": bat.get("rbi", 0),
                           "tb": bat.get("totalBases", 0), "k_p": pit.get("strikeOuts", 0),
                           "bf": pit.get("battersFaced", 0), "outs": pit.get("outs", 0),
                           "sp": pid == sp})
    lu = pd.DataFrame(lineup, columns=["gamePk", "side", "team", "slot", "id", "name"])
    lu.to_parquet(f"{D}/lineups.parquet")
    pd.DataFrame(pg).to_parquet(f"{D}/player_game.parquet")
    n = lu[lu.slot > 0].groupby(["gamePk", "side"]).size()
    print(f"lineups {len(lu)} rows, {lu.gamePk.nunique()} games; sides != 9 batters: "
          f"{(n != 9).sum()}; player_game {len(pg)} rows; failed {failed}")


# ---------------------------------------------------------------- step 3: park factors
def build_park(shrink: float) -> pd.DataFrame:
    """Per 2025 venue_id: (x_home + W*r_road) / ((n_home + W) * r_road), where 'road' =
    all PAs (both teams) in the road games of the teams that hosted at that venue."""
    from arb.mlb.schedule import fetch
    g25 = fetch("2025-03-01", "2025-10-01")[["gamePk", "venue_id"]]
    pa = pd.read_parquet(f"{D}/pa.parquet")
    pa = pa[pa.game_date < "2026"].merge(g25, left_on="game_pk", right_on="gamePk")
    rows = []
    for v, home in pa.groupby("venue_id"):
        hosts = home.home_team.unique()
        road = pa[pa.away_team.isin(hosts)]
        row = {"venue_id": v, "hosts": ",".join(hosts), "n_home": len(home), "n_road": len(road)}
        for k in ("HR", "1B", "2B", "3B"):
            r_road = (road.outcome == k).mean()
            row[k] = ((home.outcome == k).sum() + shrink * r_road) / ((len(home) + shrink) * r_road)
        rows.append(row)
    park = pd.DataFrame(rows)
    park.to_parquet(f"{D}/park.parquet")
    print(park.sort_values("HR").round(3).to_string())
    return park


# ---------------------------------------------------------------- step 4: sim inputs
def shrink_alpha(x: np.ndarray, league: np.ndarray, w: np.ndarray) -> np.ndarray:
    """x: 7 counts, league: 7 probs, w: 6 prior weights (K..HR). OUT = residual,
    renormalised. Returns Dirichlet alpha = r * (n + mean(w))."""
    n = x.sum()
    r = np.empty(7)
    r[:6] = (x[:6] + w * league[:6]) / (n + w)
    r[6] = max(1.0 - r[:6].sum(), 1e-6)
    return r / r.sum() * (n + w.mean())


def _cum(df: pd.DataFrame, key: str) -> dict:
    """{key: (sorted dates, cumulative 7-count matrix)} for O(log n) as-of lookups."""
    c = (df.groupby([key, "game_date", "outcome"]).size().unstack(fill_value=0)
         .reindex(columns=OUTCOMES, fill_value=0).groupby(level=0).cumsum())
    return {k: (g.index.get_level_values(1).to_numpy(), g.to_numpy())
            for k, g in c.groupby(level=0)}


def _asof(cum: dict, k, date: str) -> np.ndarray:
    """Counts from all rows dated strictly before `date`."""
    if k not in cum:
        return np.zeros(7)
    dates, m = cum[k]
    i = np.searchsorted(dates, date, side="left")
    return m[i - 1].astype(float) if i else np.zeros(7)


class _Store:
    def __init__(self, pa: pd.DataFrame, cfg: dict):
        pa = pa.assign(fld=np.where(pa.inning_topbot == "Top", pa.home_team, pa.away_team), one=0)
        self.cfg = cfg
        self.w_b = np.array([cfg["shrink_pa"]["batter"][k] for k in OUTCOMES[:6]], float)
        self.w_p = np.array([cfg["shrink_pa"]["pitcher"][k] for k in OUTCOMES[:6]], float)
        self.league = _cum(pa, "one")
        self.bat = _cum(pa, "batter")
        self.pit = _cum(pa, "pitcher")
        pen = pa[~pa.is_sp]
        self.pen26 = _cum(pen[pen.game_date >= "2026"], "fld")
        self.pen25 = _cum(pen[pen.game_date < "2026"], "fld")
        self.games = pd.read_parquet(f"{D}/games.parquet").set_index("gamePk")
        self.lineups = {k: g for k, g in pd.read_parquet(f"{D}/lineups.parquet").groupby("gamePk")}
        pg = pd.read_parquet(f"{D}/player_game.parquet")
        st = pg[pg.sp].merge(self.games.date_et.reset_index(), on="gamePk")
        self.starts = {k: (g.date_et.to_numpy(), g.bf.cumsum().to_numpy())
                       for k, g in st.sort_values("date_et").groupby("id")}
        park = pd.read_parquet(f"{D}/park.parquet").set_index("venue_id")
        self.park = park[["HR", "1B", "2B", "3B"]].to_dict("index")

    def exp_bf(self, pid: int, date: str) -> float:
        d0, w = self.cfg["starter_bf_default"], 3
        if pid not in self.starts:
            return float(d0)
        dates, cbf = self.starts[pid]
        i = np.searchsorted(dates, date, side="left")
        return float(((cbf[i - 1] if i else 0) + w * d0) / (i + w))

    def side(self, pk: int, side: str, date: str, league: np.ndarray) -> dict:
        lu = self.lineups[pk]
        lu = lu[lu.side == side].sort_values("slot")
        bats, sp = lu[lu.slot > 0], lu[lu.slot == 0].iloc[0]
        team = sp.team  # Stats API and Statcast abbreviations are identical (checked 2026-10-29)
        prior = shrink_alpha(_asof(self.pen25, team, "2026"), league, self.w_p)
        prior = prior / prior.sum()
        return {"lineup": [{"id": int(r.id), "name": r.name,
                            "alpha": shrink_alpha(_asof(self.bat, r.id, date), league, self.w_b).tolist()}
                           for r in bats.itertuples()],
                "sp": {"id": int(sp.id), "name": sp["name"],
                       "alpha": shrink_alpha(_asof(self.pit, sp.id, date), league, self.w_p).tolist(),
                       "exp_bf": self.exp_bf(int(sp.id), date)},
                "pen": {"alpha": shrink_alpha(_asof(self.pen26, team, date), prior, self.w_p).tolist()}}


_STORE: _Store | None = None


def inputs_asof(gamePk: int) -> dict:
    """Sim input contract (see module docstring). Hard rule: only PAs dated < game ET date."""
    global _STORE
    if _STORE is None:
        import yaml
        _STORE = _Store(pd.read_parquet(f"{D}/pa.parquet"), yaml.safe_load(open("config.yaml"))["mlb"])
    s, pk = _STORE, int(gamePk)
    g = s.games.loc[pk]
    league = _asof(s.league, 0, g.date_et)
    league = league / league.sum()
    return {"gamePk": pk, "regular_season": g.game_type == "R", "league": league.tolist(),
            "park": s.park.get(g.venue_id, {"HR": 1.0, "1B": 1.0, "2B": 1.0, "3B": 1.0}),
            "home": s.side(pk, "home", g.date_et, league),
            "away": s.side(pk, "away", g.date_et, league)}


if __name__ == "__main__":
    import yaml
    step = sys.argv[1] if len(sys.argv) > 1 else "all"
    if step in ("pa", "all"):
        build_pa()
    if step in ("box", "all"):
        build_box()
    if step in ("park", "all"):
        build_park(yaml.safe_load(open("config.yaml"))["mlb"]["park_shrink_pa"])
