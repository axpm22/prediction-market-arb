"""L14 collector, part two: whale discovery and cold-start backfill.

Split out of arb/whale_tape.py on size alone (modules stay under ~300 lines);
same file, same rules — public GETs, Decimal at the store boundary, and
nothing here can reach data/arb.sqlite.
"""
from __future__ import annotations

import json
from decimal import Decimal

from arb.venues.base import get_json
from arb.whale_tape import (DATA_API, GAMMA, GAMMA_IDS_PER_CALL, LEADERBOARD_PAGE,
                            MARKET_COLS, SPORTS_MARKET_TYPE, _backoff_active, _Bucket,
                            _call, _iso, _keep, _meta, _set_backoff, _set_meta, log)
from arb.models import q6


def _add_wallets(con, pairs, now, source) -> int:
    """New wallets only: a wallet's source is whichever lane saw it first."""
    before = con.total_changes
    con.executemany(
        "INSERT OR IGNORE INTO wallets (proxy_wallet, pseudonym, first_seen, source) "
        "VALUES (?,?,?,?)", [(w, p, now.isoformat(), source) for w, p in pairs])
    con.commit()
    return con.total_changes - before


def snapshot_leaderboard(cfg, con, now, get=get_json) -> int:
    """Seed discovery from the public leaderboard, at most once per UTC day.
    Returns the number of wallets it added (source never enters the score)."""
    lb = cfg["whale"]["leaderboard"]
    day = now.strftime("%Y-%m-%d")
    if not lb["enabled"] or _meta(con, "leaderboard_day") == day or _backoff_active(con, now):
        return 0
    res: dict = {}
    bucket = _Bucket(cfg["whale"]["max_calls_per_min"])
    added = 0
    for page in range(int(lb["pages"])):
        url = (f"{DATA_API}/v1/leaderboard?category={lb['category']}"
               f"&timePeriod={lb['time_period']}&orderBy={lb['order_by']}"
               f"&limit={LEADERBOARD_PAGE}&offset={page * LEADERBOARD_PAGE}")
        try:
            body = _call(bucket, get, url, cfg, res)
        except Exception:
            _set_backoff(con, cfg, now)
            return added
        con.executemany(
            "INSERT INTO leaderboard_snapshots (snapshot_at, category, time_period,"
            " order_by, rank, proxy_wallet, user_name, vol, pnl) VALUES (?,?,?,?,?,?,?,?,?)",
            [(now.isoformat(), str(lb["category"]), str(lb["time_period"]),
              str(lb["order_by"]), int(r.get("rank") or 0), str(r.get("proxyWallet") or ""),
              r.get("userName"), str(q6(Decimal(str(r.get("vol") or 0)))),
              str(q6(Decimal(str(r.get("pnl") or 0))))) for r in body])
        con.commit()
        added += _add_wallets(con, [(str(r.get("proxyWallet") or ""), r.get("userName"))
                                    for r in body], now, "leaderboard")
        if len(body) < LEADERBOARD_PAGE:
            break
    _set_meta(con, "leaderboard_day", day)
    return added


def discover_wallets(cfg, con, now) -> int:
    """A wallet becomes a candidate whale once its cumulative PREGAME BUY
    notional in tracked markets clears discovery_min_notional_usd (spec §5
    step 3). Summed in Decimal, never in SQL floats (rule 5)."""
    floor = Decimal(str(cfg["whale"]["discovery_min_notional_usd"]))
    totals: dict[str, list] = {}
    for wallet, pseudonym, notional in con.execute(
            "SELECT p.proxy_wallet, p.pseudonym, p.notional_usd FROM prints p "
            "JOIN markets m ON m.condition_id = p.condition_id "
            "WHERE p.side='BUY' AND m.start_utc IS NOT NULL "
            "AND p.ts < CAST(strftime('%s', m.start_utc) AS INTEGER)"):
        row = totals.setdefault(wallet, [Decimal(0), pseudonym])
        row[0] += Decimal(notional)
    return _add_wallets(con, [(w, v[1]) for w, v in totals.items() if v[0] >= floor],
                        now, "tape")


def backfill(cfg, con, now, get=get_json) -> dict:
    """Cold start (spec §5 step 4): per-wallet /activity?type=TRADE pages into
    `prints`, FIFO by discovery time, then map any market we have never seen
    through Gamma. Unmappable markets are stored and never scored."""
    w = cfg["whale"]
    bf = w["backfill"]
    res = {"wallets": 0, "rows": 0, "dupes": 0, "calls": 0, "mapped": 0, "unmappable": 0,
           "status_counts": {}, "status": "ok"}
    if _backoff_active(con, now):
        res["status"] = "backoff"
        return res
    wallets = [r[0] for r in con.execute(
        "SELECT proxy_wallet FROM wallets WHERE backfilled_at IS NULL "
        "ORDER BY first_seen, proxy_wallet LIMIT ?", (int(bf["wallets_per_cycle"]),))]
    bucket = _Bucket(w["max_calls_per_min"])
    size = int(bf["page_size"])
    seen: set[str] = set()
    try:
        for wallet in wallets:
            for page in range(int(bf["pages_per_wallet"])):
                body = _call(bucket, get, f"{DATA_API}/activity?user={wallet}&type=TRADE"
                             f"&limit={size}&offset={page * size}", cfg, res)
                _keep(con, body, None, now, res, source="activity")
                seen.update(str(r.get("conditionId") or "") for r in body)
                if len(body) < size:
                    break
            con.execute("UPDATE wallets SET backfilled_at=? WHERE proxy_wallet=?",
                        (now.isoformat(), wallet))
            con.commit()
            res["wallets"] += 1
        _map_markets(cfg, con, seen, now, bucket, get, res)
    except Exception as e:
        log.warning("whale backfill aborted after %d call(s): %s", res["calls"], e)
        _set_backoff(con, cfg, now)
        res["status"] = "error"
    return res


def _map_markets(cfg, con, seen, now, bucket, get, res):
    """Gamma's condition_ids takes the REPEATED-param form only (verified
    2026-09-03; the comma CSV silently matches nothing), at most 2 ids a call
    because a larger batch was never probed (rule 3)."""
    known = {r[0] for r in con.execute("SELECT condition_id FROM markets")}
    todo = sorted(c for c in seen if c and c not in known)
    prefixes = cfg["whale"]["slug_sport_prefixes"]
    for i in range(0, len(todo), GAMMA_IDS_PER_CALL):
        chunk = todo[i:i + GAMMA_IDS_PER_CALL]
        url = GAMMA + "/markets?" + "&".join(f"condition_ids={c}" for c in chunk)
        body = _call(bucket, get, url, cfg, res)
        found = {str(m.get("conditionId") or ""): m for m in body}
        rows = [_gamma_row(c, found.get(c), prefixes, now) for c in chunk]
        con.executemany(f"INSERT OR IGNORE INTO markets ({MARKET_COLS}) "
                        f"VALUES ({','.join('?' * 10)})", rows)
        con.commit()
        for r in rows:
            res["mapped" if r[1] and r[2] else "unmappable"] += 1
            if not (r[1] and r[2]):
                log.info("whale market %s unmappable (sport=%s type=%s) — never scored",
                         r[0], r[1], r[2])


def _gamma_row(cond, m, prefixes, now) -> tuple:
    sport = mtype = start = None
    tokens: list = []
    if m:
        mtype = SPORTS_MARKET_TYPE.get(str(m.get("sportsMarketType") or "").lower())
        sport = _sport_of(str(m.get("slug") or ""), prefixes)
        start = _iso(m.get("gameStartTime"))
        tokens = [[t, o] for t, o in zip(_json_list(m.get("clobTokenIds")),
                                         _json_list(m.get("outcomes")))]
    return (cond, sport, mtype, None, start, None, json.dumps(tokens),
            str(m.get("slug") or "") if m else None, now.isoformat(), now.isoformat())


def _sport_of(slug: str, prefixes) -> str | None:
    """Polymarket sports slugs lead with a league code: `kbo-lot-kt-2026-08-09`.
    Longest configured prefix wins, so one `cric` key covers crict20max,
    cricjcl, cricodc ... Unknown code -> None -> the market is never scored."""
    head = slug.split("-")[0].lower()
    for key in sorted(prefixes, key=len, reverse=True):
        if head == key or head.startswith(key):
            return prefixes[key]
    return None


def _json_list(value) -> list:
    """Gamma JSON-encodes clobTokenIds/outcomes as STRINGS, not arrays."""
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []
