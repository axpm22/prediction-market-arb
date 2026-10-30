import json
from arb.corr.kc import get
from arb.corr.inv import snap
from arb.corr.settled import d
cats = {}
for c in ["Climate and Weather", "Economics"]:
    for s in get("/series", {"category": c, "include_volume": "true"})[1]["series"]: cats[s["ticker"]] = s
L = {"weather": "KXHIGHNY KXHIGHPHIL KXHIGHCHI KXHIGHDEN KXHIGHMIA KXHIGHAUS KXHIGHLAX KXLOWTNYC KXLOWTPHIL".split(),
     "cpi": "KXCPI KXCPICORE KXCPIYOY KXCPICOREYOY".split(), "gas": "KXAAAGASD KXAAAGASW KXAAAGASM".split()}
out = {}
for fam, ts in L.items():
    for t in ts:
        s = cats[t]
        st, j = get("/events", {"series_ticker": t, "status": "open", "with_nested_markets": "true", "limit": 200}); ev = j["events"]
        r = dict(family=fam, title=s["title"], freq=s["frequency"], fee_type=s["fee_type"], fee_mult=s["fee_multiplier"], total_volume=float(s["volume_fp"]),
                 n_open_events=len(ev), open_event_tickers=[e["event_ticker"] for e in ev][:12], snap=snap(ev))
        if ev and ev[0]["markets"]:
            m = ev[0]["markets"][0]; r["sample_market"] = dict(ticker=m["ticker"], yes_sub_title=m.get("yes_sub_title"), rules_primary=m.get("rules_primary"), n_in_event=len(ev[0]["markets"]))
        n, cur, dates, pages = 0, "", [], 0
        while True:
            p = {"series_ticker": t, "status": "settled", "limit": 200}
            if cur: p["cursor"] = cur
            st, j = get("/events", p); pages += 1; n += len(j["events"]); dates += [d(e["event_ticker"]) for e in j["events"]]; cur = j.get("cursor")
            if not cur or pages >= (2 if fam != "cpi" else 6): break
        dates = [x for x in dates if x]
        r["settled"] = dict(n_counted=n, complete=not cur, earliest_seen=min(dates) if dates else None, latest_seen=max(dates) if dates else None)
        out[t] = r; print(t, r["n_open_events"], r["snap"], r["settled"], flush=True)
        json.dump(out, open("data/corr/nonsports.json", "w"), indent=1)
st, j = get("/series/fee_changes", {})
json.dump(j, open("data/corr/fee_changes.json", "w"), indent=1); print("fee_changes", st, str(j)[:300])
