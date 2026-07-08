"""The daily report — the product (SPEC §14.2). Recomputed idempotently from raw
rows via metrics.py; stored lanes/trusted are read, never re-derived."""
from __future__ import annotations

import csv
import json
import statistics
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from rich.console import Console
from rich.table import Table

from arb import metrics
from arb.capacity import capacity_frame
from arb.venues.fees import fee_is_stale

console = Console()


def headline(cfg, store) -> tuple[Decimal, int]:
    """THE number: settled P&L, trusted+sports+pregame+clean, optimistic basis,
    GROSS OF CARRY (§10.6). Matches scripts/headline.sql by construction."""
    rows = store.query(
        """SELECT fb.* FROM flat_bets fb
           WHERE fb.trusted = 1 AND fb.category = 'sports' AND fb.lane = 'clean'
             AND (fb.event_start_utc IS NULL OR fb.created_at < fb.event_start_utc)
             AND EXISTS (SELECT 1 FROM bet_events be
                         WHERE be.bet_id = fb.id AND be.kind = 'settled')""")
    total, n = Decimal(0), 0
    for r in rows:
        if metrics._annotated(store, r["id"]):
            continue  # invariant 5: annotated-excluded rows never enter a P&L cell
        pnl = metrics.bet_pnl_optimistic(store, dict(r))
        if pnl is not None:
            total += pnl
            n += 1
    return total, n


def _cadence(store, day: str) -> dict:
    rows = store.query(
        "SELECT started_at FROM polls WHERE kind='watch' AND date(started_at)=? "
        "ORDER BY started_at", (day,))
    times = [datetime.fromisoformat(r["started_at"]) for r in rows]
    gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:])]
    if not gaps:
        return {"n_polls": len(times), "mean": None, "p95": None}
    return {"n_polls": len(times), "mean": statistics.mean(gaps),
            "p95": sorted(gaps)[int(0.95 * (len(gaps) - 1))]}


def daily_frame(cfg, store, day: str) -> dict:
    bets = [dict(r) for r in store.query(
        "SELECT * FROM flat_bets WHERE date(created_at)=? ORDER BY id", (day,))]
    per_bet = []
    for b in bets:
        pnl = metrics.bet_pnl_optimistic(store, b)
        confirmed, pnl_conf = metrics.bet_confirmed(store, b)
        marks = [dict(m) for m in store.query(
            "SELECT * FROM markouts WHERE bet_id=?", (b["id"],))]
        excl = metrics._annotated(store, b["id"])
        per_bet.append({**b, "pnl": pnl, "confirmed": confirmed,
                        "pnl_if_confirmed": pnl_conf,
                        "pnl_markout": metrics.pnl_markout(cfg, b, marks)
                        if marks else None,
                        "excluded": ",".join(excl)})
    by_class: dict[tuple, dict] = {}
    for b in per_bet:
        key = (b["kind"], b["lane"])
        agg = by_class.setdefault(key, {"bets": 0, "staked": Decimal(0),
                                        "settled_pnl": Decimal(0), "pending": 0,
                                        "missed": 0})
        agg["bets"] += 1
        agg["staked"] += Decimal(b["stake_usd"])
        if b["excluded"]:
            continue
        if b["pnl"] is None:
            agg["pending"] += 1
        else:
            agg["settled_pnl"] += b["pnl"]
        if not b["confirmed"]:
            agg["missed"] += 1
    lot = store.query(
        """SELECT reason, COUNT(*) n, SUM(CAST(edge_points AS REAL)) edge
           FROM left_on_table lo JOIN polls p ON p.id = lo.poll_id
           WHERE date(p.started_at)=? GROUP BY reason ORDER BY n DESC""", (day,))
    venue_hours = store.query(
        """SELECT pv.venue, strftime('%H', p.started_at) hour,
                  AVG(pv.ok) ok_rate, COUNT(*) n
           FROM poll_venues pv JOIN polls p ON p.id = pv.poll_id
           WHERE date(p.started_at)=? GROUP BY pv.venue, hour""", (day,))
    newest = store.query("SELECT MAX(fetched_at) t FROM quotes")[0]["t"]
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(newest)
           ).total_seconds() if newest else None
    hl, hl_n = headline(cfg, store)
    return {
        "day": day, "per_bet": per_bet, "by_class": by_class,
        "left_on_table": [dict(r) for r in lot],
        "cadence": _cadence(store, day),
        "venue_hours": [dict(r) for r in venue_hours],
        "headline_pnl": hl, "headline_n": hl_n,
        "capacity": capacity_frame(cfg, store, day),
        "data_age_secs": age,
        "data_stale": age is not None and age > float(cfg["data_stale_alarm_secs"]),
        "stale_fees": [v for v in ("kalshi", "polymarket", "sx")
                       if fee_is_stale(cfg, v)],
    }


def _capacity_md(cap: dict) -> list[str]:
    """§13.4 parallel section — clearly labeled, never blended into headline."""
    md = ["## PROJECTIONS — sizing/capacity (§13.4, never headline)",
          f"- capacity: **{cap['status']}** — ladder capture "
          f"{cap['n_with_ladders']}/{cap['n_feasible']} feasible positions "
          f"({cap['ladder_fraction']:.0%})"]
    if cap["status"] == "OK" and cap["positions"]:
        md.append(f"- projected P&L at min(Kelly-cap, depth/5, q*) sizing, "
                  f"VWAP fills: ${cap['projected_pnl']} over "
                  f"{len(cap['positions'])} trusted clean positions")
        md.append("")
        md.append("| position | kind | contracts | stake | net edge | projected P&L |")
        md.append("|---|---|---|---|---|---|")
        for p in cap["positions"]:
            md.append(f"| {p['position_key']} | {p['kind']} | {p['contracts']} | "
                      f"${p['stake_usd']} | {p['net_edge']} | ${p['pnl_usd']} |")
    return md


def write_daily(cfg, store, day: str, outdir: str | Path = "reports/daily") -> dict:
    frame = daily_frame(cfg, store, day)
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    csv_path = outdir / f"{day}.csv"
    cols = ["id", "created_at", "kind", "lane", "trusted", "category", "sport",
            "venue_pair", "event_key", "stake_usd", "edge_points", "status",
            "pnl", "confirmed", "pnl_if_confirmed", "pnl_markout", "excluded", "notes"]
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for b in frame["per_bet"]:
            w.writerow(b)
    md = [f"# Daily report {day}", "",
          f"**Headline (cumulative, trusted+sports+pregame+clean, optimistic, "
          f"GROSS OF CARRY): ${frame['headline_pnl']} over {frame['headline_n']} "
          f"settled bets**", ""]
    c = frame["cadence"]
    md.append(f"- polls: {c['n_polls']}; cadence mean "
              f"{c['mean'] and round(c['mean']) or '—'}s, p95 "
              f"{c['p95'] and round(c['p95']) or '—'}s (planned "
              f"{cfg['watch_interval_secs']}s)")
    md.append(f"- newest quote age: "
              f"{frame['data_age_secs'] and round(frame['data_age_secs']) or '—'}s"
              + (" **STALE-DATA ALARM**" if frame["data_stale"] else ""))
    if frame["stale_fees"]:
        md.append(f"- **stale fee badge**: {', '.join(frame['stale_fees'])}")
    md.append("")
    md.append("| class | lane | bets | staked | settled P&L | pending | missed-confirm |")
    md.append("|---|---|---|---|---|---|---|")
    for (kind, lane), a in sorted(frame["by_class"].items()):
        md.append(f"| {kind} | {lane} | {a['bets']} | ${a['staked']} | "
                  f"${a['settled_pnl']} | {a['pending']} | {a['missed']} |")
    md.append("")
    md.extend(_capacity_md(frame["capacity"]))
    md.append("")
    md.append("## Left on the table")
    md.append("| reason | flags | summed edge |")
    md.append("|---|---|---|")
    for r in frame["left_on_table"]:
        md.append(f"| {r['reason']} | {r['n']} | {round(r['edge'] or 0, 4)} |")
    (outdir / f"{day}.md").write_text("\n".join(md) + "\n")
    return frame


def print_daily(cfg, store, day: str) -> None:
    frame = daily_frame(cfg, store, day)
    t = Table(title=f"Daily report {day} — headline ${frame['headline_pnl']} "
                    f"({frame['headline_n']} settled clean bets, gross of carry)")
    for col in ("class", "lane", "bets", "staked", "settled P&L", "pending", "missed"):
        t.add_column(col)
    if not frame["by_class"]:
        t.add_row("—", "—", "0", "$0", "$0", "0", "0")
    for (kind, lane), a in sorted(frame["by_class"].items()):
        t.add_row(kind, lane, str(a["bets"]), f"${a['staked']}",
                  f"${a['settled_pnl']}", str(a["pending"]), str(a["missed"]))
    console.print(t)
    c = frame["cadence"]
    console.print(f"[dim]polls={c['n_polls']} cadence mean={c['mean'] and round(c['mean'])}s "
                  f"p95={c['p95'] and round(c['p95'])}s; quote age="
                  f"{frame['data_age_secs'] and round(frame['data_age_secs'])}s"
                  f"{' STALE' if frame['data_stale'] else ''}[/]")
    cap = frame["capacity"]
    console.print(f"[dim]PROJECTION (§13.4, never headline) — capacity "
                  f"{cap['status']}, ladders {cap['n_with_ladders']}/"
                  f"{cap['n_feasible']}"
                  + (f", projected P&L ${cap['projected_pnl']} "
                     f"({len(cap['positions'])} positions)"
                     if cap["status"] == "OK" else "") + "[/]")
    if frame["left_on_table"]:
        lt = Table(title="Left on the table")
        for col in ("reason", "flags", "summed edge"):
            lt.add_column(col)
        for r in frame["left_on_table"]:
            lt.add_row(r["reason"], str(r["n"]), str(round(r["edge"] or 0, 4)))
        console.print(lt)
