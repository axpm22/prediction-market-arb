"""Dashboard queries (Task 12): the P&L curves, the open-position marks and the
lane verdicts the panel serves as JSON.

Split out of arb/dashboard.py on size alone — same rules. READS ONLY: every
number comes from arb/metrics.py or the query `arb report` already runs, the
lane and variant are the STORED ones (rule 12 — nothing here re-decides
eligibility), and money stays Decimal to the JSON boundary, where it is
serialised as a string and formatted by the page (rule 5).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from arb import metrics, report
from arb.models import q6
from arb.venues.fees import leg_fee_cost

# §9, ONE definition (arb/dashboard.py's open-positions tile selects its own
# columns with the same clause): open means no TERMINAL event. `lifetime_closed`
# counts — a position we stopped tracking is not a position we still hold.
OPEN_WHERE = """NOT EXISTS (SELECT 1 FROM bet_events be WHERE be.bet_id = fb.id
                       AND be.kind IN ('settled','void','lifetime_closed'))"""
OPEN_SQL = f"""
    SELECT fb.* FROM flat_bets fb WHERE {OPEN_WHERE}
     ORDER BY fb.event_start_utc IS NULL, fb.event_start_utc, fb.created_at"""
BASES = ("optimistic", "confirmed", "markout")
CHUNK = 400                                  # SQLite's parameter limit is 999


def _tz(cfg) -> ZoneInfo:
    return ZoneInfo(str(cfg["timezone"]))


def _day(cfg, iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(_tz(cfg)).date().isoformat()


def _chunks(ids: list) -> list[list]:
    return [ids[i:i + CHUNK] for i in range(0, len(ids), CHUNK)]


def _marks_by_bet(store, ids: list[int]) -> dict[int, list]:
    out: dict[int, list] = {}
    for chunk in _chunks(ids):
        for r in store.query(
                f"SELECT * FROM markouts WHERE bet_id IN ({','.join('?' * len(chunk))})",
                chunk):
            out.setdefault(r["bet_id"], []).append(dict(r))
    return out


def pnl_series(cfg, store, days: int, now=None) -> dict:
    """Cumulative settled P&L per (kind, lane, variant) over the last `days`
    config-timezone days, plus the three bases side by side for the headline
    class. The settlement DAY is the terminal event's own date — a bet booked in
    March and settled today moves today's curve, which is what a P&L curve
    means. Values are aligned to `days`, so the page draws a polyline without
    knowing any dates."""
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(_tz(cfg)).date()
    day_list = [(today - timedelta(days=i)).isoformat()
                for i in range(days - 1, -1, -1)]
    since = metrics.day_bounds(cfg, day_list[0])[0]
    settled_at = {r["bet_id"]: r["at"] for r in store.query(
        "SELECT bet_id, MIN(created_at) at FROM bet_events "
        "WHERE kind IN ('settled','void') AND created_at >= ? GROUP BY bet_id",
        (since,))}
    ids = sorted(settled_at)
    # `headline_class` is report.headline's own WHERE, evaluated by SQLite on
    # the row — one predicate, never a second Python copy of it (rule 12)
    bets = [dict(r) for chunk in _chunks(ids) for r in store.query(
        f"""SELECT fb.*, ({report.HEADLINE_WHERE}) headline_class FROM flat_bets fb
             WHERE fb.id IN ({','.join('?' * len(chunk))})""", chunk)]
    marks = _marks_by_bet(store, ids)

    per_class: dict[tuple, dict] = {}
    per_basis: dict[str, dict] = {b: {} for b in BASES}
    for bet in bets:
        if metrics._annotated(store, bet["id"]):
            continue          # invariant 5: annotated rows never enter a P&L cell
        pnl = metrics.bet_pnl_optimistic(store, bet)
        if pnl is None:
            continue
        day = _day(cfg, settled_at[bet["id"]])
        key = (bet["kind"], bet["lane"], bet["variant"] or "")
        cell = per_class.setdefault(key, {})
        cell[day] = cell.get(day, Decimal(0)) + pnl
        if not bet["headline_class"]:
            continue
        confirmed, pnl_conf = metrics.bet_confirmed(store, bet)
        mk = metrics.pnl_markout(cfg, bet, marks.get(bet["id"], []))
        for basis, value in (("optimistic", pnl),
                             # a missed confirm is $0, never dropped (bug I8)
                             ("confirmed", pnl_conf if confirmed and pnl_conf
                              is not None else Decimal(0)),
                             ("markout", mk if mk is not None else pnl)):
            per_basis[basis][day] = per_basis[basis].get(day, Decimal(0)) + value

    def cumulative(by_day: dict) -> list[str]:
        total, out = Decimal(0), []
        for day in day_list:
            total += by_day.get(day, Decimal(0))
            out.append(str(total))
        return out

    return {
        "days": day_list, "generated_at": now.isoformat(timespec="seconds"),
        "series": [{"kind": k, "lane": lane, "variant": v,
                    "values": cumulative(by_day)}
                   for (k, lane, v), by_day in sorted(per_class.items())],
        "bases": [{"basis": b, "values": cumulative(per_basis[b])} for b in BASES],
        "headline_class": "trusted + sports + pregame + clean",
    }


def _leg_bid(store, bet: dict, leg: dict) -> Decimal | None:
    """The latest persisted bid for this leg's own key tuple (rule 7 keeps every
    quote; nothing is fetched here). None when nothing quotes it — a position we
    cannot mark says so instead of showing a made-up number."""
    rows = store.query(
        """SELECT bid FROM quotes WHERE event_key = ? AND venue = ?
             AND IFNULL(market_type,'') = IFNULL(?,'')
             AND IFNULL(line,'') = IFNULL(?,'')
             AND outcome = ? AND side = ? AND bid IS NOT NULL
           ORDER BY fetched_at DESC LIMIT 1""",
        (bet["event_key"], leg["venue"], leg.get("market_type"), leg.get("line"),
         leg.get("outcome"), leg.get("side")))
    return Decimal(rows[0]["bid"]) if rows else None


def positions(cfg, store, now=None, con=None) -> list[dict]:
    """Every open bet, marked at the latest bid minus the taker fee that selling
    into it would cost — the same basis the L14 exit prices at (§9). WHALE rows
    also carry their signal's whales and their LIVE exit-rule status."""
    now = now or datetime.now(timezone.utc)
    bets = [dict(r) for r in store.query(OPEN_SQL)]
    whale = {}
    if any(b["kind"] == "WHALE" for b in bets):
        from arb import dashboard_whale
        whale = dashboard_whale.whale_positions(cfg, store, bets, now, con=con)
    out = []
    for bet in bets:
        legs = json.loads(bet["legs_json"])
        stake = Decimal(str(bet["stake_usd"]))
        cost = metrics.bet_effective_cost(bet)
        bids = [_leg_bid(store, bet, leg) for leg in legs]
        proceeds = None
        if all(b is not None for b in bids) and cost > 0:
            proceeds = sum(
                b - leg_fee_cost(cfg, leg["venue"], bet["category"], b)
                for b, leg in zip(bids, legs))
        out.append({
            "id": bet["id"], "kind": bet["kind"], "lane": bet["lane"],
            "variant": bet["variant"] or "", "sport": bet["sport"],
            "event_key": bet["event_key"], "venue_pair": bet["venue_pair"],
            "fingerprint": bet["fingerprint"], "status": bet["status"],
            # one leg per outcome; a multi-leg basket shows them joined
            "outcome": " + ".join(str(leg.get("outcome")) for leg in legs),
            "side": " + ".join(str(leg.get("side")) for leg in legs),
            "entry_cost": str(cost), "stake_usd": str(stake),
            "edge_points": bet["edge_points"], "signal_id": bet["signal_id"],
            "created_at": bet["created_at"],
            "event_start_utc": bet["event_start_utc"],
            "age_secs": int((now - datetime.fromisoformat(
                bet["created_at"])).total_seconds()),
            "bid": None if proceeds is None else str(sum(bids)),
            "unrealized_pnl": None if proceeds is None
            else str(q6(proceeds * (stake / cost) - stake)),
            "whale": whale.get(bet["id"]),
        })
    return out


def lane_verdicts(cfg, store, now=None) -> list[dict]:
    """The latest `bench_results` row per lane (schema v8), L1 first. A row past
    `dashboard.bench_stale_days` is flagged stale rather than hidden: an old
    verdict is still the answer, it is just an old answer."""
    now = now or datetime.now(timezone.utc)
    stale_after = timedelta(days=int(cfg["dashboard"]["bench_stale_days"]))
    rows = []
    for r in store.query(
            "SELECT * FROM bench_results b WHERE b.id = "
            "(SELECT MAX(x.id) FROM bench_results x WHERE x.lane = b.lane)"):
        row = dict(r)
        row["payload"] = json.loads(row.pop("payload_json") or "{}")
        row["stale"] = (now - datetime.fromisoformat(row["ran_at"])) > stale_after
        rows.append(row)
    return sorted(rows, key=lambda r: (len(r["lane"]), r["lane"]))


def jsonable(obj):
    """Decimals as strings (rule 5 — the page formats them), and the tuple keys
    the whale frame's Counters carry, which json.dumps cannot encode."""
    if isinstance(obj, dict):
        return {("/".join(map(str, k)) if isinstance(k, tuple) else str(k)):
                jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, Decimal):
        return str(obj)
    return obj
