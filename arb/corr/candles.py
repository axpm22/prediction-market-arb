import json, datetime as dt
from arb.corr.kc import get
CUT = dt.datetime(2026, 8, 30, tzinfo=dt.timezone.utc)
PICK = {"NFL": ("KXNFLRECYDS", 1), "NCAAF": ("KXNCAAFTEAMTOTAL", 1), "NBA": ("KXNBAPTS", 1), "NCAAMB": ("KXNCAAMBSPREAD", 1),
        "NHL": ("KXNHLGOAL", 1), "EPL": ("KXEPLSCORE", 1), "ATP": ("KXATPSETWINNER", 1), "UFC": ("KXUFCMOV", 1), "PGA": ("KXPGATOP10", 1),
        "NFL_oldest": ("KXNFLRECYDS", -1), "NBA_oldest": ("KXNBAPTS", -1), "NHL_oldest": ("KXNHLGOAL", -1)}
def ts(s): return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
out = {}
for k, (ser, which) in PICK.items():
    st, j = get("/events", {"series_ticker": ser, "status": "settled", "limit": 200}) if which == 1 else (None, None)
    if which == 1:
        et = j["events"][1]["event_ticker"]   # [0] may be just-finished; take 2nd newest
    else:   # oldest from last cached page of settled.py: re-walk cursors from cache
        cur = ""
        while True:
            p = {"series_ticker": ser, "status": "settled", "limit": 200}
            if cur: p["cursor"] = cur
            st, j = get("/events", p); cur = j.get("cursor")
            if not cur: break
        et = j["events"][-1]["event_ticker"]
    st, ej = get(f"/events/{et}")
    ms = (ej or {}).get("markets") or []
    if not ms:
        st, hj = get("/historical/markets", {"event_ticker": et, "limit": 1000}); ms = (hj or {}).get("markets") or []
    if not ms:
        out[k] = dict(series=ser, event=et, error="no markets found"); continue
    m = max(ms, key=lambda m: float(m.get("volume_fp") or 0))
    o, c = ts(m["open_time"]), ts(m["close_time"])
    hist = dt.datetime.fromtimestamp(c, dt.timezone.utc) < CUT
    path = f"/historical/markets/{m['ticker']}/candlesticks" if hist else f"/series/{ser}/markets/{m['ticker']}/candlesticks"
    st, cj = get(path, {"start_ts": o, "end_ts": min(c, o + 14 * 86400), "period_interval": 60})
    cs = (cj or {}).get("candlesticks") or []
    nz = [x for x in cs if float((x.get("volume_fp") or x.get("volume") or 0)) > 0]
    out[k] = dict(series=ser, event=et, ticker=m["ticker"], n_markets=len(ms), vol=m.get("volume_fp"), open=m["open_time"], close=m["close_time"],
                  historical_endpoint=hist, status=st, n_candles=len(cs), n_with_volume=len(nz),
                  first_end=cs[0]["end_period_ts"] if cs else None, last_end=cs[-1]["end_period_ts"] if cs else None,
                  keys=list(cs[0].keys()) if cs else None)
    print(k, out[k], flush=True)
    json.dump(out, open("data/corr/candles.json", "w"), indent=1)
