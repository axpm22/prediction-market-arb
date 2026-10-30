"""Lane S (PREREG_CORR3_v1): soccer scoreline coherence, paper only.

Anchors GAME (home/tie/away) + TOTAL ladder -> per-match (lam_h, lam_a) under an independent
Poisson (Dixon-Coles with rho=0, v1) scoreline matrix 0..10 -> price BTTS / SPREAD / TEAMTOTAL /
SCORE. Signals, fills and fees reuse arb/mlb/lane.py (quote/fill/fee).

    python -m arb.corr.soccer discovery

Confirmation candles (data/corr/S_confirm.sqlite) are sealed until
reports/corr/CONFIRM_OPENED_S.md exists.
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

from arb.mlb.lane import candle_rows, fee, fill, quote

MK = "data/corr/markets.sqlite"
OUT = "reports/corr"
N = 11  # goals 0..10 (prereg truncation)
HG, AG = np.meshgrid(np.arange(N), np.arange(N), indexing="ij")
MULT = Decimal("1")  # all lane-S series quadratic x1 (INVENTORY, fee_changes empty)
C = 10
BAND = (0.05, 0.95)
NREF = 200
ANCHOR_KINDS = {"GAME", "TOTAL"}
FAMILIES = ("BTTS", "SPREAD", "TEAMTOTAL", "SCORE")
RULE_90 = "after 90 minutes plus stoppage time (does not include extra time or penalties)"


# ---------- model ----------
def score_matrix(lh, la, rho=0.0):
    """(B,N,N) P(home=i, away=j); Dixon-Coles tau on the four low cells, renormalised after truncation."""
    lh, la = np.atleast_1d(np.asarray(lh, float)), np.atleast_1d(np.asarray(la, float))
    k = np.arange(N)
    lf = np.cumprod(np.r_[1.0, np.arange(1, N)])
    ph = np.exp(-lh[:, None]) * lh[:, None] ** k / lf
    pa = np.exp(-la[:, None]) * la[:, None] ** k / lf
    M = ph[:, :, None] * pa[:, None, :]
    if rho:
        M[:, 0, 0] *= 1 - lh * la * rho
        M[:, 0, 1] *= 1 + lh * rho
        M[:, 1, 0] *= 1 + la * rho
        M[:, 1, 1] *= 1 - rho
    return M / M.sum((1, 2), keepdims=True)


def indicator(sp):
    """Spec -> (N,N) bool over (home goals, away goals), matching Kalshi rule text."""
    kind = sp[0]
    if kind == "GAME":
        return {"home": HG > AG, "tie": HG == AG, "away": AG > HG}[sp[1]]
    if kind == "TOTAL":
        return HG + AG > sp[1]
    if kind == "SPREAD":
        return (HG - AG if sp[1] == "home" else AG - HG) > sp[2]
    if kind == "TEAMTOTAL":
        return (HG if sp[1] == "home" else AG) > sp[2]
    if kind == "BTTS":
        return (HG > 0) & (AG > 0)
    if kind == "SCORE":
        return (HG == sp[1]) & (AG == sp[2])
    raise ValueError(sp)


def probs(lh, la, inds, rho=0.0):
    return np.einsum("bij,kij->bk", score_matrix(lh, la, rho), np.asarray(inds, float))


def fit_lambdas(inds, y, iters=40):
    """Least squares of anchor probabilities to targets y (B,K) -> (B,2) lambdas. Batched damped
    Gauss-Newton in log-lambda (2 params; ponytail: no scipy needed)."""
    y = np.atleast_2d(y)
    th = np.full((len(y), 2), np.log(1.3))
    h = 1e-5
    for _ in range(iters):
        p = probs(np.exp(th[:, 0]), np.exp(th[:, 1]), inds)
        J = np.stack([(probs(np.exp(th[:, 0] + h * (d == 0)), np.exp(th[:, 1] + h * (d == 1)), inds) - p) / h
                      for d in range(2)], -1)  # (B,K,2)
        A = np.einsum("bki,bkj->bij", J, J) + 1e-6 * np.eye(2)
        g = np.einsum("bki,bk->bi", J, p - y)
        step = np.linalg.solve(A, g[..., None])[..., 0]
        th = np.clip(th - np.clip(step, -1, 1), np.log(0.05), np.log(6.0))
        if np.abs(step).max() < 1e-7:
            break
    return np.exp(th)


# ---------- parsing ----------
def kind_of(series):
    s = re.sub(r"^KX(EPL|LALIGA|SERIEA|BUNDESLIGA|UCL|MLS)", "", series)
    return s if s in {"GAME", "TOTAL", "SPREAD", "BTTS", "SCORE", "TEAMTOTAL"} else None


def _role(who, home):
    return "home" if who == home else "away"


def parse(series, rules):
    """Rule text -> spec tuple, or None. Roles come from the 'X vs Y' order in the same sentence."""
    k, r = kind_of(series), rules or ""
    if k is None or RULE_90 not in r:
        return None
    if k == "GAME":
        if r.startswith("If Tie is the result"):
            return ("GAME", "tie")
        m = re.match(r"If (.+?) wins the (.+?) vs ", r)
        return m and ("GAME", _role(m[1], m[2]))
    if k == "TOTAL":
        m = re.match(r"If over ([\d.]+) goals are scored in the ", r)
        return m and ("TOTAL", float(m[1]))
    if k == "SPREAD":
        m = re.match(r"If (.+?) wins by more than ([\d.]+) goals in the (.+?) vs ", r)
        return m and ("SPREAD", _role(m[1], m[3]), float(m[2]))
    if k == "TEAMTOTAL":
        m = re.match(r"If (.+?) records above ([\d.]+) goals in the (.+?) vs ", r)
        return m and ("TEAMTOTAL", _role(m[1], m[3]), float(m[2]))
    if k == "BTTS":
        return ("BTTS",) if re.match(r"If .+ both score a goal in the ", r) else None
    m = re.match(r"If the result is a (\d+)-(\d+) draw", r)
    if m:
        return ("SCORE", int(m[1]), int(m[2]))
    m = re.match(r"If (.+?) wins (\d+)-(\d+) in the (.+?) vs ", r)
    if not m:
        return None
    w, l = int(m[2]), int(m[3])
    return ("SCORE", w, l) if m[1] == m[4] else ("SCORE", l, w)


# ---------- data ----------
def candles_db(setname):
    if setname != "discovery":
        if not os.path.exists(f"{OUT}/CONFIRM_OPENED_S.md"):
            sys.exit("confirmation is sealed: commit reports/corr/CONFIRM_OPENED_S.md first (prereg)")
        return sqlite3.connect("data/corr/S_confirm.sqlite")
    return sqlite3.connect("data/corr/S_disc.sqlite")


def load(setname):
    mk = sqlite3.connect(MK)
    sn = {"discovery": "disc", "confirmation": "confirm"}[setname]
    ev = pd.read_sql("SELECT * FROM events WHERE lane='S' AND set_name=?", mk, params=(sn,))
    m = pd.read_sql("SELECT ticker, series, league, game_key, rules_primary, result FROM markets WHERE lane='S'", mk)
    m["spec"] = [parse(s, r) for s, r in zip(m.series, m.rules_primary)]
    m["kind"] = m.series.map(kind_of)
    return ev, m


def series_audit(m):
    """Prereg: every series settles on 90 min + stoppage; drop any series with a deviating market."""
    ok = m.groupby("series").rules_primary.apply(lambda s: bool(s.fillna("").str.contains(RULE_90, regex=False).all()))
    unparsed = m[m.spec.isna()].groupby("series").size().to_dict()
    quote1 = m.groupby("kind").rules_primary.first().to_dict()
    return sorted(ok[~ok].index), unparsed, quote1


def outcome_scores(m):
    """Final 90-min score per (league, game_key): the unique cell consistent with every settled
    yes/no market of the game (SCORE + GAME + TOTAL + SPREAD + TEAMTOTAL + BTTS)."""
    out = {}
    for (lg, gk), g in m[m.result.isin(["yes", "no"]) & m.spec.notna()].groupby(["league", "game_key"]):
        ok = np.ones((N, N), bool)
        for sp, res in zip(g.spec, g.result):
            ok &= indicator(sp) == (res == "yes")
        cells = np.argwhere(ok)
        if len(cells) == 1:
            out[(lg, gk)] = tuple(int(x) for x in cells[0])
    return out


# ---------- in-play check ----------
def inplay_check(ev, m, cdb, T_of, n=20, seed=20261029):
    """Prereg: on 20 discovery events, T must precede the first 1-min candle (GAME markets summed)
    with volume > 5x the median pregame minute (minutes <= T in the pulled window, zeros included)."""
    rng = np.random.default_rng(seed)
    pool = ev.sample(frac=1, random_state=rng.integers(1 << 31)).reset_index(drop=True)
    rows = []
    for e in pool.itertuples():
        T = T_of(e)
        tick = m[(m.game_key == e.game_key) & (m.league == e.league) & (m.kind == "GAME")].ticker.tolist()
        raws = [r[0] for t in tick for r in cdb.execute(
            "SELECT raw_json FROM candles_raw WHERE ticker=? AND status=200", (t,))]
        if not raws:
            continue
        v = pd.concat([candle_rows(r) for r in raws]).groupby("ts").vol.sum()
        grid = np.arange(e.T_ts - 75 * 60 + 60, e.T_ts + 15 * 60 + 1, 60)
        v = v.reindex(grid, fill_value=0.0)
        med = float(v[v.index <= T].median())
        spike = v[v > 5 * med]
        first = int(spike.index[0]) if len(spike) else None
        rows.append({"game_key": e.game_key, "league": e.league, "T": T, "median_pregame": med,
                     "first_spike_min_rel_T": None if first is None else (first - T) / 60,
                     "pass": first is None or first > T})
        if len(rows) == n:
            break
    return rows


# ---------- per match ----------
def match(e, mm, cdb, T):
    """Quotes at T for every parsed, yes/no-settled market of the match."""
    mm = mm[mm.spec.notna() & mm.result.isin(["yes", "no"])]
    raw = dict(cdb.execute(f"SELECT ticker, raw_json FROM candles_raw WHERE status=200 AND ticker IN "
                           f"({','.join('?' * len(mm))})", mm.ticker.tolist()).fetchall()) if len(mm) else {}
    mk = []
    for r in mm.itertuples():
        q = quote(candle_rows(raw[r.ticker]), T) if r.ticker in raw else None
        if q is not None:
            mk.append({"ticker": r.ticker, "kind": r.kind, "spec": r.spec, "q": q, "result": r.result})
    return {"game_key": e.game_key, "league": e.league, "date_et": e.date_et, "T": T, "mkts": mk}


def anchors_ok(anc):
    kinds = {a["kind"] for a in anc}
    return ANCHOR_KINDS <= kinds


def refit(anc, targets, seed):
    """200 refits with anchor mids ~ U[bid, ask] -> (fair, lo, hi) per target, plus point lambdas."""
    rng = np.random.default_rng(seed)
    bid = np.array([a["q"]["bid"] for a in anc])
    ask = np.array([a["q"]["ask"] for a in anc])
    I = [indicator(a["spec"]) for a in anc]
    lam = fit_lambdas(I, rng.uniform(bid, ask, (NREF, len(anc))))
    P = probs(lam[:, 0], lam[:, 1], [indicator(t["spec"]) for t in targets])
    return P.mean(0), np.percentile(P, 5, 0), np.percentile(P, 95, 0), lam


def decide(q, fair, lo, hi):
    a, b = q["entry_ask"], q["entry_bid"]
    if BAND[0] <= a <= BAND[1] and lo - a - fee(a, C, MULT) / C > 0:
        return "yes", a
    if BAND[0] <= 1 - b <= BAND[1] and b - hi - fee(1 - b, C, MULT) / C > 0:
        return "no", 1 - b
    return None


def evaluate(G, anchors_from=None, n2=None):
    """Signals/fills for one match. anchors_from: another match (N1, re-applied by role through the
    role-based specs). n2: {(league, spec): (sum_mid, n)} league-average target price (N2)."""
    anc = [a for a in (anchors_from or G)["mkts"] if a["kind"] in ANCHOR_KINDS]
    tg = [t for t in G["mkts"] if t["kind"] in FAMILIES]
    if not tg or not anchors_ok(anc):
        return [], [], []
    if n2 is not None:
        fair, rows = [], []
        for t in tg:
            s, k = n2.get((G["league"], t["spec"]), (0.0, 0))
            k_ex = k - 1  # leave this match out
            if k_ex >= 1:
                rows.append(t)
                fair.append((s - t["q"]["mid"]) / k_ex)
        tg, fair = rows, np.array(fair)
        lo = hi = fair
    else:
        fair, lo, hi, _ = refit(anc, tg, zlib.crc32(G["game_key"].encode()))
    sigs, fills, san = [], [], []
    for t, f, l, h in zip(tg, fair, lo, hi):
        san.append({"family": t["kind"], "mid": t["q"]["mid"], "fair": float(f), "y": float(t["result"] == "yes")})
        d = decide(t["q"], f, l, h)
        if d is None:
            continue
        base = {"game_key": G["game_key"], "gamePk": G["game_key"], "league": G["league"], "date_et": G["date_et"],
                "ticker": t["ticker"], "family": t["kind"], "side": d[0], "fair_yes": float(f), "lo": float(l),
                "hi": float(h), "mid": t["q"]["mid"]}
        sigs.append(base)
        x = fill(t, d[0], d[1], MULT, float(f))
        if x:
            fills.append({**base, **x})
    return sigs, fills, san


def flip(fills, by_key):
    out = []
    for s in fills:
        t = next(m for m in by_key[s["game_key"]]["mkts"] if m["ticker"] == s["ticker"])
        side = "no" if s["side"] == "yes" else "yes"
        entry = 1 - t["q"]["entry_bid"] if side == "no" else t["q"]["entry_ask"]
        x = fill(t, side, entry, MULT, s["fair_yes"])
        if x:
            out.append({**s, "side": side, **x})
    return out


# ---------- rho (outcomes only; v2 candidate, NOT applied) ----------
def poisson_glm(rows, ridge=1e-6, iters=50):
    """Goals ~ Poisson(exp(league + home_adv*home + att_team - def_opp)) by IRLS. rows: (league,
    team, opp, is_home, goals). Returns fitted means."""
    teams = sorted({(r[0], r[1]) for r in rows} | {(r[0], r[2]) for r in rows})
    lgs = sorted({r[0] for r in rows})
    ti, li = {t: i for i, t in enumerate(teams)}, {l: i for i, l in enumerate(lgs)}
    nt, nl = len(teams), len(lgs)
    X = np.zeros((len(rows), nl + 1 + 2 * nt))
    y = np.array([r[4] for r in rows], float)
    for i, (lg, t, o, hm, _) in enumerate(rows):
        X[i, li[lg]] = 1
        X[i, nl] = hm
        X[i, nl + 1 + ti[(lg, t)]] = 1
        X[i, nl + 1 + nt + ti[(lg, o)]] = -1
    b = np.zeros(X.shape[1])
    b[:nl] = np.log(max(y.mean(), 0.1))
    pen = ridge * np.r_[np.zeros(nl + 1), np.ones(2 * nt)]
    for _ in range(iters):
        mu = np.exp(X @ b)
        z = X @ b + (y - mu) / mu
        A = X.T @ (mu[:, None] * X) + np.diag(pen + 1e-9)
        nb = np.linalg.solve(A, X.T @ (mu * z))
        if np.abs(nb - b).max() < 1e-8:
            b = nb
            break
        b = nb
    return np.exp(X @ b)


def rho_mle(lh, la, hg, ag):
    """Profile MLE of DC rho with lambdas fixed; SE from the numeric second derivative."""
    lh, la, hg, ag = map(np.asarray, (lh, la, hg, ag))

    def ll(r):
        t = np.ones(len(hg))
        t = np.where((hg == 0) & (ag == 0), 1 - lh * la * r, t)
        t = np.where((hg == 0) & (ag == 1), 1 + lh * r, t)
        t = np.where((hg == 1) & (ag == 0), 1 + la * r, t)
        t = np.where((hg == 1) & (ag == 1), 1 - r, t)
        return np.log(np.clip(t, 1e-12, None)).sum()
    # feasible range keeps every tau > 0
    hi = min(1.0, (1 / (lh * la)).min())
    lo = max(-1.0, (-1 / lh).max(), (-1 / la).max())
    g = np.linspace(lo + 1e-4, hi - 1e-4, 4001)
    r = g[np.argmax([ll(x) for x in g])]
    e = 1e-3
    d2 = (ll(r + e) - 2 * ll(r) + ll(r - e)) / e ** 2
    return {"rho": float(r), "se": float(1 / np.sqrt(-d2)) if d2 < 0 else None, "n": int(len(hg))}


# ---------- driver ----------
def T_prereg(e, extra_min=0):
    """Puller T_ts = exp - 2h - 30m. Prereg: kickoff = exp - 2h - 30m buffer, T = kickoff - 30m,
    so T = T_ts - 30m (+ another 30m if the in-play check fails)."""
    return int(e.T_ts) - 60 * (30 + extra_min)


def summ(f, n_sig):
    from arb.mlb.score import summarize
    s = summarize(f, n_sig, alpha_k=7) if n_sig else {"n_signals": 0, "n_fills": 0}
    if len(f):  # prereg E3 is per league (score.py's E3 is the MLB family rule)
        lg = f.groupby("league").pnl.sum().clip(lower=0)
        s["league_pnl"] = f.groupby("league").pnl.sum().round(4).to_dict()
        s["max_league_share_of_pos"] = float(lg.max() / lg.sum()) if lg.sum() > 0 else None
        s["E3"] = bool((s["max_league_share_of_pos"] or 1) <= 0.5 and (s.get("top5pct_games_share_of_pos") or 1) < 0.5)
        s.pop("max_family_share_of_pos", None)
    return s


def team_codes(m):
    """(league, game_key) -> (home_code, away_code) from GAME tickers + rule roles."""
    out = {}
    g = m[(m.kind == "GAME") & m.spec.notna()]
    for r in g.itertuples():
        if r.spec[1] == "tie":
            continue
        code = r.ticker.rsplit("-", 1)[1]
        out.setdefault((r.league, r.game_key), {})[r.spec[1]] = code
    return {k: (v["home"], v["away"]) for k, v in out.items() if len(v) == 2}


def run(setname="discovery"):
    ev, m = load(setname)
    dropped, unparsed, quote1 = series_audit(m)
    m = m[~m.series.isin(dropped)]
    cdb = candles_db(setname)
    res = {"set": setname, "series_dropped": dropped, "unparsed_by_series": unparsed, "rules_quote": quote1}

    chk = inplay_check(ev, m, cdb, T_prereg)
    extra = 0 if chk and all(r["pass"] for r in chk) else 30
    res["inplay_check"] = {"n": len(chk), "n_pass": sum(r["pass"] for r in chk), "buffer_extra_min": extra,
                           "rows": chk}
    if extra:  # informational: does the check pass at the buffered T?
        chk2 = inplay_check(ev, m, cdb, lambda e: T_prereg(e, 30))
        res["inplay_check"]["n_pass_after_buffer"] = sum(r["pass"] for r in chk2)

    groups = {k: g for k, g in m.groupby(["league", "game_key"])}
    Gs = []
    for e in ev.itertuples():
        g = groups.get((e.league, e.game_key))
        if g is not None:
            G = match(e, g, cdb, T_prereg(e, extra))
            if G["mkts"]:
                Gs.append(G)
    by_key = {G["game_key"]: G for G in Gs}
    assert len(by_key) == len(Gs), "game_key collides across leagues"
    n2 = {}
    for G in Gs:
        for t in G["mkts"]:
            if t["kind"] in FAMILIES:
                s, k = n2.get((G["league"], t["spec"]), (0.0, 0))
                n2[(G["league"], t["spec"])] = (s + t["q"]["mid"], k + 1)

    rng = np.random.default_rng(20261029)
    out = {k: {"sigs": [], "fills": []} for k in ("primary", "N1", "N2", "N3")}
    san = []
    for G in Gs:
        s, f, sa = evaluate(G)
        out["primary"]["sigs"] += s
        out["primary"]["fills"] += f
        san += sa
        same = [H for H in Gs if H["date_et"] == G["date_et"] and H is not G
                and anchors_ok([a for a in H["mkts"] if a["kind"] in ANCHOR_KINDS])]
        if same:
            s, f, _ = evaluate(G, anchors_from=same[rng.integers(len(same))])
            out["N1"]["sigs"] += s
            out["N1"]["fills"] += f
        s, f, _ = evaluate(G, n2=n2)
        out["N2"]["sigs"] += s
        out["N2"]["fills"] += f
    out["N3"]["fills"] = flip(out["primary"]["fills"], by_key)
    out["N3"]["sigs"] = out["primary"]["sigs"]

    with_anchor = [G for G in Gs if anchors_ok([a for a in G["mkts"] if a["kind"] in ANCHOR_KINDS])]
    res.update({"n_events": int(len(ev)), "n_events_with_quotes": len(Gs), "n_events_with_anchors": len(with_anchor),
                "n_target_quotes": int(sum(t["kind"] in FAMILIES for G in with_anchor for t in G["mkts"])),
                "target_quotes_by_family": pd.Series([t["kind"] for G in with_anchor for t in G["mkts"]
                                                      if t["kind"] in FAMILIES]).value_counts().to_dict(),
                "events_by_league": pd.Series([G["league"] for G in with_anchor]).value_counts().to_dict()})
    for k, v in out.items():
        f = pd.DataFrame(v["fills"])
        res[k] = summ(f, len(v["sigs"]))
        res[k]["by_family"] = {fam: summ(f[f.family == fam] if len(f) else f,
                                         sum(s["family"] == fam for s in v["sigs"])) for fam in FAMILIES}
        f.to_csv(f"{OUT}/fills_S_{setname}_{k}.csv", index=False)
    sd = pd.DataFrame(san)
    if len(sd):
        res["sanity"] = {fam: {"n": int(len(x)), "base_rate": float(x.y.mean()),
                               "brier_market": float(((x.mid - x.y) ** 2).mean()),
                               "brier_model": float(((x.fair - x.y) ** 2).mean()),
                               "mean_model_minus_mid": float((x.fair - x.mid).mean()),
                               "mean_abs_gap": float((x.fair - x.mid).abs().mean())}
                         for fam, x in sd.groupby("family")}

    # rho MLE on discovery OUTCOMES (v2 candidate, not applied)
    scores = outcome_scores(m)
    codes = team_codes(m)
    keys = [(e.league, e.game_key) for e in ev.itertuples() if (e.league, e.game_key) in scores
            and (e.league, e.game_key) in codes]
    rows = []
    for lg, gk in keys:
        (h, a), (hc, ac) = scores[(lg, gk)], codes[(lg, gk)]
        rows += [(lg, hc, ac, 1, h), (lg, ac, hc, 0, a)]
    mu = poisson_glm(rows) if rows else np.array([])
    hg = np.array([scores[k][0] for k in keys])
    ag = np.array([scores[k][1] for k in keys])
    res["rho"] = {"score_source": "unique (h,a) cell in 0..10^2 consistent with all yes/no-settled SCORE/GAME/"
                                  "TOTAL/SPREAD/TEAMTOTAL/BTTS results of the match",
                  "n_events": int(len(ev)), "n_scores_resolved": len(keys),
                  "mean_home_goals": float(hg.mean()) if len(hg) else None,
                  "mean_away_goals": float(ag.mean()) if len(ag) else None}
    if len(keys) > 10:
        res["rho"]["outcomes_only_team_glm"] = rho_mle(mu[0::2], mu[1::2], hg, ag)
        lgm = {lg: (hg[[k[0] == lg for k in keys]].mean(), ag[[k[0] == lg for k in keys]].mean())
               for lg in {k[0] for k in keys}}
        res["rho"]["outcomes_only_league_means"] = rho_mle([lgm[k[0]][0] for k in keys],
                                                           [lgm[k[0]][1] for k in keys], hg, ag)
        # sensitivity only: lambdas from anchor mids at T (anchor prices, not target prices)
        al = []
        for k, h, a in zip(keys, hg, ag):
            G = by_key.get(k[1])
            anc = [x for x in G["mkts"] if x["kind"] in ANCHOR_KINDS] if G else []
            if anchors_ok(anc):
                lam = fit_lambdas([indicator(x["spec"]) for x in anc], [[x["q"]["mid"] for x in anc]])[0]
                al.append((lam[0], lam[1], h, a))
        if len(al) > 10:
            al = np.array(al)
            res["rho"]["sensitivity_anchor_lambdas"] = rho_mle(al[:, 0], al[:, 1], al[:, 2].astype(int),
                                                               al[:, 3].astype(int))
    os.makedirs(OUT, exist_ok=True)
    json.dump(res, open(f"{OUT}/results_S_{setname}.json", "w"), indent=1, default=str)
    return res


if __name__ == "__main__":
    r = run(*(sys.argv[1:] or ["discovery"]))
    print(json.dumps({k: v for k, v in r.items() if k not in ("rules_quote",)}, indent=1, default=str)[:8000])
