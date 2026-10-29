"""Prereg §8: outcome-only calibration of the untilted simulator (no prices).

Games: 2026 regular season, discovery date range, excluding cancelled/resumed/shortened.
Marginals: team runs >= k, total over x, home win, HR 1+, HIT 1+/2+, SP KS k+.
Benchmark: naive independence (player prop = Poisson(expected PA x player rate),
team runs independent of player outcomes). Writes reports/mlb_corr/calibration.{json,md}.
"""
from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import yaml

from arb.mlb.data import inputs_asof
from arb.mlb.sim import simulate

OUT = "reports/mlb_corr/calibration"
# ponytail: fixed league PA-by-slot table for the naive benchmark only
SLOT_PA = [4.65, 4.55, 4.45, 4.35, 4.25, 4.15, 4.05, 3.95, 3.85]
K, BB, S1, S2, S3, HR, OUT_ = range(7)


def poisson_ge(lam: float, k: int) -> float:
    return 1.0 - sum(math.exp(-lam) * lam ** i / math.factorial(i) for i in range(k))


def eligible_games(cfg: dict) -> pd.DataFrame:
    g = pd.read_parquet("data/mlb/games.parquet")
    d = cfg["discovery"]
    g = g[(g.game_type == "R") & (g.date_et >= d["start"]) & (g.date_et <= d["end"])]
    return g[(g.state == "Final") & g.resumed_from.isna() & (g.innings >= 9)]


def naive_rate(alpha, idx) -> float:
    a = np.asarray(alpha, float)
    return float(a[idx].sum() / a.sum())


def game_rows(pk: int, pg: pd.DataFrame, game: pd.Series) -> tuple[list, list]:
    inp = inputs_asof(pk)
    r = simulate(inp, seed=pk)
    runs = r.runs  # [away, home]
    actual = {"away": game.away_runs, "home": game.home_runs}
    rows, cond = [], []
    for s, side in enumerate(("away", "home")):
        opp = 1 - s
        for k in range(3, 8):
            p = float((runs[:, s] >= k).mean())
            rows.append(("team_runs_ge%d" % k, p, p, actual[side] >= k))
        sp = inp[side]["sp"]
        spk = pg.get(sp["id"])
        if spk is not None:
            lam = sp["exp_bf"] * naive_rate(sp["alpha"], [K])
            for k in range(3, 9):
                pm = float((r.sp_k[:, s] >= k).mean())
                rows.append(("KS%d" % k, pm, poisson_ge(lam, k), spk.k_p >= k))
            m7 = r.sp_k[:, s] >= 7
            cond.append(("opp_ge5|SP_K7", float((m7 & (runs[:, opp] >= 5)).mean()),
                         float(m7.mean() * (runs[:, opp] >= 5).mean()), float(m7.mean()),
                         spk.k_p >= 7, actual["home" if side == "away" else "away"] >= 5))
        for j, b in enumerate(inp[side]["lineup"]):
            st = pg.get(b["id"])
            if st is None:
                continue
            ePA = SLOT_PA[j]
            hr = r.bat_hr[:, s, j] >= 1
            for fam, sim_ind, rate_idx, k, y in (
                    ("HR1", hr, [HR], 1, st.hr >= 1),
                    ("HIT1", r.bat_h[:, s, j] >= 1, [S1, S2, S3, HR], 1, st.h >= 1),
                    ("HIT2", r.bat_h[:, s, j] >= 2, [S1, S2, S3, HR], 2, st.h >= 2)):
                rows.append((fam, float(sim_ind.mean()),
                             poisson_ge(ePA * naive_rate(b["alpha"], rate_idx), k), y))
            t5 = runs[:, s] >= 5
            cond.append(("team_ge5|HR", float((hr & t5).mean()), float(hr.mean() * t5.mean()),
                         float(hr.mean()), st.hr >= 1, actual[side] >= 5))
    tot = runs.sum(1)
    for x in (7.5, 8.5, 9.5):
        p = float((tot > x).mean())
        rows.append(("total_over%.1f" % x, p, p, game.home_runs + game.away_runs > x))
    p = float((runs[:, 1] > runs[:, 0]).mean())
    rows.append(("home_win", p, p, game.home_runs > game.away_runs))
    return rows, cond


def reliability(df: pd.DataFrame, col: str) -> list:
    b = pd.cut(df[col], np.linspace(0, 1, 11), include_lowest=True)
    t = df.groupby(b, observed=True).agg(n=("y", "size"), pred=(col, "mean"), obs=("y", "mean"))
    return [[str(i), int(r.n), round(r.pred, 4), round(r.obs, 4)] for i, r in t.iterrows()]


def main():
    cfg = yaml.safe_load(open("config.yaml"))["mlb"]
    games = eligible_games(cfg)
    pgd = pd.read_parquet("data/mlb/player_game.parquet")
    rows, cond = [], []
    for i, g in enumerate(games.itertuples()):
        pg = {r.id: r for r in pgd[pgd.gamePk == g.gamePk].itertuples()}
        rr, cc = game_rows(g.gamePk, pg, g)
        rows += rr
        cond += cc
        if i % 200 == 0:
            print(i, len(games), flush=True)
    df = pd.DataFrame(rows, columns=["family", "p_model", "p_naive", "y"]).astype({"y": float})
    out = {"n_games": int(len(games)), "families": {}}
    for fam, f in df.groupby("family"):
        out["families"][fam] = {
            "n": int(len(f)), "base_rate": round(f.y.mean(), 4),
            "brier_model": round(((f.p_model - f.y) ** 2).mean(), 5),
            "brier_naive": round(((f.p_naive - f.y) ** 2).mean(), 5),
            "brier_climo": round(((f.y.mean() - f.y) ** 2).mean(), 5),
            "mean_pred_model": round(f.p_model.mean(), 4),
            "reliability_model": reliability(f, "p_model")}
    c = pd.DataFrame(cond, columns=["cond", "p_joint", "p_joint_naive", "p_given", "y_given", "y_out"])
    out["conditionals"] = {}
    for name, f in c.groupby("cond"):
        hit = f[f.y_given.astype(bool)]
        out["conditionals"][name] = {
            "n_given_events": int(len(hit)),
            "empirical": round(hit.y_out.astype(float).mean(), 4),
            "model": round(f.p_joint.sum() / f.p_given.sum(), 4),
            "naive_independence": round(f.p_joint_naive.sum() / f.p_given.sum(), 4),
            "empirical_unconditional": round(f.y_out.astype(float).mean(), 4)}
    json.dump(out, open(OUT + ".json", "w"), indent=1)
    print(json.dumps({k: {x: v[x] for x in ("n", "brier_model", "brier_naive", "brier_climo",
                                             "base_rate", "mean_pred_model")}
                      for k, v in out["families"].items()}, indent=1))
    print(json.dumps(out["conditionals"], indent=1))


if __name__ == "__main__":
    main()
