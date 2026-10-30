"""Lane P (PREREG_CORR3_v1): NFL passing-yards identity, QB arm and REC arm. Paper only.

Per team-game: fit N(mu, sigma) to every player's "X+ yards" ladder mids, R = sum mu_rec / mu_QB,
R_bar = median R over discovery team-games. QB arm prices QB rungs at mu*_QB = sum mu_rec / R_bar;
REC arm scales each receiver mu by mu_QB * R_bar / sum mu_rec. 200 refits (mids ~ U[bid, ask],
R_bar bootstrapped over discovery team-games) give fair / lo / hi. Signal, fill and fee reuse
arb.mlb.lane. Unit of inference = game (Common rules: all fills in one event are one unit).

    .venv/bin/python -m arb.corr.passing discovery
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3
import sys
import urllib.request
from decimal import Decimal

import numpy as np
import pandas as pd
from scipy.optimize import least_squares
from scipy.stats import norm

from arb.mlb.lane import candle_rows, decide, fill, quote
from arb.mlb.lane import norm as nname

SER = {"KXNFLPASSYDS": "QB", "KXNFLRECYDS": "REC"}
TEAMS = {"ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN", "DET", "GB", "HOU",
         "IND", "JAC", "JAX", "KC", "LA", "LAR", "LAC", "LV", "MIA", "MIN", "NE", "NO", "NYG", "NYJ",
         "PHI", "PIT", "SEA", "SF", "TB", "TEN", "WAS", "WSH"}
RX = re.compile(r"If (.+?) records (\d+)\+ (passing|receiving) yards in the")
MULT = Decimal("1")  # all lane series quadratic x1 (INVENTORY fee table; fee_changes empty)
NREF, BAND, SEED = 200, (0.05, 0.95), 7
D = "data/corr"


# ---------- parsing ----------
def split_teams(ev: str):
    """'26OCT29CARGB' -> ('CAR', 'GB') = (away, home)."""
    s = ev.split("-")[-1][7:]
    for i in (2, 3):
        if s[:i] in TEAMS and s[i:] in TEAMS:
            return s[:i], s[i:]
    return None


def parse(ticker: str, rules: str) -> dict | None:
    ser, ev, pseg, x = ticker.split("-")
    teams, m = split_teams(ev), RX.search(rules or "")
    if ser not in SER or not teams or not m or int(m.group(2)) != int(x):
        return None
    team = [t for t in teams if pseg.startswith(t)]
    if len(team) != 1:  # ponytail: ambiguous prefix (e.g. LA vs LAC game) -> drop the market
        return None
    return {"series": ser, "role": SER[ser], "team": team[0], "side": "away" if team[0] == teams[0] else "home",
            "player": m.group(1), "pid": pseg, "X": int(x)}


# ---------- normal ladder ----------
def sf(X, mu, sd):
    """P(Y >= X) for integer yards: continuity-corrected at X - 0.5."""
    return norm.sf(np.asarray(X, float) - 0.5, mu, sd)


def fit_normal(X, p):
    """Least-squares N(mu, sigma) to ladder mids; None unless >= 2 mids in [0.05, 0.95]."""
    X, p = np.asarray(X, float), np.asarray(p, float)
    k = (p >= BAND[0]) & (p <= BAND[1])
    if k.sum() < 2:
        return None
    X, p = X[k], p[k]
    mu0 = X[np.argmin(abs(p - 0.5))]
    r = least_squares(lambda th: sf(X, th[0], th[1]) - p, [mu0, max(10.0, 0.4 * abs(mu0))],
                      bounds=([-300, 1], [900, 500]))
    return float(r.x[0]), float(r.x[1])


# ---------- data ----------
def load(setname: str):
    if setname != "discovery" and not os.path.exists("reports/corr/CONFIRM_OPENED_P.md"):
        sys.exit("confirmation is sealed: commit reports/corr/CONFIRM_OPENED_P.md first (prereg)")
    cpath = f"{D}/P_disc.sqlite" if setname == "discovery" else f"{D}/P_confirm.sqlite"
    mk = sqlite3.connect(f"file:{D}/markets.sqlite?mode=ro", uri=True)
    # coordinator convention (all lanes): puller T_ts = exp - D - 30 is the buffered kickoff estimate,
    # so T = T_ts - 30 min; an in-play check failure subtracts a further 30 (candles start at T_ts - 75).
    ev = pd.read_sql("SELECT game_key, date_et, T_ts - 1800 AS T, kickoff_src FROM events WHERE lane='P' AND set_name=?",
                     mk, params=["disc" if setname == "discovery" else "confirm"])
    m = pd.read_sql("SELECT ticker, event_ticker, series, game_key, rules_primary, result, settlement_value, "
                    "expected_expiration_time FROM markets WHERE series IN ('KXNFLPASSYDS','KXNFLRECYDS')", mk)
    m = m[m.game_key.isin(ev.game_key)]
    p = [parse(t, r) for t, r in zip(m.ticker, m.rules_primary)]
    m = m.assign(**pd.DataFrame([x or {} for x in p], index=m.index))[[x is not None for x in p]]
    cdb = sqlite3.connect(f"file:{cpath}?mode=ro", uri=True)
    cand = dict(cdb.execute("SELECT ticker, raw_json FROM candles_raw WHERE status=200").fetchall())
    return ev, m, cand


def settled_yes(r) -> float | None:
    """1/0 for yes/no settlement; None for fair-price / void (excluded from P&L, prereg)."""
    y = {"yes": 1.0, "no": 0.0}.get(str(r.result).lower())
    sv = r.settlement_value
    return y if y is not None and (sv is None or pd.isna(sv) or sv in (0, 1, 100)) else None


# ---------- in-play check ----------
def inplay_first(cmap: dict, tickers, T: int):
    """First 1-min candle (game aggregate volume) with vol > 5x median pregame minute (minutes <= T-15)."""
    frames = [candle_rows(cmap[t])[["ts", "vol"]] for t in tickers if t in cmap]
    if not frames:
        return None, None
    v = pd.concat(frames).groupby("ts").vol.sum()
    v = v.reindex(range(int(v.index.min()), int(v.index.max()) + 60, 60), fill_value=0.0)
    med = float(v[v.index <= T - 900].median()) if (v.index <= T - 900).any() else 0.0
    thr = 5 * max(med, 1.0)  # ponytail: floor of 1 contract so an all-zero pregame median isn't "any trade"
    hit = v[(v.index > T - 900) & (v > thr)]
    return (int(hit.index[0]) if len(hit) else None), med


# ---------- per team-game ----------
def build(ev, m, cand, buffer_extra=0):
    """Quotes at T and point fits; returns dict game_key -> game dict."""
    games = {}
    for e in ev.itertuples():
        T = int(e.T) - 60 * buffer_extra
        g = m[m.game_key == e.game_key]
        mk = []
        for r in g.itertuples():
            q = quote(candle_rows(cand[r.ticker]), T) if r.ticker in cand else None
            if q is not None:
                mk.append({"ticker": r.ticker, "role": r.role, "team": r.team, "side": r.side, "pid": r.pid,
                           "player": r.player, "X": r.X, "q": q, "y": settled_yes(r),
                           "result": str(r.result).lower()})
        units = {}
        for team, side in {(r.team, r.side) for r in g.itertuples()}:
            qbs = set(g[(g.team == team) & (g.role == "QB")].pid)
            if len(qbs) != 1:
                continue
            players = {}
            for x in mk:
                if x["team"] == team:
                    players.setdefault((x["role"], x["pid"]), []).append(x)
            for v in players.values():
                v.sort(key=lambda x: x["X"])
            qb = players.get(("QB", next(iter(qbs))))
            recs = {k[1]: v for k, v in players.items() if k[0] == "REC"}
            fq = qb and fit_normal([x["X"] for x in qb], [x["q"]["mid"] for x in qb])
            fr = {pid: f for pid, v in recs.items()
                  if (f := fit_normal([x["X"] for x in v], [x["q"]["mid"] for x in v]))}
            if not fq or not fr or fq[0] <= 0:
                continue
            units[team] = {"side": side, "qb": qb, "recs": {p: recs[p] for p in fr}, "fq": fq, "fr": fr,
                           "R": sum(f[0] for f in fr.values()) / fq[0]}
        games[e.game_key] = {"game_key": e.game_key, "date_et": e.date_et, "T": T, "mkts": mk, "units": units,
                             "all_tickers": list(g.ticker)}
    return games


def refits(u, rng):
    """NREF draws of (mu, sd) for the QB and each fitted receiver, mids ~ U[bid, ask]."""
    def draw(ladder):
        X = [x["X"] for x in ladder]
        out = []
        for _ in range(NREF):
            p = [rng.uniform(x["q"]["bid"], x["q"]["ask"]) for x in ladder]
            out.append(fit_normal(X, p) or (np.nan, np.nan))
        return np.array(out)
    u["dq"] = draw(u["qb"])
    u["dr"] = {p: draw(v) for p, v in u["recs"].items()}


def arm_fairs(u, Rb, anchor=None):
    """{ticker: (fair, lo, hi)} for both arms. Rb: NREF R_bar draws. anchor: other unit for N1 (by role)."""
    a = anchor or u
    S_rec = np.nansum([d[:, 0] for d in a["dr"].values()], axis=0)  # anchors of QB arm
    mu_q_anchor = a["dq"][:, 0]                                      # anchor of REC arm
    out = {"QB": {}, "REC": {}}
    mu_star, sd_q = S_rec / Rb, u["dq"][:, 1]
    for x in u["qb"]:
        out["QB"][x["ticker"]] = sf(x["X"], mu_star, sd_q)
    S_own = np.nansum([d[:, 0] for d in u["dr"].values()], axis=0)
    scale = mu_q_anchor * Rb / S_own
    for pid, lad in u["recs"].items():
        d = u["dr"][pid]
        for x in lad:
            out["REC"][x["ticker"]] = sf(x["X"], d[:, 0] * scale, d[:, 1])
    return {arm: {t: (float(np.nanmean(v)), float(np.nanpercentile(v, 5)), float(np.nanpercentile(v, 95)))
                  for t, v in d.items() if np.isfinite(v).sum() >= NREF // 2} for arm, d in out.items()}


def trade(G, fairs, sigs, fills, sanity=None, flip=False):
    by_t = {x["ticker"]: x for x in G["mkts"]}
    for arm, d in fairs.items():
        for t, (fair, lo, hi) in d.items():
            x = by_t[t]
            if x["y"] is None:  # fair-price settlement: excluded
                continue
            if sanity is not None:
                sanity.append({"arm": arm, "mid": x["q"]["mid"], "model": fair, "y": x["y"]})
            dd = decide(x, fair, lo, hi, MULT)
            if dd is None:
                continue
            side, entry = dd
            if flip:
                side = "no" if side == "yes" else "yes"
                entry = 1 - x["q"]["entry_bid"] if side == "no" else x["q"]["entry_ask"]
            base = {"gamePk": G["game_key"], "date_et": G["date_et"], "ticker": t, "family": arm,
                    "side": side, "fair": fair, "lo": lo, "hi": hi, "mid": x["q"]["mid"]}
            sigs.append(base)
            f = fill(x, side, entry, MULT, fair)
            if f:
                fills.append({**base, **f})


# ---------- outcome check (ESPN keyless box scores) ----------
ESPN = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"


def _get(url, path):
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with urllib.request.urlopen(url, timeout=30) as r, open(path, "wb") as f:
            f.write(r.read())
    return json.load(open(path))


def espn_box(date_et: str):
    """{(team_abbr): {"kickoff": ts, "pass": {name: yds}, "rec": {name: yds}}} for one ET date."""
    d = date_et.replace("-", "")
    sb = _get(f"{ESPN}/scoreboard?dates={d}", f"{D}/espn/sb_{d}.json")
    out = {}
    for e in sb.get("events", []):
        ko = int(dt.datetime.fromisoformat(e["date"].replace("Z", "+00:00")).timestamp())
        s = _get(f"{ESPN}/summary?event={e['id']}", f"{D}/espn/sum_{e['id']}.json")
        for tm in s.get("boxscore", {}).get("players", []):
            ab = tm["team"]["abbreviation"]
            rec = {"kickoff": ko, "pass": {}, "rec": {}}
            for st in tm.get("statistics", []):
                if st["name"] not in ("passing", "receiving"):
                    continue
                i = st["labels"].index("YDS")
                for a in st["athletes"]:
                    rec["pass" if st["name"] == "passing" else "rec"][nname(a["athlete"]["displayName"])] = int(a["stats"][i])
            out[ab] = rec
    return out


ESPN_ABBR = {"JAC": "JAX", "LA": "LAR", "WAS": "WSH"}


def outcome_check(games):
    rows = []
    for G in games.values():
        try:
            box = espn_box(G["date_et"])
        except Exception as ex:  # network is optional for the descriptive check
            return {"error": repr(ex)}
        for team, u in G["units"].items():
            b = box.get(ESPN_ABBR.get(team, team))
            if not b:
                continue
            qbn = nname(u["qb"][0]["player"])
            names = [nname(v[0]["player"]) for v in u["recs"].values()]
            if qbn not in b["pass"]:
                continue
            rows.append({"game_key": G["game_key"], "team": team, "qb_pass": b["pass"][qbn],
                         "listed_rec": sum(b["rec"].get(n, 0) for n in names),
                         "all_rec": sum(b["rec"].values()), "all_pass": sum(b["pass"].values()), "n_missing": sum(n not in b["rec"] for n in names),
                         "R_mkt": u["R"], "kickoff": b["kickoff"], "T": G["T"]})
    df = pd.DataFrame(rows)
    if df.empty:
        return {"n": 0}
    df = df.assign(R_real=df.listed_rec / df.qb_pass.where(df.qb_pass > 0))
    return {"n": int(len(df)), "identity_all_rec_minus_all_pass_mean": float((df.all_rec - df.all_pass).mean()),
            "identity_exact_share": float((df.all_rec == df.all_pass).mean()),
            "listed_qb_share_of_team_pass": float((df.qb_pass / df.all_pass.where(df.all_pass > 0)).mean()),
            "R_realized_median": float(df.R_real.median()), "R_realized_mean": float(df.R_real.mean()),
            "R_realized_p10_p90": [float(df.R_real.quantile(.1)), float(df.R_real.quantile(.9))],
            "R_market_median": float(df.R_mkt.median()),
            "corr_R_market_vs_realized": float(df[["R_mkt", "R_real"]].corr().iloc[0, 1]),
            "listed_names_missing_per_unit": float(df.n_missing.mean()),
            "_df": df}


# ---------- run ----------
def main(setname="discovery"):
    from arb.mlb.score import summarize
    ev, m, cand = load(setname)
    ev = ev.sort_values(["date_et", "game_key"]).reset_index(drop=True)
    rng = np.random.default_rng(SEED)

    # in-play check (prereg Common rules): 20 discovery events
    chk = ev.sample(min(20, len(ev)), random_state=SEED)
    ip = []
    for e in chk.itertuples():
        first, med = inplay_first(cand, m[m.game_key == e.game_key].ticker, int(e.T))
        ip.append({"game_key": e.game_key, "T": int(e.T), "first_inplay": first, "pregame_median_vol": med,
                   "ok": first is None or first > int(e.T)})
    buffer_extra = 0 if all(x["ok"] for x in ip) else 30
    # Validity filter (disclosed deviation): Kalshi expected_expiration is kickoff+3h for some NFL games and
    # kickoff+6h for others, so exp-D puts T in-play for the latter. Keep only games whose ESPN kickoff is
    # >= 15 min after the final T (quote + fill window pregame). Prices are never used for this.
    ko = {}
    for e in ev.itertuples():
        b = espn_box(e.date_et).get(ESPN_ABBR.get(t := split_teams(e.game_key)[1], t))
        ko[e.game_key] = b["kickoff"] if b else None
    ev = ev.assign(kickoff=ev.game_key.map(ko))
    dropped = ev[~(ev.kickoff >= ev["T"] - 60 * buffer_extra + 900)]
    ev = ev.drop(dropped.index)
    games = build(ev, m, cand, buffer_extra)

    units = [(G, t, u) for G in games.values() for t, u in G["units"].items()]
    Rs = np.array([u["R"] for _, _, u in units])
    Rbar = float(np.median(Rs))
    Rb = np.array([np.median(rng.choice(Rs, len(Rs))) for _ in range(NREF)])
    for _, _, u in units:
        refits(u, rng)

    res = {k: {"sigs": [], "fills": []} for k in ("primary", "N1", "N2", "N3")}
    sanity = []
    for G in games.values():
        same = [H for H in games.values() if H["date_et"] == G["date_et"] and H is not G and H["units"]]
        for team, u in G["units"].items():
            trade(G, arm_fairs(u, Rb), res["primary"]["sigs"], res["primary"]["fills"], sanity)
            trade(G, arm_fairs(u, Rb), res["N3"]["sigs"], res["N3"]["fills"], flip=True)
            if same:  # N1: anchors from another same-date game, same role (away/home side) if present
                H = same[rng.integers(len(same))]
                a = next((v for v in H["units"].values() if v["side"] == u["side"]), next(iter(H["units"].values())))
                trade(G, arm_fairs(u, Rb, anchor=a), res["N1"]["sigs"], res["N1"]["fills"])
                # N2: R_bar replaced by a game-shuffled same-date R
                Rs_same = [v["R"] for v in H["units"].values()]
                trade(G, arm_fairs(u, np.full(NREF, Rs_same[rng.integers(len(Rs_same))])),
                      res["N2"]["sigs"], res["N2"]["fills"])

    out = {"set": setname, "lane": "P", "inplay_check": ip, "buffer_extra_min": buffer_extra,
           "n_events": int(len(ev)), "dropped_T_not_pregame": dropped.game_key.tolist(),
           "T_before_kickoff_min_kept": [float(((ev.kickoff - ev["T"]) / 60 + buffer_extra).min()),
                                         float(((ev.kickoff - ev["T"]) / 60 + buffer_extra).max())], "n_games_with_units": sum(bool(G["units"]) for G in games.values()),
           "n_team_games": len(units), "n_receivers_per_unit_median": float(np.median([len(u["fr"]) for *_, u in units])) if units else None,
           "R_bar": Rbar, "R_dispersion": {"sd": float(Rs.std()), "p10": float(np.quantile(Rs, .1)),
                                            "p90": float(np.quantile(Rs, .9)), "min": float(Rs.min()), "max": float(Rs.max())},
           "R_bar_boot_p5_p95": [float(np.percentile(Rb, 5)), float(np.percentile(Rb, 95))],
           "n_target_quotes": {arm: sum(1 for s in sanity if s["arm"] == arm) for arm in ("QB", "REC")}}
    os.makedirs("reports/corr", exist_ok=True)
    for k, v in res.items():
        f = pd.DataFrame(v["fills"])
        out[k] = {}
        for arm in ("QB", "REC"):
            fa = f[f.family == arm] if not f.empty else f
            s = summarize(fa, sum(x["family"] == arm for x in v["sigs"]), alpha_k=7)
            if "top5pct_games_share_of_pos" in s:  # E3 per prereg: single league (NFL), top-5% events < 50%
                s["E3"] = bool((s["top5pct_games_share_of_pos"] or 1) < 0.5)
            out[k][arm] = s
        f.to_csv(f"reports/corr/fills_P_{setname}_{k}.csv", index=False)
    san = pd.DataFrame(sanity)
    out["brier"] = {arm: {"n": int(len(x)), "market": float(((x.mid - x.y) ** 2).mean()),
                          "model": float(((x.model - x.y) ** 2).mean())} for arm, x in san.groupby("arm")}
    oc = outcome_check(games)
    df = oc.pop("_df", None)
    if df is not None:
        df.to_csv(f"reports/corr/outcome_P_{setname}.csv", index=False)
        oc["T_before_kickoff_min"] = {"min": float(((df.kickoff - df["T"]) / 60).min()),
                                      "median": float(((df.kickoff - df["T"]) / 60).median())}
    out["outcome_check"] = oc
    json.dump(out, open(f"reports/corr/results_P_{setname}.json", "w"), indent=1, default=float)
    print(json.dumps({k: v for k, v in out.items() if k != "inplay_check"}, indent=1, default=float)[:8000])


if __name__ == "__main__":
    main(*sys.argv[1:])
