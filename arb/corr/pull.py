"""Kalshi pull for the three CORR3 lanes (S soccer, F football, P passing). Public GETs only, via ONE arb.mlb.kalshi_pull.Client.
Usage (repo root): .venv/bin/python -m arb.corr.pull meta | events | estimate | candles
Confirmation candles are SEALED: only counts are printed. Resumable; INSERT OR REPLACE.
"""
from __future__ import annotations

import collections, datetime as dt, json, os, re, sqlite3, sys, urllib.parse
from zoneinfo import ZoneInfo

from arb.mlb.kalshi_pull import Client, ET

DB = "data/corr/markets.sqlite"
D0, D1, DISC_END = "2026-08-30", "2026-10-28", "2026-10-04"
LEAGUES = ["EPL", "LALIGA", "SERIEA", "BUNDESLIGA", "UCL", "MLS"]
SOCCER = {f"KX{l}{s}": ("S", l) for l in LEAGUES for s in ("GAME", "TOTAL", "SPREAD", "BTTS", "SCORE", "TEAMTOTAL")}
FOOT = {f"KX{l}{s}": ("F", l) for l in ("NFL", "NCAAF") for s in ("GAME", "SPREAD", "TOTAL", "TEAMTOTAL")}
PASS = {"KXNFLPASSYDS": ("P", "NFL"), "KXNFLRECYDS": ("P", "NFL")}
SERIES = {**SOCCER, **FOOT, **PASS}
DUR = {"NFL": 3.5, "NCAAF": 3.75}  # hours; soccer 2.0
EV_RE = re.compile(r"^(KX[A-Z0-9]+)-(\d\d)([A-Z]{3})(\d\d)(\d{4})?([A-Z0-9]+)$")
MON = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}
PRE, POST = 75, 15  # widened candle window, minutes around T


def parse_ev(ev):
    m = EV_RE.match(ev)
    if not m or m.group(3) not in MON:
        return None
    s, yy, mon, dd, hhmm, teams = m.groups()
    return s, f"20{yy}-{MON[mon]:02d}-{dd}", hhmm, f"{yy}{mon}{dd}{hhmm or ''}{teams}"


def db():
    c = sqlite3.connect(DB); c.execute("PRAGMA journal_mode=WAL"); return c


def iso(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def meta():
    c = db()
    c.execute("""CREATE TABLE IF NOT EXISTS markets(ticker TEXT PRIMARY KEY, event_ticker TEXT, series TEXT, lane TEXT, league TEXT,
        game_key TEXT, title TEXT, yes_sub_title TEXT, rules_primary TEXT, result TEXT, settlement_value REAL,
        expected_expiration_time TEXT, close_time TEXT, raw_json TEXT)""")
    c.execute("CREATE TABLE IF NOT EXISTS meta_done(series TEXT PRIMARY KEY, n INT)")
    cl = Client()
    st, j = cl.get("/historical/cutoff"); print("cutoff", j, flush=True)
    min_ts = int(dt.datetime.fromisoformat(D0 + "T00:00:00").replace(tzinfo=ET).timestamp())
    for s, (lane, lg) in SERIES.items():
        if c.execute("SELECT 1 FROM meta_done WHERE series=?", (s,)).fetchone():
            continue
        cursor, n = "", 0
        while True:
            p = {"series_ticker": s, "status": "settled", "limit": 1000, "min_settled_ts": min_ts}
            if cursor: p["cursor"] = cursor
            st, j = cl.get("/markets", p)
            if st != 200 or j is None:
                print("meta", s, "status", st, "(series skipped)", flush=True); break
            rows = []
            for m in j.get("markets", []):
                pe = parse_ev(m["event_ticker"])
                if not pe or not (D0 <= pe[1] <= D1): continue
                sv = m.get("settlement_value_dollars", m.get("settlement_value"))
                try: sv = float(sv) if sv not in (None, "") else None
                except ValueError: sv = None
                rows.append((m["ticker"], m["event_ticker"], s, lane, lg, pe[3], m.get("title"), m.get("yes_sub_title"),
                             m.get("rules_primary"), m.get("result"), sv, m.get("expected_expiration_time"),
                             m.get("close_time"), json.dumps(m)))
            c.executemany("INSERT OR REPLACE INTO markets VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows); c.commit()
            n += len(rows); cursor = j.get("cursor") or ""
            if not cursor: break
        c.execute("INSERT OR REPLACE INTO meta_done VALUES(?,?)", (s, n)); c.commit()
        print(f"meta {s}: {n} markets", flush=True)
    print("calls", cl.ncalls, "429s", cl.n429)


def events():
    c = db()
    c.execute("DROP TABLE IF EXISTS events")
    c.execute("""CREATE TABLE events(lane TEXT, game_key TEXT, league TEXT, date_et TEXT, kickoff_est TEXT, kickoff_src TEXT,
        T_ts INT, T_et TEXT, set_name TEXT, n_markets INT, n_fair INT, PRIMARY KEY(lane, game_key, league))""")
    c.execute("DROP TABLE IF EXISTS fair_price_settled")
    c.execute("CREATE TABLE fair_price_settled(ticker TEXT PRIMARY KEY, fair INT)")
    c.execute("INSERT INTO fair_price_settled SELECT ticker, settlement_value>0 AND settlement_value<1 FROM markets")
    # NCAAF: only games with a TEAMTOTAL event
    tt = {r[0] for r in c.execute("SELECT game_key FROM markets WHERE series='KXNCAAFTEAMTOTAL'")}
    # expected_expiration_time source: GAME market of that game (league-level), else min over any market of the game
    exp = {}
    for lg, gk, e in c.execute("SELECT league, game_key, expected_expiration_time FROM markets WHERE series LIKE '%GAME' AND expected_expiration_time IS NOT NULL"):
        exp[(lg, gk)] = ("GAME", e)
    for lg, gk, e in c.execute("SELECT league, game_key, min(expected_expiration_time) FROM markets WHERE expected_expiration_time IS NOT NULL GROUP BY 1,2"):
        exp.setdefault((lg, gk), ("ANY", e))
    groups = collections.defaultdict(lambda: [0, 0, None])
    for t, ev, lane, lg, gk, sv in c.execute("SELECT ticker,event_ticker,lane,league,game_key,settlement_value FROM markets"):
        if lg == "NCAAF" and gk not in tt: continue
        g = groups[(lane, gk, lg)]; g[0] += 1; g[1] += int(sv is not None and 0 < sv < 1); g[2] = ev
    rows = []
    for (lane, gk, lg), (n, nf, ev) in groups.items():
        _, d, hhmm, _ = parse_ev(ev)
        if hhmm:
            k = dt.datetime.fromisoformat(f"{d}T{hhmm[:2]}:{hhmm[2:]}:00").replace(tzinfo=ET); src = "ticker"
        else:
            src_, e = exp.get((lg, gk), (None, None))
            if not e: print("no expiration for", lane, gk); continue
            k = iso(e).astimezone(ET) - dt.timedelta(hours=DUR.get(lg, 2.0)); src = f"exp_{src_}-D"
            # prereg: kickoff - 30min = T; the 'further 30 min' buffer is the T offset itself (per task brief T = exp - D - 30)
        T = k - dt.timedelta(minutes=30)
        rows.append((lane, gk, lg, d, k.isoformat(), src, int(T.timestamp()), T.isoformat(),
                     "disc" if d <= DISC_END else "confirm", n, nf))
    c.executemany("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows); c.commit()
    for r in c.execute("SELECT lane, league, set_name, kickoff_src, count(*), sum(n_markets), sum(n_fair) FROM events GROUP BY 1,2,3,4 ORDER BY 1,2,3"):
        print(r)


def plan():
    """-> sorted list of tasks (setname, lane, T, game_key, [tickers]) honouring existing candles_raw rows."""
    c = db()
    done = {}
    tasks = []
    ev = c.execute("SELECT lane, game_key, league, T_ts, set_name FROM events").fetchall()
    for lane, gk, lg, T, sn in ev:
        tick = [r[0] for r in c.execute("""SELECT m.ticker FROM markets m JOIN fair_price_settled f USING(ticker)
                 WHERE m.lane=? AND m.game_key=? AND m.league=? AND f.fair=0 ORDER BY m.ticker""", (lane, gk, lg))]
        key = (sn, lane)
        if key not in done:
            p = f"data/corr/{lane}_{sn}.sqlite"
            done[key] = {r[0] for r in cdb(lane, sn).execute("SELECT ticker FROM candles_raw WHERE status=200")} if os.path.exists(p) else set()
        tick = [t for t in tick if t not in done[key]]
        for i in range(0, len(tick), 100):
            tasks.append((sn, lane, T, gk, tick[i:i + 100]))
    tasks.sort(key=lambda t: (t[0] != "disc", t[2], t[1], t[3]))
    return tasks


def cdb(lane, sn):
    c = sqlite3.connect(f"data/corr/{lane}_{sn}.sqlite"); c.execute("PRAGMA journal_mode=WAL")
    c.execute("""CREATE TABLE IF NOT EXISTS candles_raw(ticker TEXT PRIMARY KEY, game_key TEXT, status INT, start_ts INT,
        end_ts INT, n_candles INT, raw_json TEXT, fetched_at TEXT)""")
    return c


def estimate():
    t = plan(); cnt = collections.Counter((x[0], x[1]) for x in t)
    print("batch calls by (set,lane):", dict(cnt), "total", len(t), "markets", sum(len(x[4]) for x in t), flush=True)
    print(f"ETA at ~3.4 s/call: {len(t) * 3.4 / 3600:.1f} h")


def candles():
    cl = Client(); dbs = {}
    tasks = plan(); left = collections.Counter((x[0], x[1]) for x in tasks)
    for sn in ("disc", "confirm"):
        for lane in "SFP":
            if left[(sn, lane)] == 0 and os.path.exists(f"data/corr/{lane}_{sn}.sqlite"):
                open(f"data/corr/{lane}_{sn}.DONE", "w").close()
    print(f"[{dt.datetime.now(ET).isoformat()}] candles: {len(tasks)} calls planned", flush=True)
    for i, (sn, lane, T, gk, tick) in enumerate(tasks):
        a, b = T - PRE * 60, T + POST * 60
        st, j = cl.get("/markets/candlesticks", dict(market_tickers=",".join(tick), start_ts=a, end_ts=b, period_interval=1))
        got = {m["market_ticker"]: m.get("candlesticks", []) for m in (j or {}).get("markets", [])} if st == 200 else {}
        if (sn, lane) not in dbs: dbs[(sn, lane)] = cdb(lane, sn)
        c = dbs[(sn, lane)]
        now = dt.datetime.now(ET).isoformat()
        for t in tick:
            cs = got.get(t, []) if st == 200 else None
            c.execute("INSERT OR REPLACE INTO candles_raw VALUES(?,?,?,?,?,?,?,?)",
                      (t, gk, st, a, b, None if cs is None else len(cs), None if cs is None else json.dumps(cs), now))
        c.commit()
        left[(sn, lane)] -= 1
        if left[(sn, lane)] == 0:
            open(f"data/corr/{lane}_{sn}.DONE", "w").close()
            print(f"[{now}] {lane}_{sn} DONE", flush=True)
        if (i + 1) % 20 == 0:
            print(f"[{now}] {i + 1}/{len(tasks)} calls={cl.ncalls} 429s={cl.n429}", flush=True)
    print(f"[{dt.datetime.now(ET).isoformat()}] ALL DONE calls={cl.ncalls} 429s={cl.n429}", flush=True)


if __name__ == "__main__":
    {"meta": meta, "events": events, "estimate": estimate, "candles": candles}[sys.argv[1]]()
