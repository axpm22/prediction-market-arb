"""v2 calibration of the sim's day-form latents on OUTCOMES only (no prices).

Fit set: 2026 regular-season games 2026-04-01..2026-08-29 (the discovery date range;
all strictly before the sealed confirmation dates). Grid over (sp_sd, off_sd, form_mu),
objective = summed Brier over team runs >= {1,3,5,7,9}, SP KS >= {3..8}, HR 1+, HIT 1+.
Writes reports/mlb_corr/fit_v2.json.
"""
from __future__ import annotations

import itertools
import json
from multiprocessing import Pool

import numpy as np
import pandas as pd
import yaml

from arb.mlb.calibrate import SLOT_PA, eligible_games
from arb.mlb.data import inputs_asof
from arb.mlb.sim import simulate

# starter's share of slot PAs, 2026-04-01..08-29 boxscores (battingOrder x00 vs x0n)
SLOT_SHARE = [0.970, 0.973, 0.973, 0.972, 0.963, 0.950, 0.943, 0.928, 0.918]
STARTER_SHARE = [round((pa * sh - 3) / (pa - 3), 3) for pa, sh in zip(SLOT_PA, SLOT_SHARE)]
GRID = list(itertools.product([0.0, 0.15, 0.3, 0.45], [0.0, 0.1, 0.2, 0.3], [0.0, -0.05, -0.1, -0.15]))
N_GAMES, N_SIMS = 300, 2000


def _game(args):
    pk, ya, yh, ks, props = args
    inp = inputs_asof(pk)
    out = []
    for sp_sd, off_sd, mu in GRID:
        r = simulate(inp, n_sims=N_SIMS, n_chunks=20, seed=pk, form_sd=(sp_sd, off_sd),
                     form_mu=mu, starter_share=STARTER_SHARE)
        b = 0.0
        for s, y in ((0, ya), (1, yh)):
            for k in (1, 3, 5, 7, 9):
                b += ((r.runs[:, s] >= k).mean() - (y >= k)) ** 2
            if ks[s] is not None:
                for k in range(3, 9):
                    b += ((r.sp_k[:, s] >= k).mean() - (ks[s] >= k)) ** 2
        for s, j, hr, h in props:
            b += ((r.bat_hr[:, s, j] >= 1).mean() - (hr >= 1)) ** 2
            b += ((r.bat_h[:, s, j] >= 1).mean() - (h >= 1)) ** 2
        out.append(b)
    return out


def main():
    cfg = yaml.safe_load(open("config.yaml"))["mlb"]
    g = eligible_games(cfg).sample(N_GAMES, random_state=7)
    pg = pd.read_parquet("data/mlb/player_game.parquet")
    lu = pd.read_parquet("data/mlb/lineups.parquet")
    jobs = []
    for row in g.itertuples():
        x = pg[pg.gamePk == row.gamePk].set_index("id")
        l = lu[lu.gamePk == row.gamePk]
        ks = []
        for side in ("away", "home"):
            sp = l[(l.side == side) & (l.slot == 0)].id
            ks.append(int(x.k_p.get(sp.iloc[0], 0)) if len(sp) else None)
        props = [(0 if r.side == "away" else 1, r.slot - 1, int(x.hr.get(r.id, 0)), int(x.h.get(r.id, 0)))
                 for r in l[l.slot > 0].itertuples() if r.id in x.index]
        jobs.append((int(row.gamePk), row.away_runs, row.home_runs, ks, props))
    with Pool(6) as p:
        tot = np.sum(p.map(_game, jobs), axis=0)
    best = int(np.argmin(tot))
    res = {"n_games": N_GAMES, "n_sims": N_SIMS, "starter_share": STARTER_SHARE,
           "grid": [[*gp, float(t)] for gp, t in zip(GRID, tot)],
           "best": {"sp_sd": GRID[best][0], "off_sd": GRID[best][1], "form_mu": GRID[best][2],
                    "brier_sum": float(tot[best])},
           "v1_equiv_brier_sum": float(tot[GRID.index((0.0, 0.0, 0.0))])}
    json.dump(res, open("reports/mlb_corr/fit_v2.json", "w"), indent=1)
    print(res["best"], "v1-equivalent", res["v1_equiv_brier_sum"])


if __name__ == "__main__":
    main()
