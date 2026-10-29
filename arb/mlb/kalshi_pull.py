"""Kalshi MLB data pull (public unauthenticated GETs only). Steps: meta, map, parse, candles <set>.

Usage: .venv/bin/python -m arb.mlb.kalshi_pull meta|map|parse|estimate|candles confirmation|discovery
Hard rules (PREREG_MLB_CORR_v1 §10): >=2.0 s between calls, <=90 per rolling 300 s, quiet windows
09:25-09:45 ET daily and Mon 10:25-10:45 ET, 429 -> Retry-After (else 120 s), second 429 aborts.
Every call is logged to logs/mlb_kalshi_requests.log. Confirmation candles are SEALED: only counts printed.
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

BASE = "https://api.elections.kalshi.com/trade-api/v2"
ET = ZoneInfo("America/New_York")
LOG = "logs/mlb_kalshi_requests.log"
MARKETS_DB = "data/mlb/kalshi_markets.sqlite"
CONFIRM_DB = "data/mlb/kalshi_confirm.sqlite"
DISC_DB = "data/mlb/kalshi_candles.sqlite"
SERIES = ["KXMLBGAME", "KXMLBTOTAL", "KXMLBSPREAD", "KXMLBTEAMTOTAL", "KXMLBHR", "KXMLBHIT", "KXMLBKS"]
SEASON_START = "2026-03-20"
PACE, WMAX, WSECS = 2.0, 90, 300.0


class Abort(SystemExit):
    pass


# ---------------------------------------------------------------- client
class Client:
    def __init__(self):
        self.times = collections.deque()  # monotonic-agnostic: wall-clock epoch of recent calls
        self.n429 = 0
        self.ncalls = 0
        # seed the rolling window from the shared log so separate processes honor the same budget
        try:
            now = time.time()
            for line in open(LOG).read().splitlines()[-200:]:
                t = dt.datetime.fromisoformat(line.split("\t")[0]).timestamp()
                if now - t < WSECS:
                    self.times.append(t)
        except (OSError, ValueError):
            pass

    @staticmethod
    def _quiet_wait(now: dt.datetime) -> float:
        m = now.hour * 60 + now.minute + now.second / 60
        if 9 * 60 + 25 <= m < 9 * 60 + 45:
            return (9 * 60 + 45 - m) * 60 + 1
        if now.weekday() == 0 and 10 * 60 + 25 <= m < 10 * 60 + 45:
            return (10 * 60 + 45 - m) * 60 + 1
        return 0.0

    def _pace(self):
        while True:
            now = time.time()
            q = self._quiet_wait(dt.datetime.now(ET))
            if q:
                time.sleep(q)
                continue
            while self.times and now - self.times[0] >= WSECS:
                self.times.popleft()
            w = 0.0
            if self.times:
                w = max(w, PACE + 0.05 - (now - self.times[-1]))
            if len(self.times) >= WMAX - 2:  # 2-call safety margin under 90
                w = max(w, WSECS - (now - self.times[0]) + 0.5)
            if w <= 0:
                return
            time.sleep(w)

    def get(self, path: str, params: dict | None = None):
        """Returns (status, parsed_json_or_None). Aborts on second 429."""
        url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
        while True:
            self._pace()
            status, body, ra = 0, b"", None
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "probly-mlb-readonly"}), timeout=60) as r:
                    status, body = r.status, r.read()
            except urllib.error.HTTPError as e:
                status, body, ra = e.code, e.read(), e.headers.get("Retry-After")
            except Exception as e:  # network error: log, back off, retry (not a 429)
                self.times.append(time.time())
                self._log(url, f"ERR:{type(e).__name__}", 0)
                time.sleep(30)
                continue
            self.times.append(time.time())
            self.ncalls += 1
            self._log(url, status, len(body))
            if status == 429:
                self.n429 += 1
                if self.n429 >= 2:
                    self._log("ABORT second 429", 429, 0)
                    raise Abort("second 429 - aborting")
                time.sleep(float(ra) if ra and ra.replace(".", "").isdigit() else 120)
                continue
            if status >= 500:  # transient server error: wait and retry
                time.sleep(30)
                continue
            try:
                return status, json.loads(body) if body else None
            except ValueError:
                return status, None

    @staticmethod
    def _log(url, status, nbytes):
        with open(LOG, "a") as f:
            f.write(f"{dt.datetime.now(ET).isoformat()}\t{url}\t{status}\t{nbytes}\n")


# ---------------------------------------------------------------- ticker parsing
EV_RE = re.compile(r"^(KX[A-Z0-9]+)-(\d\d)([A-Z]{3})(\d\d)(\d{4})?([A-Z]+)$")
MON = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}


def parse_event_ticker(ev: str):
    """KXMLBHR-26SEP152140MIAAZ -> (series, date 'YYYY-MM-DD', 'HH:MM' or None, 'MIAAZ')."""
    m = EV_RE.match(ev)
    if not m or m.group(3) not in MON:
        return None
    s, yy, mon, dd, hhmm, teams = m.groups()
    d = f"20{yy}-{MON[mon]:02d}-{dd}"
    t = f"{hhmm[:2]}:{hhmm[2:]}" if hhmm else None
    return s, d, t, teams


def split_teams(s: str, codes: set[str]):
    """Split concatenated away+home codes; returns list of (away, home) candidates."""
    return [(s[:i], s[i:]) for i in range(1, len(s)) if s[:i] in codes and s[i:] in codes]


# Kalshi team codes == MLB Stats API abbreviations for all 30 clubs (verified vs games.parquet); AL/NL = All-Star game.
KALSHI_TO_MLB = {c: c for c in "ATH ATL AZ BAL BOS CHC CIN CLE COL CWS DET HOU KC LAA LAD MIA MIL MIN NYM NYY PHI PIT SD SEA SF STL TB TEX TOR WSH".split()}


def parse_market(row: dict) -> dict:
    """row: ticker, series, title, yes_sub_title; optional teams=(away, home) to resolve the player's team code.
    SPREAD: YES iff `team` wins by MORE than `line` runs (line x.5). TEAMTOTAL: YES iff `team` scores MORE than `line` runs.
    TOTAL: YES iff combined runs > line. HR/HIT/KS: YES iff player records >= k."""
    t, series, title, ysub = row["ticker"], row["series"], row.get("title") or "", row.get("yes_sub_title") or ""
    out = dict(ticker=t, series=series, team=None, line=None, player=None, team_code=None, k=None)
    suffix = t.split("-", 2)[2] if t.count("-") >= 2 else ""
    if series == "KXMLBGAME":
        out["team"] = suffix
    elif series == "KXMLBTOTAL":
        m = re.search(r"Over ([\d.]+) runs", ysub)
        out["line"] = float(m.group(1)) if m else None
    elif series == "KXMLBSPREAD":
        m = re.match(r"^[A-Z]+", suffix)
        out["team"] = m.group(0) if m else None
        m = re.search(r"wins by over ([\d.]+) runs", ysub)
        out["line"] = float(m.group(1)) if m else None
    elif series == "KXMLBTEAMTOTAL":
        m = re.match(r"^[A-Z]+", suffix)
        out["team"] = m.group(0) if m else None
        m = re.search(r"over ([\d.]+) runs scored", ysub)
        out["line"] = float(m.group(1)) if m else None
    elif series in ("KXMLBHR", "KXMLBHIT", "KXMLBKS"):
        m = re.match(r"^(.+?):\s*(\d+)\+", ysub) or re.match(r"^(.+?):\s*(\d+)\+", title)
        if m:
            out["player"], out["k"] = m.group(1).strip(), int(m.group(2))
        seg = suffix.rsplit("-", 1)[0]
        cands = [c for c in (row.get("teams") or ()) if seg.startswith(c)]
        out["team_code"] = max(cands, key=len) if cands else None
    return out


# ---------------------------------------------------------------- step 1: meta
def _db(path):
    c = sqlite3.connect(path)
    c.execute("PRAGMA journal_mode=WAL")
    return c


def meta():
    c = _db(MARKETS_DB)
    c.execute("""CREATE TABLE IF NOT EXISTS markets(ticker TEXT PRIMARY KEY, event_ticker TEXT, series TEXT, title TEXT,
        yes_sub_title TEXT, rules_primary TEXT, rules_secondary TEXT, result TEXT, settlement_value REAL,
        open_time TEXT, close_time TEXT, raw_json TEXT, tier TEXT, settled_time TEXT)""")
    c.execute("CREATE TABLE IF NOT EXISTS meta_progress(series TEXT, tier TEXT, cursor TEXT, done INT, PRIMARY KEY(series,tier))")
    cl = Client()
    for s in SERIES:
        for tier, path, extra in (("historical", "/historical/markets", {}), ("live", "/markets", {"status": "settled"})):
            r = c.execute("SELECT cursor, done FROM meta_progress WHERE series=? AND tier=?", (s, tier)).fetchone()
            if r and r[1]:
                continue
            cursor = r[0] if r else ""
            while True:
                p = {"series_ticker": s, "limit": 1000, **extra}
                if cursor:
                    p["cursor"] = cursor
                st, j = cl.get(path, p)
                if st != 200 or j is None:
                    raise SystemExit(f"meta {s} {tier} status {st}")
                rows = []
                for m in j.get("markets", []):
                    pe = parse_event_ticker(m["event_ticker"])
                    if not pe or pe[1] < SEASON_START:
                        continue
                    sv = m.get("settlement_value_dollars", m.get("settlement_value"))
                    try:
                        sv = float(sv) if sv not in (None, "") else None
                    except ValueError:
                        sv = None
                    rows.append((m["ticker"], m["event_ticker"], s, m.get("title"), m.get("yes_sub_title"),
                                 m.get("rules_primary"), m.get("rules_secondary"), m.get("result"), sv,
                                 m.get("open_time"), m.get("close_time"), json.dumps(m), tier,
                                 m.get("settlement_ts") or m.get("expiration_time") or m.get("close_time")))
                c.executemany("INSERT OR REPLACE INTO markets VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
                cursor = j.get("cursor") or ""
                c.execute("INSERT OR REPLACE INTO meta_progress VALUES(?,?,?,?)", (s, tier, cursor, 0 if cursor else 1))
                c.commit()
                print(f"meta {s} {tier}: +{len(rows)} of {len(j.get('markets', []))}, cursor={'yes' if cursor else 'done'}", flush=True)
                if not cursor:
                    break
    print("calls", cl.ncalls, "429s", cl.n429)


# ---------------------------------------------------------------- step 2: map
def _events(c):
    return c.execute("SELECT DISTINCT event_ticker, series FROM markets").fetchall()


def build_map():
    import pandas as pd
    c = _db(MARKETS_DB)
    codes = {r[0].rsplit("-", 1)[1] for r in c.execute("SELECT ticker FROM markets WHERE series='KXMLBGAME'")}
    g = pd.read_parquet("data/mlb/games.parquet")
    g["start_et"] = g.start_utc.dt.tz_convert(ET)
    g["date_et"] = g.start_et.dt.strftime("%Y-%m-%d")
    rows = []
    for ev, series in _events(c):
        pe = parse_event_ticker(ev)
        rec = dict(event_ticker=ev, series=series, gamePk=None, start_et_ticker=None, match_quality="bad_ticker",
                   date=None, away=None, home=None, game_type=None, gap_min=None)
        rows.append(rec)
        if not pe:
            continue
        _, d, hhmm, tm = pe
        rec["date"] = d
        if hhmm is None:
            rec["match_quality"] = "no_time"
            continue
        rec["start_et_ticker"] = f"{d} {hhmm}"
        k_start = dt.datetime.fromisoformat(f"{d}T{hhmm}:00").replace(tzinfo=ET)
        sp = split_teams(tm, codes)
        if not sp:
            rec["match_quality"] = "team_split_none"
            continue
        best = None
        for away, home in sp:  # usually exactly one split
            a, h = KALSHI_TO_MLB.get(away), KALSHI_TO_MLB.get(home)
            cand = g[(g.away == a) & (g.home == h) & g.date_et.isin([d])]
            if cand.empty:  # tolerate a +-1 day ticker/ET-date offset, but only if the start time agrees (<=3h)
                d0 = dt.date.fromisoformat(d)
                alt = [str(d0 - dt.timedelta(days=1)), str(d0 + dt.timedelta(days=1))]
                cand = g[(g.away == a) & (g.home == h) & g.date_et.isin(alt)]
                cand = cand[(cand.start_et - k_start).abs() <= pd.Timedelta(hours=3)]
            if not cand.empty:
                best = (away, home, cand)
                break
        if best is None:
            rec.update(away=sp[0][0], home=sp[0][1], match_quality="no_game")
            continue
        away, home, cand = best
        rec.update(away=away, home=home)
        gaps = sorted(((abs((r.start_et - k_start).total_seconds()) / 60, r.gamePk, r.game_type, r.date_et) for r in cand.itertuples()))
        rec["gap_min"] = gaps[0][0]
        rec["game_type"] = gaps[0][2]
        if len(gaps) == 1:
            gap = gaps[0][0]
            q = "unique" if gap <= 15 else "unique_timediff" if gap <= 180 else "time_conflict"  # >3h: likely a postponed twin
            rec.update(gamePk=gaps[0][1] if q != "time_conflict" else None, match_quality=q)
        elif gaps[0][0] <= 15 and gaps[1][0] > gaps[0][0] + 30:
            rec.update(gamePk=gaps[0][1], match_quality="dh_nearest")
        else:
            rec["match_quality"] = "ambiguous"
    df = pd.DataFrame(rows)
    df["gamePk"] = df.gamePk.astype("Int64")
    df.to_parquet("data/mlb/event_map.parquet", index=False)
    for ser, grp in df.groupby("series"):
        print(ser, len(grp), grp.match_quality.value_counts().to_dict())
    print("ALL", len(df), df.match_quality.value_counts().to_dict())
    return df


# ---------------------------------------------------------------- step 3: parse
def parse_all():
    import pandas as pd
    c = _db(MARKETS_DB)
    em = pd.read_parquet("data/mlb/event_map.parquet").set_index("event_ticker")
    c.execute("""CREATE TABLE IF NOT EXISTS parsed(ticker TEXT PRIMARY KEY, event_ticker TEXT, series TEXT, team TEXT,
        line REAL, player TEXT, team_code TEXT, k INT, fair_price_settled INT)""")
    out = []
    for t, ev, ser, title, ysub, sv in c.execute("SELECT ticker,event_ticker,series,title,yes_sub_title,settlement_value FROM markets"):
        teams = ()
        if ev in em.index and em.at[ev, "away"]:
            teams = (em.at[ev, "away"], em.at[ev, "home"])
        r = parse_market(dict(ticker=t, series=ser, title=title, yes_sub_title=ysub, teams=teams))
        fp = int(sv is not None and 0 < sv < 1)
        out.append((t, ev, ser, r["team"], r["line"], r["player"], r["team_code"], r["k"], fp))
    c.executemany("INSERT OR REPLACE INTO parsed VALUES(?,?,?,?,?,?,?,?,?)", out)
    c.commit()
    for r in c.execute("""SELECT series, count(*), sum(fair_price_settled), sum(line IS NULL AND series IN
        ('KXMLBTOTAL','KXMLBSPREAD','KXMLBTEAMTOTAL')), sum(k IS NULL AND series IN ('KXMLBHR','KXMLBHIT','KXMLBKS')),
        sum(team_code IS NULL AND series IN ('KXMLBHR','KXMLBHIT','KXMLBKS')) FROM parsed GROUP BY 1"""):
        print("parsed", r, "(series, n, fair_price_settled, line_null, k_null, team_code_null)")


# ---------------------------------------------------------------- step 4: candles
SETS = {"confirmation": CONFIRM_DB, "discovery": DISC_DB}


def _scope(c_m, setname):
    """-> list of (ticker, series, gamePk, window_start_ts, window_end_ts, tier, event_ticker)."""
    import pandas as pd
    gs = json.load(open("reports/mlb_corr/game_sets.json"))[setname]
    em = pd.read_parquet("data/mlb/event_map.parquet")
    em = em[em.gamePk.isin(gs) & em.match_quality.isin(["unique", "unique_timediff", "dh_nearest"])].set_index("event_ticker")
    q = """SELECT m.ticker, m.event_ticker, m.series, m.tier, m.settled_time, p.k FROM markets m JOIN parsed p USING(ticker)
           WHERE p.fair_price_settled=0"""
    out = []
    for t, ev, ser, tier, close, k in c_m.execute(q):
        if ev not in em.index or (ser == "KXMLBHIT" and (k is None or k > 2)):
            continue
        st = dt.datetime.fromisoformat(em.at[ev, "start_et_ticker"].replace(" ", "T") + ":00").replace(tzinfo=ET)
        T = st - dt.timedelta(minutes=30)
        out.append((t, ser, int(em.at[ev, "gamePk"]), int((T - dt.timedelta(minutes=15)).timestamp()),
                    int((T + dt.timedelta(minutes=15)).timestamp()), tier, close))
    out.sort(key=lambda r: (r[3], r[2], r[0]))
    return out


def _cutoff(cl):
    st, j = cl.get("/historical/cutoff")
    return dt.datetime.fromisoformat(j["market_settled_ts"].replace("Z", "+00:00")) if st == 200 and j else None


def _tier(row, cutoff):
    t, tier, close = row[0], row[5], row[6]
    if tier == "historical":
        return "historical"
    if cutoff and close and dt.datetime.fromisoformat(close.replace("Z", "+00:00")) < cutoff:
        return "historical"
    return "live"


def _plan(cm, setname, cutoff, done=()):
    scope = [r for r in _scope(cm, setname) if r[0] not in done]
    import numpy as np
    pks = sorted({r[2] for r in scope})
    rank = {pk: i for i, pk in enumerate(np.random.default_rng(1).permutation(len(pks)))}
    rank = {pk: rank[i] for i, pk in enumerate(pks)}  # fixed random game order: any prefix is a random subset of games
    hist = sorted((r for r in scope if _tier(r, cutoff) == "historical"), key=lambda r: (rank[r[2]], r[0]))
    live = [r for r in scope if _tier(r, cutoff) == "live"]
    groups = collections.defaultdict(list)
    for r in live:
        groups[(r[3], r[4])].append(r)
    batches = [(k, v[i:i + 100]) for k, v in sorted(groups.items()) for i in range(0, len(v), 100)]
    return scope, hist, batches


def estimate(cl=None):
    cl = cl or Client()
    cutoff = _cutoff(cl)
    cm = _db(MARKETS_DB)
    print("historical cutoff market_settled_ts:", cutoff)
    for s in ("confirmation", "discovery"):
        done = _done(s)
        scope, hist, batches = _plan(cm, s, cutoff, done)
        calls = len(hist) + len(batches)
        print(f"{s}: markets_in_scope={len(scope)} (already pulled {len(done)}) historical_calls={len(hist)} "
              f"batch_calls={len(batches)} total_calls={calls} ETA_h@3.34s/call={calls * 300 / 90 / 3600:.1f}")


def _cdb(setname):
    c = _db(SETS[setname])
    c.execute("""CREATE TABLE IF NOT EXISTS candles_raw(ticker TEXT PRIMARY KEY, gamePk INT, tier TEXT, status INT,
        start_ts INT, end_ts INT, n_candles INT, raw_json TEXT, fetched_at TEXT)""")
    return c


def _done(setname):
    import os
    if not os.path.exists(SETS[setname]):
        return set()
    return {r[0] for r in _cdb(setname).execute("SELECT ticker FROM candles_raw WHERE status=200")}


def candles(setname, cl):
    cm = _db(MARKETS_DB)
    cc = _cdb(setname)
    cutoff = _cutoff(cl)
    scope, hist, batches = _plan(cm, setname, cutoff, _done(setname))
    now = dt.datetime.now(ET).isoformat()
    print(f"[{now}] candles {setname}: {len(scope)} markets to pull, {len(hist)} historical + {len(batches)} batch calls", flush=True)

    def save(r, tier, status, cs):
        cc.execute("INSERT OR REPLACE INTO candles_raw VALUES(?,?,?,?,?,?,?,?,?)",
                   (r[0], r[2], tier, status, r[3], r[4], len(cs) if cs is not None else None,
                    json.dumps(cs) if cs is not None else None, dt.datetime.now(ET).isoformat()))

    def do_batch(rows):
        st, j = cl.get("/markets/candlesticks", dict(market_tickers=",".join(r[0] for r in rows), start_ts=rows[0][3],
                                                     end_ts=rows[0][4], period_interval=1))
        got = {m["market_ticker"]: m.get("candlesticks", []) for m in (j or {}).get("markets", [])} if st == 200 else {}
        for r in rows:
            save(r, "live", st, got.get(r[0], []) if st == 200 else None)
        cc.commit()
        return st

    # time order: interleave so partial results span the set; historical per-market, live batched
    n = 0
    for r in hist:
        st, j = cl.get(f"/historical/markets/{urllib.parse.quote(r[0])}/candlesticks",
                       dict(start_ts=r[3], end_ts=r[4], period_interval=1))
        if st == 404:  # not (yet) archived: fall back to the live batch endpoint
            do_batch([r])
        else:
            save(r, "historical", st, (j or {}).get("candlesticks", []) if st == 200 else None)
            cc.commit()
        n += 1
        if n % 200 == 0:
            print(f"[{dt.datetime.now(ET).isoformat()}] {setname}: {n}/{len(hist)} historical, calls={cl.ncalls}, 429s={cl.n429}", flush=True)
    for i, (_, rows) in enumerate(batches):
        do_batch(rows)
        if (i + 1) % 20 == 0:
            print(f"[{dt.datetime.now(ET).isoformat()}] {setname}: {i + 1}/{len(batches)} batches, calls={cl.ncalls}, 429s={cl.n429}", flush=True)
    stat = dict(cc.execute("SELECT status, count(*) FROM candles_raw GROUP BY 1").fetchall())
    # SEALED for confirmation: counts only
    print(f"[{dt.datetime.now(ET).isoformat()}] {setname} DONE markets_by_status={stat} "
          f"with_candles={cc.execute('SELECT count(*) FROM candles_raw WHERE n_candles>0').fetchone()[0]} calls={cl.ncalls} 429s={cl.n429}", flush=True)


if __name__ == "__main__":
    step = sys.argv[1]
    if step == "meta":
        meta()
    elif step == "map":
        build_map()
    elif step == "parse":
        parse_all()
    elif step == "estimate":
        estimate()
    elif step == "candles":
        cl = Client()
        for sname in sys.argv[2:]:
            candles(sname, cl)
