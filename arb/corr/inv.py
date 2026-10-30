"""Phase-0 inventory harvester (read-only, resumable via arb.corr.kc cache)."""
import json, statistics as stt, re, sys
from arb.corr.kc import get
S = {s["ticker"]: s for s in get("/series", {"category": "Sports", "include_volume": "true"})[1]["series"]}
FAM = {
 "NFL": "GAME SPREAD TOTAL TEAMTOTAL 1HSPREAD 1HTOTAL 1H TD ANYTD FIRSTTD RECYDS RSHYDS PASSYDS REC PASSTDS 2TD".split(),
 "NCAAF": "GAME SPREAD TOTAL TEAMTOTAL 1HSPREAD 1HTOTAL 1H".split(),
 "NBA": "GAME SPREAD TOTAL TEAMTOTAL 1HSPREAD 1HTOTAL 1HWINNER PTS REB AST 3PT 2D".split(),
 "NCAAMB": "GAME SPREAD TOTAL 1HWINNER 1HSPREAD 1HTOTAL".split(),
 "NHL": "GAME SPREAD TOTAL TEAMTOTAL GOAL PTS AST 1PTOTAL FIRSTGOAL".split(),
 "EPL": "GAME TOTAL SPREAD BTTS SCORE 1H 1HTOTAL TEAMTOTAL GOAL".split(),
 "UCL": "GAME TOTAL SPREAD BTTS SCORE 1H 1HTOTAL TEAMTOTAL GOAL".split(),
 "LALIGA": "GAME TOTAL SPREAD BTTS SCORE 1H 1HTOTAL TEAMTOTAL GOAL".split(),
 "SERIEA": "GAME TOTAL SPREAD BTTS SCORE".split(),
 "BUNDESLIGA": "GAME TOTAL SPREAD BTTS SCORE".split(),
 "MLS": "GAME TOTAL SPREAD BTTS SCORE".split(),
 "ATP": "MATCH SETWINNER GTOTAL GSPREAD EXACTMATCH TOTALSETS".split(),
 "WTA": "MATCH SETWINNER GTOTAL GSPREAD EXACTMATCH".split(),
 "UFC": "FIGHT MOV VICROUND DISTANCE MOF ROUNDS".split(),
 "PGA": "TOUR TOP5 TOP10 TOP20 MAKECUT H2H R1LEAD".split(),
}
fam_of = {}
for f, ks in FAM.items():
    for k in ks:
        t = "KX" + f + k
        if t in S and float(S[t]["volume_fp"]) >= 1e6: fam_of[t] = f
def f(x):
    try: return float(x)
    except: return None
def snap(evs):
    sp, vol, oi, two, n = [], [], [], 0, 0
    for e in evs:
        for m in e.get("markets", []):
            n += 1
            b, a = f(m.get("yes_bid_dollars")), f(m.get("yes_ask_dollars"))
            vol.append(f(m.get("volume_fp")) or 0); oi.append(f(m.get("open_interest_fp")) or 0)
            if b and a and b > 0 and a < 1 and 0.03 <= (a + b) / 2 <= 0.97:   # live two-sided, not near-resolved
                two += 1; sp.append(a - b)
    med = lambda x: round(stt.median(x), 4) if x else None
    return dict(n_markets=n, n_two_sided_midrange=two, med_spread=med(sp), med_volume=med(vol), med_oi=med(oi))
if __name__ == "__main__":
    out = {}
    for t, fam in fam_of.items():
        s = S[t]
        st, j = get("/events", {"series_ticker": t, "status": "open", "with_nested_markets": "true", "limit": 200})
        ev = j["events"]
        r = dict(family=fam, title=s["title"], freq=s["frequency"], fee_type=s["fee_type"], fee_mult=s["fee_multiplier"],
                 total_volume=float(s["volume_fp"]), n_open_events=len(ev), open_cursor=bool(j.get("cursor")),
                 open_event_tickers=[e["event_ticker"] for e in ev][:60], snap=snap(ev))
        if ev:
            m = ev[0]["markets"][0] if ev[0]["markets"] else {}
            r["sample_market"] = dict(ticker=m.get("ticker"), yes_sub_title=m.get("yes_sub_title"), rules_primary=m.get("rules_primary"),
                                      strike=m.get("floor_strike") or m.get("cap_strike"), n_in_event=len(ev[0]["markets"]))
        out[t] = r
        print(t, r["n_open_events"], r["snap"], flush=True)
    json.dump(out, open("data/corr/open.json", "w"), indent=1)
