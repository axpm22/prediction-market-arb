import json, re
from arb.corr.kc import get
from arb.corr.inv import fam_of
o = json.load(open("data/corr/open.json"))
MON = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}
def d(et):
    m = re.search(r"-(\d\d)([A-Z]{3})(\d\d)", et)
    return f"20{m[1]}-{MON[m[2]]:02d}-{m[3]}" if m and m[2] in MON else None
KEY = set("KXNFLRECYDS KXNFLRSHYDS KXNFLPASSYDS KXNFLREC KXNFLTD KXNFLSPREAD KXNFLTEAMTOTAL KXNBAPTS KXNBAREB KXNBASPREAD KXNBATEAMTOTAL KXNHLGOAL KXNHLTOTAL KXNHLSPREAD KXNCAAFSPREAD KXNCAAFTEAMTOTAL KXEPLSCORE KXEPLBTTS KXEPLTEAMTOTAL KXATPSETWINNER KXATPGTOTAL KXUFCMOV KXUFCVICROUND KXPGATOP10 KXNFLGAME KXNBAGAME".split())
res = {}
for t in fam_of:
    r = o[t]
    if r["n_open_events"] == 0 and r["total_volume"] < 5e6: continue
    page, n, cur, dates, pages = 0, 0, "", [], 0
    while True:
        p = {"series_ticker": t, "status": "settled", "limit": 200}
        if cur: p["cursor"] = cur
        st, j = get("/events", p); pages += 1
        ev = j["events"]; n += len(ev); dates += [d(e["event_ticker"]) for e in ev]
        cur = j.get("cursor")
        if not cur or (t not in KEY and pages >= 1) or pages >= 6: break
    dates = [x for x in dates if x]
    res[t] = dict(n_settled_counted=n, complete=not cur, pages=pages, earliest_seen=min(dates) if dates else None, latest_seen=max(dates) if dates else None)
    print(t, res[t], flush=True)
    json.dump(res, open("data/corr/settled.json", "w"), indent=1)
