"""Prereg §3–§6, §9: quotes at T, market-anchored fair values, signals, fill proxy, nulls.

`run(setname)` -> fills/signals tables for the primary rule and nulls N1–N3, plus the
untilted-model-vs-market sanity rows (§8). Confirmation reads the SEALED store only
after reports/mlb_corr/CONFIRM_OPENED.md exists.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3
import sys
import unicodedata
from decimal import Decimal

import numpy as np
import pandas as pd
import yaml

from arb.mlb.calibrate import SLOT_PA, naive_rate, poisson_ge
from arb.mlb.data import inputs_asof
from arb.mlb.sim import simulate
from arb.mlb.tilt import fit_tilt, interval
from arb.config import load as load_config
from arb.venues.fees import leg_fee_cost

CFG = load_config()  # Decimal money fields (repo rule 5)
M = CFG["mlb"]
ET = dt.timezone(dt.timedelta(hours=-4))  # all 2026 regular-season dates are EDT
ANCHORS = {"KXMLBGAME", "KXMLBTOTAL", "KXMLBSPREAD", "KXMLBTEAMTOTAL"}
FAMILY = {"KXMLBHR": "HR", "KXMLBHIT": "HIT", "KXMLBKS": "KS"}
K, BB, S1, S2, S3, HR, OUT = range(7)


# ---------- quotes ----------
def _f(d, k):
    if not d:
        return None
    v = d.get(k, d.get(k + "_dollars"))
    return None if v is None else float(v)


def candle_rows(raw: str) -> pd.DataFrame:
    rows = [{"ts": c["end_period_ts"], "bid": _f(c.get("yes_bid"), "close"),
             "ask": _f(c.get("yes_ask"), "close"), "hi": _f(c.get("price"), "high"),
             "lo": _f(c.get("price"), "low"),
             "vol": float(c.get("volume", c.get("volume_fp")) or 0)} for c in json.loads(raw or "[]")]
    return pd.DataFrame(rows, columns=["ts", "bid", "ask", "hi", "lo", "vol"])


def quote(c: pd.DataFrame, T: int):
    """§3/§4/§6: last 2-sided, uncrossed candle ending in [T-stale, T]; latency at T+1."""
    q = c[(c.ts <= T) & (c.ts >= T - 60 * M["stale_min"]) & c.bid.notna() & c.ask.notna()]
    if q.empty:
        return None
    b, a = q.iloc[-1][["bid", "ask"]]
    if a <= b or b <= 0 or a >= 1:
        return None
    n1 = c[(c.ts > T) & (c.ts <= T + 60) & c.bid.notna() & c.ask.notna()]
    b1, a1 = (n1.iloc[0][["bid", "ask"]] if len(n1) else (b, a))
    fw = c[(c.ts > T) & (c.ts <= T + 60 * M["fill_window_min"])]
    return {"bid": b, "ask": a, "mid": (a + b) / 2, "entry_ask": max(a, a1),
            "entry_bid": min(b, b1), "fw": fw}


# ---------- market -> sim indicator ----------
def norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z ]", "", s.replace("-", " "))
    return " ".join(w for w in s.split() if w not in {"jr", "sr", "ii", "iii", "iv"})


def find_slot(inp, side, player):
    """Lineup slot (0-8) by normalized full name, else unique last name; None if unmatched."""
    names = [norm(b["name"]) for b in inp[side]["lineup"]]
    p = norm(player)
    if p in names:
        return names.index(p)
    last = [i for i, n in enumerate(names) if n.split()[-1:] == p.split()[-1:]]
    return last[0] if len(last) == 1 else None


def spec(row, inp, sides):
    """Abstract market spec (kind, role, line, k, slot) so N1 can re-apply anchors across games."""
    ser = row.series
    role = sides.get(row.team if isinstance(row.team, str) else row.team_code)
    if ser == "KXMLBTOTAL":
        return ("total", None, row.line, None, None)
    if role is None:
        return None
    if ser == "KXMLBGAME":
        return ("win", role, None, None, None)
    if ser == "KXMLBSPREAD":
        return ("spread", role, row.line, None, None)
    if ser == "KXMLBTEAMTOTAL":
        return ("team", role, row.line, None, None)
    if ser == "KXMLBKS":
        if norm(inp[role]["sp"]["name"]) != norm(row.player):
            return None
        return ("ks", role, None, int(row.k), None)
    slot = find_slot(inp, role, row.player)
    return None if slot is None else (FAMILY[ser].lower(), role, None, int(row.k), slot)


def indicator(sp, r):
    kind, role, line, k, slot = sp
    s = {"away": 0, "home": 1}.get(role)
    R = r.runs
    if kind == "total":
        return R.sum(1) > line
    if kind == "win":
        return R[:, s] > R[:, 1 - s]
    if kind == "spread":
        return R[:, s] - R[:, 1 - s] > line
    if kind == "team":
        return R[:, s] > line
    if kind == "ks":
        return r.sp_k[:, s] >= k
    return (r.bat_hr if kind == "hr" else r.bat_h)[:, s, slot] >= k


def naive_prob(sp, inp):
    """§8/§9 N2: player rate alone, no game context."""
    kind, role, _, k, slot = sp
    if kind == "ks":
        p = inp[role]["sp"]
        return poisson_ge(p["exp_bf"] * naive_rate(p["alpha"], [K]), k)
    idx = [HR] if kind == "hr" else [S1, S2, S3, HR]
    return poisson_ge(SLOT_PA[slot] * naive_rate(inp[role]["lineup"][slot]["alpha"], idx), k)


# ---------- money ----------
def fee_mult(T: int) -> Decimal:
    t = dt.datetime.fromtimestamp(T, dt.timezone.utc)
    m = 1.0
    for s in M["fee_multiplier_schedule"]:
        if t >= dt.datetime.fromisoformat(s["from"].replace("Z", "+00:00")):
            m = s["m"]
    return Decimal(str(m))


def fee(price, contracts, mult):
    return float(leg_fee_cost(CFG, "kalshi", "sports", Decimal(str(round(price, 4))),
                              Decimal(str(contracts)), mode="execution", multiplier=mult))


def decide(t, fair, lo, hi, mult):
    """§6 rule: returns (side, entry) or None. Fee checked per contract at full size."""
    C = M["contracts"]
    a, b = t["q"]["entry_ask"], t["q"]["entry_bid"]
    lo_b, hi_b = M["entry_band"]
    if lo_b <= a <= hi_b and lo - a - fee(a, C, mult) / C > 0:
        return "yes", a
    if lo_b <= 1 - b <= hi_b and b - hi - fee(1 - b, C, mult) / C > 0:
        return "no", 1 - b
    return None


def fill(t, side, entry, mult, fair):
    fw = t["q"]["fw"]
    hit = fw[fw.hi >= entry] if side == "yes" else fw[fw.lo <= 1 - entry]
    C = min(M["contracts"], float(hit.vol.sum()))
    if C <= 0:
        return None
    won = (t["result"] == side)
    cost = C * entry + fee(entry, C, mult)
    return {"contracts": C, "entry": entry, "cost": cost, "pnl": C * won - cost, "won": won,
            "fair": fair if side == "yes" else 1 - fair}


# ---------- game ----------
def load_game(pk, mk, cdb):
    q = """SELECT p.*, m.result FROM parsed p JOIN markets m USING(ticker)
           WHERE p.fair_price_settled=0 AND p.event_ticker IN (%s)"""
    evs = EVMAP.get(pk, [])
    rows = pd.read_sql(q % ",".join("?" * len(evs)), mk, params=evs) if evs else pd.DataFrame()
    if rows.empty:
        return rows, {}
    cand = dict(cdb.execute("SELECT ticker, raw_json FROM candles_raw WHERE gamePk=? AND status=200",
                            (pk,)).fetchall())
    return rows[rows.ticker.isin(cand)], cand


def game(pk, mk, cdb, games):
    g = games.loc[pk]
    rows, cand = load_game(pk, mk, cdb)
    if rows.empty:
        return None
    inp = inputs_asof(pk)
    sides = {g.away: "away", g.home: "home"}
    start = START[pk]
    T = int((start - dt.timedelta(minutes=M["decision_offset_min"])).timestamp())
    mkts = []
    for row in rows.itertuples():
        sp = spec(row, inp, sides)
        q = quote(candle_rows(cand[row.ticker]), T) if sp else None
        if q is not None:
            mkts.append({"ticker": row.ticker, "series": row.series, "spec": sp, "q": q,
                         "result": row.result})
    return {"gamePk": pk, "date_et": g.date_et, "T": T, "inp": inp, "mkts": mkts}


def evaluate(G, anchors_from=None, mode="primary"):
    """Signals + fills for one game. anchors_from: another game's dict (N1)."""
    anc_src = anchors_from or G
    anchors = [m for m in anc_src["mkts"] if m["series"] in ANCHORS]
    targets = [m for m in G["mkts"] if m["series"] in FAMILY]
    if len(anchors) < 2 or not targets:
        return [], [], []
    r = simulate(G["inp"], n_sims=M["n_sims"], n_chunks=M["n_chunks"], seed=G["gamePk"], **SIM_KW)
    w, _, _ = fit_tilt(r.runs, [(indicator(a["spec"], r), a["q"]["mid"]) for a in anchors], r.chunk)
    mult = fee_mult(G["T"])
    sigs, fills, sanity = [], [], []
    for t in targets:
        ind = indicator(t["spec"], r)
        if mode == "naive":
            fair = lo = hi = naive_prob(t["spec"], G["inp"])
        else:
            fair, lo, hi = interval(ind, w, r.chunk)
        sanity.append({"family": FAMILY[t["series"]], "mid": t["q"]["mid"],
                       "p_untilted": float(ind.mean()), "p_anchored": fair,
                       "y": float(t["result"] == "yes")})
        d = decide(t, fair, lo, hi, mult)
        if d is None:
            continue
        side, entry = d
        base = {"gamePk": G["gamePk"], "date_et": G["date_et"], "ticker": t["ticker"],
                "family": FAMILY[t["series"]], "side": side, "fair": fair, "lo": lo, "hi": hi,
                "mid": t["q"]["mid"]}
        sigs.append(base)
        f = fill(t, side, entry, mult, fair)
        if f:
            fills.append({**base, **f})
    return sigs, fills, sanity


def flip(fills_rows, G_by_pk):
    """N3: opposite side of every primary signal at its own entry (1 - entry + spread ignored)."""
    out = []
    for s in fills_rows:
        t = next(m for m in G_by_pk[s["gamePk"]]["mkts"] if m["ticker"] == s["ticker"])
        side = "no" if s["side"] == "yes" else "yes"
        entry = 1 - t["q"]["entry_bid"] if side == "no" else t["q"]["entry_ask"]
        f = fill(t, side, entry, fee_mult(G_by_pk[s["gamePk"]]["T"]), s["fair"])
        if f:
            out.append({**s, "side": side, **f})
    return out


EVMAP: dict = {}
SIM_KW: dict = {}  # v1: none; v2: config mlb.sim_v2
START: dict = {}


def run(setname: str):
    global EVMAP, START
    if setname == "confirmation" and not os.path.exists("reports/mlb_corr/CONFIRM_OPENED.md"):
        sys.exit("confirmation is sealed: commit CONFIRM_OPENED.md first (prereg §8)")
    em = pd.read_parquet("data/mlb/event_map.parquet")
    em = em[em.match_quality.isin(["unique", "unique_timediff", "dh_nearest"])]
    EVMAP = em.groupby("gamePk").event_ticker.apply(list).to_dict()
    START = {int(pk): dt.datetime.fromisoformat(s.replace(" ", "T")).replace(tzinfo=ET)
             for pk, s in em.groupby("gamePk").start_et_ticker.first().items()}
    games = pd.read_parquet("data/mlb/games.parquet").set_index("gamePk")
    ok = games[(games.state == "Final") & games.resumed_from.isna() & (games.innings >= 9)]
    pks = [pk for pk in json.load(open("reports/mlb_corr/game_sets.json"))[setname] if pk in ok.index]
    mk = sqlite3.connect("data/mlb/kalshi_markets.sqlite")
    cdb = sqlite3.connect("data/mlb/kalshi_confirm.sqlite" if setname == "confirmation"
                          else "data/mlb/kalshi_candles.sqlite")
    Gs = [G for G in (game(pk, mk, cdb, games) for pk in pks) if G and G["mkts"]]
    by_pk = {G["gamePk"]: G for G in Gs}
    rng = np.random.default_rng(M["discovery"]["seed"])
    res = {k: {"sigs": [], "fills": []} for k in ("primary", "N1", "N2", "N3")}
    sanity = []
    for G in Gs:
        s, f, san = evaluate(G)
        res["primary"]["sigs"] += s
        res["primary"]["fills"] += f
        sanity += san
        same = [H for H in Gs if H["date_et"] == G["date_et"] and H is not G]
        if same:
            s, f, _ = evaluate(G, anchors_from=same[rng.integers(len(same))])
            res["N1"]["sigs"] += s
            res["N1"]["fills"] += f
        s, f, _ = evaluate(G, mode="naive")
        res["N2"]["sigs"] += s
        res["N2"]["fills"] += f
    res["N3"]["fills"] = flip(res["primary"]["fills"], by_pk)
    res["N3"]["sigs"] = res["primary"]["sigs"]
    return Gs, res, pd.DataFrame(sanity)


def main(setname: str, version: str = "v1"):
    from arb.mlb.score import summarize
    if version == "v2":  # config numbers load as Decimal; numpy wants floats
        SIM_KW.update({k: [float(x) for x in v] if isinstance(v, list) else float(v)
                       for k, v in M["sim_v2"].items()})
    Gs, res, san = run(setname)
    out = {"set": setname, "version": version, "n_games_with_quotes": len(Gs),
           "n_target_quotes": int(sum(m["series"] in FAMILY for G in Gs for m in G["mkts"]))}
    for k, v in res.items():
        f = pd.DataFrame(v["fills"])
        out[k] = summarize(f, len(v["sigs"]))
        if not f.empty:
            out[k]["by_family"] = {fam: summarize(x, sum(s["family"] == fam for s in v["sigs"]))
                                   for fam, x in f.groupby("family")}
            ext = f[(f.mid <= 0.10) | (f.mid >= 0.90)]
            out[k]["extreme_band"] = summarize(ext, len(ext)) if len(ext) else None
        f.to_csv(f"reports/mlb_corr/fills_{setname}_{version}_{k}.csv", index=False)
    if not san.empty:
        out["sanity"] = {fam: {"n": int(len(x)),
                               "brier_market": float(((x.mid - x.y) ** 2).mean()),
                               "brier_untilted": float(((x.p_untilted - x.y) ** 2).mean()),
                               "brier_anchored": float(((x.p_anchored - x.y) ** 2).mean()),
                               "mean_abs_gap_untilted": float((x.p_untilted - x.mid).abs().mean()),
                               "mean_abs_gap_anchored": float((x.p_anchored - x.mid).abs().mean())}
                         for fam, x in san.groupby("family")}
        for fam, s in out["sanity"].items():
            s["model_suspect"] = s["brier_untilted"] - s["brier_market"] > 0.005
    json.dump(out, open(f"reports/mlb_corr/results_{setname}_{version}.json", "w"), indent=1, default=float)
    print(json.dumps(out, indent=1, default=float)[:6000])


if __name__ == "__main__":
    main(*sys.argv[1:])
