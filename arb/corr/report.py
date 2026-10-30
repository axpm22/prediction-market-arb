import json, statistics as st
o = json.load(open("data/corr/open.json")); s = json.load(open("data/corr/settled.json"))
c = json.load(open("data/corr/candles.json")); ns = json.load(open("data/corr/nonsports.json"))
ANCH = ("GAME", "SPREAD", "TOTAL", "MATCH", "FIGHT", "TOUR")
inv = dict(generated="2026-10-29", cutoff_historical="2026-08-30", open_series=o, settled_series=s, candle_probes=c, nonsports=ns)
json.dump(inv, open("data/corr/inventory.json", "w"), indent=1)
med = lambda x: round(st.median(x), 3) if x else None
rows = []
for fam in dict.fromkeys(r["family"] for r in o.values()):
    ts = [t for t in o if o[t]["family"] == fam]
    a = [t for t in ts if t[2 + len(fam):] in ANCH and o[t]["snap"]["n_markets"]]
    p = [t for t in ts if t not in a and o[t]["snap"]["n_markets"]]
    g = lambda L, k: med([o[t]["snap"][k] for t in L if o[t]["snap"][k] is not None])
    nev = lambda t: o[t]["n_open_events"]
    sc = lambda L: sorted(((s[t]["n_settled_counted"], t[2 + len(fam):], s[t]["earliest_seen"], s[t]["complete"]) for t in L if t in s), reverse=True)
    rows.append((fam, len(a), len(p), g(a, "med_spread"), g(a, "med_volume"), g(p, "med_spread"), g(p, "med_volume"), sc(p)[:3], sum(o[t]["snap"]["n_markets"] for t in p)))
with open("data/corr/families.txt", "w") as f:
    for r in rows: f.write(repr(r) + "\n")
    f.write("\nopen events by series:\n")
    for fam in dict.fromkeys(r["family"] for r in o.values()):
        f.write(fam + ": " + ", ".join(f"{t[2+len(fam):]}={o[t]['n_open_events']}" for t in o if o[t]["family"] == fam) + "\n")
print(open("data/corr/families.txt").read())
