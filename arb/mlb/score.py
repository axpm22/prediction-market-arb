"""Prereg §7 scoring on a fills table (one row per filled signal).

Required columns: gamePk, date_et, family (HR|HIT|KS), pnl, cost, fair, won.
Unit of inference = game (all fills in one game are one bet, §6).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def game_units(fills: pd.DataFrame) -> pd.DataFrame:
    return (fills.groupby(["gamePk", "date_et"], as_index=False)
            .agg(pnl=("pnl", "sum"), cost=("cost", "sum"), n=("pnl", "size"))
            .sort_values(["date_et", "gamePk"]))


def cluster_bootstrap_p(units: pd.DataFrame, n_boot=10_000, seed=0) -> float:
    """One-sided p for mean game P&L > 0: share of recentred resamples >= observed."""
    x = units.pnl.to_numpy(float)
    if len(x) < 2:
        return float("nan")
    rng = np.random.default_rng(seed)
    obs = x.mean()
    boot = rng.choice(x - obs, (n_boot, len(x)), replace=True).mean(axis=1)
    return float((boot >= obs).mean())


def folds_positive(units: pd.DataFrame, k=4) -> list[float]:
    """Net P&L in k chronological folds of roughly equal game count."""
    x = units.pnl.to_numpy(float)
    return [float(c.sum()) for c in np.array_split(x, k)] if len(x) >= k else []


def concentration(fills: pd.DataFrame, units: pd.DataFrame) -> dict:
    pos = units.pnl.clip(lower=0)
    top = pos.sort_values(ascending=False)
    k = max(1, int(np.ceil(0.05 * len(units))))
    fam = fills.groupby("family").pnl.sum().clip(lower=0)
    return {
        "top5pct_games_share_of_pos": float(top.iloc[:k].sum() / pos.sum()) if pos.sum() > 0 else None,
        "max_family_share_of_pos": float(fam.max() / fam.sum()) if fam.sum() > 0 else None,
        "family_pnl": fills.groupby("family").pnl.sum().round(4).to_dict(),
    }


def max_drawdown(units: pd.DataFrame) -> float:
    eq = units.groupby("date_et").pnl.sum().cumsum()
    return float((eq.cummax() - eq).max()) if len(eq) else 0.0


def summarize(fills: pd.DataFrame, n_signals: int, alpha_k=3) -> dict:
    if fills.empty:
        return {"n_signals": n_signals, "n_fills": 0, "verdict_inputs": "no fills"}
    u = game_units(fills)
    p = cluster_bootstrap_p(u)
    folds = folds_positive(u)
    conc = concentration(fills, u)
    out = {
        "n_signals": n_signals, "n_fills": int(len(fills)),
        "fill_rate": len(fills) / n_signals if n_signals else None,
        "n_games": int(len(u)), "n_dates": int(u.date_et.nunique()),
        "pnl": round(float(u.pnl.sum()), 4), "cost": round(float(u.cost.sum()), 4),
        "pnl_per_dollar": float(u.pnl.sum() / u.cost.sum()),
        "hit_rate": float(fills.won.mean()), "pred_hit_rate": float(fills.fair.mean()),
        "max_drawdown": round(max_drawdown(u), 4),
        "p_one_sided": p, "folds": folds, **conc,
    }
    out["E1"] = bool(out["pnl"] > 0 and p < 0.05 / alpha_k and out["n_games"] >= 100
                     and out["n_dates"] >= 20)
    out["E2"] = bool(sum(f > 0 for f in folds) >= 3)
    out["E3"] = bool((conc["max_family_share_of_pos"] or 1) <= 0.40
                     and (conc["top5pct_games_share_of_pos"] or 1) < 0.50)
    return out


if __name__ == "__main__":  # self-check
    rng = np.random.default_rng(1)
    f = pd.DataFrame({"gamePk": np.repeat(np.arange(200), 2), "date_et": np.repeat(
        [f"2026-09-{d:02d}" for d in rng.integers(1, 28, 200)], 2),
        "family": rng.choice(["HR", "HIT", "KS"], 400), "pnl": rng.normal(0.05, 1, 400),
        "cost": 5.0, "fair": 0.5, "won": rng.random(400) < 0.5})
    s = summarize(f, 500)
    assert s["n_games"] == 200 and 0 <= s["p_one_sided"] <= 1 and len(s["folds"]) == 4
    z = f.assign(pnl=-1.0)
    assert summarize(z, 400)["E1"] is False
    print("ok", {k: s[k] for k in ("pnl", "p_one_sided", "E1", "E2", "E3")})
