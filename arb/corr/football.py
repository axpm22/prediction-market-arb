"""PREREG_CORR3_v1 Lane F: football TEAMTOTAL vs GAME/SPREAD/TOTAL anchors (paper only, no network).

Usage (repo root):  .venv/bin/python -m arb.corr.football inplay|rho|run [disc]
  inplay : §Common in-play check on 20 discovery events per league -> buffer decision
  rho    : realized corr(M,S) per league from settled ladders (outcomes only) -> F_rho_discovery.json
  run    : signals/fills/nulls on discovery -> results_F_discovery.json + fills CSVs
Confirmation is sealed: refuses unless reports/corr/CONFIRM_OPENED_F.md exists.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3
import sys
import zlib
from decimal import Decimal

import numpy as np
import pandas as pd
from scipy.optimize import least_squares
from scipy.stats import norm

from arb.mlb.lane import candle_rows, decide, fee, fill, quote

MK = "data/corr/markets.sqlite"
OUT = "reports/corr"
MULT = Decimal("1")          # fee_changes: no multiplier changes; all lane series quadratic x1
N_REFIT = 200
SET_DB = {"disc": "data/corr/F_disc.sqlite", "confirm": "data/corr/F_confirm.sqlite"}
# pull.py stores T_ts = exp - D - 30min; prereg T = exp - D - 30min(buffer) - 30min.
PREREG_EXTRA_S = 30 * 60
KIND_RE = re.compile(r"KX(NFL|NCAAF)(TEAMTOTAL|SPREAD|TOTAL|GAME)$")
LINE_RE = re.compile(r"over ([\d.]+)", re.I)


# ---------- parsing ----------
def parse(series: str, ticker: str, yes_sub_title: str):
    """-> (league, kind, team|None, line|None). Line is the half-point 'over X' strike."""
    m = KIND_RE.match(series)
    if not m:
        return None
    league, kind = m.groups()
    suf = ticker.rsplit("-", 1)[1]
    if kind == "GAME":
        return league, kind, suf, None
    lm = LINE_RE.search(yes_sub_title or "")
    line = float(lm.group(1)) if lm else None
    if kind == "TOTAL":
        return league, kind, None, line
    tm = re.match(r"([A-Z]+?)\d+$", suf)
    return league, kind, (tm.group(1) if tm else None), line


# ---------- model ----------
def fit_normal(s, L, y, x0):
    """Least squares on mids: P(s*X > L) = Phi((s*mu - L)/sigma). -> (mu, sigma)."""
    s, L, y = (np.asarray(v, float) for v in (s, L, y))
    r = least_squares(lambda p: norm.cdf((s * p[0] - L) / p[1]) - y, x0,
                      bounds=([-100, 1.0], [150, 60.0]))
    return float(r.x[0]), float(r.x[1])


def p_team_over(x, role, muM, sdM, muS, sdS, rho):
    """P(team score > x), team = (S + r*M)/2 with r=+1 favorite, -1 underdog."""
    r = 1.0 if role == "fav" else -1.0
    mu = (muS + r * muM) / 2
    sd = np.sqrt(sdS ** 2 + sdM ** 2 + 2 * r * rho * sdS * sdM) / 2
    return 1 - norm.cdf((x - mu) / sd)


def refits(anc_M, anc_S, rng, n=N_REFIT):
    """200 refits, each anchor mid ~ U[bid, ask]. anc_*: list of (s, L, bid, ask). -> (n,4) muM,sdM,muS,sdS."""
    out = np.empty((n, 4))
    for name, anc, cols, x0 in (("M", anc_M, (0, 1), (3.0, 13.0)), ("S", anc_S, (2, 3), (45.0, 13.0))):
        s, L, b, a = (np.array(v, float) for v in zip(*anc))
        x0 = (x0[0], x0[1]) if name == "M" else (float(np.median(L)), x0[1])
        for i in range(n):
            out[i, cols] = x0 = fit_normal(s, L, rng.uniform(b, a), x0)
    return out


# ---------- data ----------
def guard(setname):
    if setname != "disc" and not os.path.exists(f"{OUT}/CONFIRM_OPENED_F.md"):
        sys.exit("F confirmation is sealed: commit reports/corr/CONFIRM_OPENED_F.md first")


def T_of(row, buffer_min):
    """Prereg decision time. buffer_min=30 (v1) or 60 (raised by the in-play check)."""
    extra = PREREG_EXTRA_S if row.kickoff_src.startswith("exp") else 0
    return int(row.T_ts) - extra - (buffer_min - 30) * 60


def load(setname):
    guard(setname)
    mk = sqlite3.connect(f"file:{MK}?mode=ro", uri=True)
    ev = pd.read_sql("SELECT * FROM events WHERE lane='F' AND set_name=?", mk, params=(setname,))
    m = pd.read_sql("""SELECT m.ticker, m.series, m.league, m.game_key, m.yes_sub_title, m.result,
                       m.settlement_value, m.raw_json, f.fair FROM markets m JOIN fair_price_settled f USING(ticker)
                       WHERE m.lane='F'""", mk)
    m = m[m.game_key.isin(ev.game_key)]
    p = [parse(r.series, r.ticker, r.yes_sub_title) for r in m.itertuples()]
    m = m.assign(kind=[x[1] if x else None for x in p], team=[x[2] if x else None for x in p],
                 line=[x[3] if x else None for x in p])
    m["settle_ts"] = [json.loads(r or "{}").get("settlement_ts") for r in m.raw_json]
    cdb = sqlite3.connect(f"file:{SET_DB[setname]}?mode=ro", uri=True)
    cand = dict(cdb.execute("SELECT ticker, raw_json FROM candles_raw WHERE status=200"))
    return ev, m.drop(columns="raw_json"), cand


# ---------- outcomes (no prices) ----------
def final_scores(gm):
    """Integer scores (team_a, team_b) consistent with every settled yes/no rung. -> (teams, feasible array)."""
    teams = sorted({t for t in gm.team.dropna()})
    if len(teams) != 2:
        return teams, np.empty((0, 2))
    A, B = np.meshgrid(np.arange(101), np.arange(101), indexing="ij")
    ok = np.ones_like(A, bool)
    sc = {teams[0]: A, teams[1]: B}
    for r in gm[(gm.fair == 0) & gm.result.isin(["yes", "no"])].itertuples():
        if r.kind == "GAME":
            if r.team not in sc:
                continue
            c = sc[r.team] > sc[[t for t in teams if t != r.team][0]]
        elif r.line is None:
            continue
        elif r.kind == "TOTAL":
            c = A + B > r.line
        elif r.team not in sc:
            continue
        elif r.kind == "TEAMTOTAL":
            c = sc[r.team] > r.line
        else:
            c = sc[r.team] - sc[[t for t in teams if t != r.team][0]] > r.line
        ok &= c if r.result == "yes" else ~c
    return teams, np.argwhere(ok)


# ---------- per-event build ----------
def build(gk, ev_row, gm, cand, buffer_min):
    T = T_of(ev_row, buffer_min)
    mk = []
    for r in gm[(gm.fair == 0) & gm.result.isin(["yes", "no"])].itertuples():
        q = quote(candle_rows(cand[r.ticker]), T) if r.ticker in cand and r.kind else None
        if q is not None:
            mk.append({"ticker": r.ticker, "kind": r.kind, "team": r.team, "line": r.line, "q": q,
                       "result": r.result})
    game = {x["team"]: x["q"]["mid"] for x in mk if x["kind"] == "GAME"}
    teams = sorted(set(gm.team.dropna()))
    if not game or len(teams) != 2:
        return None
    t0, pm = max(game.items(), key=lambda kv: kv[1])
    fav = t0 if pm >= 0.5 else [t for t in teams if t != t0][0]
    if len(game) == 2 and abs(game[teams[0]] - game[teams[1]]) < 1e-9:
        return None  # pick'em: no favorite at T
    role = {t: ("fav" if t == fav else "dog") for t in teams}
    anc_M = [((1 if role[x["team"]] == "fav" else -1), (0.0 if x["kind"] == "GAME" else x["line"]),
              x["q"]["bid"], x["q"]["ask"]) for x in mk
             if x["kind"] in ("GAME", "SPREAD") and x["team"] in role and (x["kind"] == "GAME" or x["line"] is not None)]
    anc_S = [(1, x["line"], x["q"]["bid"], x["q"]["ask"]) for x in mk if x["kind"] == "TOTAL" and x["line"] is not None]
    tgt = [dict(x, role=role.get(x["team"])) for x in mk if x["kind"] == "TEAMTOTAL" and x["line"] is not None
           and x["team"] in role]
    return {"game_key": gk, "league": ev_row.league, "date_et": ev_row.date_et, "T": T, "fav": fav,
            "role": role, "anc_M": anc_M, "anc_S": anc_S, "tgt": tgt}


def fit_event(G):
    """Refit params (or None if < 2 anchors on either side)."""
    if len(G["anc_M"]) < 2 or len(G["anc_S"]) < 2:
        return None
    rng = np.random.default_rng(zlib.crc32(G["game_key"].encode()))
    return refits(G["anc_M"], G["anc_S"], rng)


def point_fit(G):
    s, L, b, a = zip(*G["anc_M"])
    mM = fit_normal(s, L, (np.array(b) + a) / 2, (3.0, 13.0))
    s, L, b, a = zip(*G["anc_S"])
    mS = fit_normal(s, L, (np.array(b) + a) / 2, (float(np.median(L)), 13.0))
    return (*mM, *mS)


def evaluate(G, P, rho, tag):
    """Signals and fills for G's targets using refit params P (n,4)."""
    sigs, fills, rows = [], [], []
    for t in G["tgt"]:
        p = p_team_over(t["line"], t["role"], P[:, 0], P[:, 1], P[:, 2], P[:, 3], rho)
        fair, lo, hi = float(p.mean()), float(np.percentile(p, 5)), float(np.percentile(p, 95))
        rows.append({"game_key": G["game_key"], "league": G["league"], "ticker": t["ticker"], "role": t["role"],
                     "line": t["line"], "mid": t["q"]["mid"], "fair": fair, "y": float(t["result"] == "yes")})
        d = decide(t, fair, lo, hi, MULT)
        if d is None:
            continue
        side, entry = d
        base = {"null": tag, "gamePk": G["game_key"], "date_et": G["date_et"], "league": G["league"],
                "ticker": t["ticker"], "role": t["role"], "line": t["line"], "side": side, "fair_yes": fair,
                "lo": lo, "hi": hi, "mid": t["q"]["mid"], "entry_ask": t["q"]["entry_ask"],
                "entry_bid": t["q"]["entry_bid"]}
        sigs.append(base)
        f = fill(t, side, entry, MULT, fair)
        if f:
            fills.append({**base, **f})
    return sigs, fills, rows


def flip(fills, tgt_by_ticker):
    """N3: opposite side of every primary fill at its own executable entry."""
    out = []
    for s in fills:
        t = tgt_by_ticker[s["ticker"]]
        side = "no" if s["side"] == "yes" else "yes"
        entry = 1 - t["q"]["entry_bid"] if side == "no" else t["q"]["entry_ask"]
        if not 0.05 <= entry <= 0.95:
            continue
        f = fill(t, side, entry, MULT, s["fair_yes"])
        if f:
            out.append({**s, "null": "N3", "side": side, **f})
    return out


# ---------- steps ----------
def inplay(setname="disc"):
    """Common-rules check: first 1-min candle with vol > 5x median pregame minute must come after T."""
    ev, m, cand = load(setname)
    res = {}
    for lg, e in ev.groupby("league"):
        e = e.sort_values("game_key")
        e = e[e.game_key.isin(m[(m.kind == "GAME") & m.ticker.isin(cand)].game_key)]
        pick = e.sample(min(20, len(e)), random_state=0) if len(e) else e
        rows = []
        for r in pick.itertuples():
            T = T_of(r, 30)
            gt = m[(m.game_key == r.game_key) & (m.kind == "GAME")].ticker
            c = pd.concat([candle_rows(cand[t]) for t in gt if t in cand]).groupby("ts").vol.sum().sort_index()
            pre = c[c.index <= T]
            med = float(pre.median()) if len(pre) else 0.0
            spike = c[c > 5 * max(med, 1.0)]
            first = int(spike.index[0]) if len(spike) else None
            st = m[(m.game_key == r.game_key) & m.settle_ts.notna()].settle_ts
            first_settle = int(min(pd.to_datetime(st, utc=True)).timestamp()) if len(st) else None
            rows.append({"game_key": r.game_key, "T": T, "median_pre_vol": med, "first_spike_ts": first,
                         "spike_min_after_T": None if first is None else (first - T) / 60,
                         "pass": first is None or first > T,
                         "first_settlement_min_after_T": None if first_settle is None else (first_settle - T) / 60})
        res[lg] = {"n": len(rows), "n_fail": sum(not x["pass"] for x in rows), "events": rows}
    buf = 60 if any(v["n_fail"] for v in res.values()) else 30
    res["buffer_min"] = buf
    # Deviation (disclosed): exp-D kickoffs are wrong for part of NFL; drop events already scoring near T.
    gap = settle_gap(ev, m, buf)
    res["settle_gap_min_by_league"] = {lg: np.histogram(x.gap, bins=[-1e4, -30, 0, 30, 60, 90, 120, 180, 1e5])[0].tolist()
                                       for lg, x in gap.groupby("league")}
    res["excluded_inplay"] = gap[gap.gap < EXCL_MIN][["league", "game_key", "gap"]].to_dict("records")
    json.dump(res, open(f"{OUT}/F_inplay_check.json", "w"), indent=1)
    print(json.dumps({k: (v if not isinstance(v, dict) else {kk: vv for kk, vv in v.items() if kk != "events"})
                      for k, v in res.items()}, indent=1))
    return res


EXCL_MIN = 60  # minutes after T before which no market of the game may have settled


def settle_gap(ev, m, buffer_min):
    """Minutes from T to the game's first settlement_ts (an in-game score fact; no prices)."""
    st = m.dropna(subset=["settle_ts"]).assign(t=lambda x: [pd.Timestamp(v).timestamp() for v in x.settle_ts])
    first = st.groupby("game_key").t.min()
    return pd.DataFrame([{"league": r.league, "game_key": r.game_key,
                          "gap": (first.get(r.game_key, np.inf) - T_of(r, buffer_min)) / 60} for r in ev.itertuples()])


def keep_events(ev, m, buffer_min):
    g = settle_gap(ev, m, buffer_min)
    return ev[ev.game_key.isin(g[g.gap >= EXCL_MIN].game_key)]


def realized(ev, m, cand, buffer_min):
    """(league, game_key, M, S, n_feasible) on events with a GAME quote at T (favorite needs prices of GAME only)."""
    rows = []
    for r in ev.itertuples():
        gm = m[m.game_key == r.game_key]
        teams, feas = final_scores(gm)
        if len(feas) == 0:
            continue
        G = build(r.game_key, r, gm, cand, buffer_min)
        if G is None:
            continue
        a, b = feas.mean(0)
        fa = G["fav"] == teams[0]
        Mv, Sv = (a - b) if fa else (b - a), a + b
        rows.append({"league": r.league, "game_key": r.game_key, "M": float(Mv), "S": float(Sv),
                     "n_feasible": int(len(feas)), "exact": len(feas) == 1})
    return pd.DataFrame(rows)


def rho_step(setname="disc"):
    buf = json.load(open(f"{OUT}/F_inplay_check.json"))["buffer_min"]
    ev, m, cand = load(setname)
    ev = keep_events(ev, m, buf)
    df = realized(ev, m, cand, buf)
    out = {"buffer_min": buf, "source": "integer scores feasible under every settled GAME/SPREAD/TOTAL/TEAMTOTAL "
           "result (Kalshi markets table); feasible-set mean used when not unique"}
    for lg, x in df.groupby("league"):
        n = len(x)
        r = float(np.corrcoef(x.M, x.S)[0, 1])
        z, se = np.arctanh(r), 1 / np.sqrt(max(n - 3, 1))
        ex = x[x.exact]
        out[lg] = {"n": n, "rho": r, "ci95": [float(np.tanh(z - 1.96 * se)), float(np.tanh(z + 1.96 * se))],
                   "n_exact": int(len(ex)), "rho_exact_only": float(np.corrcoef(ex.M, ex.S)[0, 1]) if len(ex) > 3 else None,
                   "median_feasible": float(x.n_feasible.median()), "mean_M": float(x.M.mean()),
                   "sd_M": float(x.M.std()), "mean_S": float(x.S.mean()), "sd_S": float(x.S.std())}
    json.dump(out, open(f"{OUT}/F_rho_discovery.json", "w"), indent=1)
    df.to_csv(f"{OUT}/F_realized_scores_discovery.csv", index=False)
    print(json.dumps(out, indent=1))
    return out


def implied_rho(rows_by_ev):
    """rho minimizing squared error of point-fit TEAMTOTAL probs to market mids."""
    grid = np.linspace(-0.95, 0.95, 191)
    sse = np.zeros_like(grid)
    for pf, tg in rows_by_ev:
        for t in tg:
            sse += (p_team_over(t["line"], t["role"], *pf, grid) - t["q"]["mid"]) ** 2
    return float(grid[sse.argmin()])


def run(setname="disc"):
    from arb.mlb.score import summarize
    rho_j = json.load(open(f"{OUT}/F_rho_discovery.json"))
    buf = rho_j["buffer_min"]
    ev, m, cand = load(setname)
    n_ev_all = ev.groupby("league").size().to_dict()
    ev = keep_events(ev, m, buf)
    Gs = []
    for r in ev.itertuples():
        G = build(r.game_key, r, m[m.game_key == r.game_key], cand, buf)
        if G is None or not G["tgt"]:
            continue
        P = fit_event(G)
        if P is not None:
            Gs.append((G, P))
    by_date = {}
    for G, P in Gs:
        by_date.setdefault((G["date_et"], G["league"]), []).append((G, P))
    rng = np.random.default_rng(7)
    res = {k: {"sigs": [], "fills": []} for k in ("primary", "N1", "N2", "N3")}
    rows, tgt_by = [], {}
    for G, P in Gs:
        rho = rho_j[G["league"]]["rho"]
        s, f, rw = evaluate(G, P, rho, "primary")
        res["primary"]["sigs"] += s; res["primary"]["fills"] += f; rows += rw
        tgt_by.update({t["ticker"]: t for t in G["tgt"]})
        same = [x for x in by_date[(G["date_et"], G["league"])] if x[0] is not G]
        if same:  # N1: other same-date same-league event's role-based fits
            s, f, _ = evaluate(G, same[rng.integers(len(same))][1], rho, "N1")
            res["N1"]["sigs"] += s; res["N1"]["fills"] += f
        s, f, _ = evaluate(G, P, 0.0, "N2")
        res["N2"]["sigs"] += s; res["N2"]["fills"] += f
    res["N3"]["fills"] = flip(res["primary"]["fills"], tgt_by)
    res["N3"]["sigs"] = res["primary"]["sigs"]

    out = {"set": setname, "prereg": "PREREG_CORR3_v1 Lane F", "buffer_min": buf,
           "rho_used": {lg: rho_j[lg]["rho"] for lg in ("NFL", "NCAAF") if lg in rho_j},
           "events_all": n_ev_all, "events_after_inplay_exclusion": ev.groupby("league").size().to_dict(),
           "events_modelled": pd.Series([G["league"] for G, _ in Gs]).value_counts().to_dict(),
           "target_quotes": pd.Series([G["league"] for G, _ in Gs for _ in G["tgt"]]).value_counts().to_dict(),
           "anchor_quotes": pd.Series([G["league"] for G, _ in Gs for _ in G["anc_M"] + G["anc_S"]]).value_counts().to_dict()}
    for k, v in res.items():
        f = pd.DataFrame(v["fills"])
        sg = pd.DataFrame(v["sigs"])
        f.to_csv(f"{OUT}/fills_F_{setname}_{k}.csv", index=False)
        out[k] = {}
        for lg in ("pooled", "NFL", "NCAAF"):
            ff = f if lg == "pooled" or f.empty else f[f.league == lg]
            ns = len(sg) if lg == "pooled" or sg.empty else int((sg.league == lg).sum())
            if not ff.empty:
                ff = ff.assign(family=ff.league)  # E3 concentration is by league in this prereg
            s = summarize(ff, ns, alpha_k=7)
            if "pnl" in s:
                s["E3"] = bool((s["max_family_share_of_pos"] or 1) <= 0.50 and (s["top5pct_games_share_of_pos"] or 1) < 0.50) \
                    if lg == "pooled" else bool((s["top5pct_games_share_of_pos"] or 1) < 0.50)
            out[k][lg] = s
    san = pd.DataFrame(rows)
    out["brier"] = {lg: {"n": int(len(x)), "market_mid": float(((x.mid - x.y) ** 2).mean()),
                         "model_fair": float(((x.fair - x.y) ** 2).mean()),
                         "mean_abs_gap": float((x.fair - x.mid).abs().mean())}
                    for lg, x in [("pooled", san)] + list(san.groupby("league"))} if len(san) else {}
    pf = [(G["league"], point_fit(G), G["tgt"]) for G, _ in Gs]
    out["implied_rho"] = {lg: implied_rho([(p, t) for l, p, t in pf if lg in ("pooled", l)])
                          for lg in ("pooled", "NFL", "NCAAF")}
    out["realized_rho"] = {lg: {k: rho_j[lg][k] for k in ("n", "rho", "ci95")} for lg in ("NFL", "NCAAF") if lg in rho_j}
    json.dump(out, open(f"{OUT}/results_F_{'discovery' if setname == 'disc' else setname}.json", "w"),
              indent=1, default=float)
    print(json.dumps(out, indent=1, default=float)[:8000])
    return out


if __name__ == "__main__":
    {"inplay": inplay, "rho": rho_step, "run": run}[sys.argv[1]](*sys.argv[2:])
