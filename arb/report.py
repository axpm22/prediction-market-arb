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

from arb import metrics, report_whale
from arb.capacity import capacity_frame
from arb.venues.fees import fee_is_stale
from arb.whale_kill import HASHES_LOG

console = Console()
# THE headline population, once (rule 12): dashboard_data selects this same
# expression in SQLite rather than re-deriving it in Python.
HEADLINE_WHERE = ("fb.trusted = 1 AND fb.category = 'sports' AND fb.lane = 'clean' AND "
                  "(fb.event_start_utc IS NULL OR fb.created_at < fb.event_start_utc)")


def headline(cfg, store) -> tuple[Decimal, int]:
    """THE number: settled P&L, trusted+sports+pregame+clean, optimistic basis,
    GROSS OF CARRY (§10.6). Matches scripts/headline.sql by construction."""
    rows = store.query(
        f"""SELECT fb.* FROM flat_bets fb WHERE {HEADLINE_WHERE}
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


def _cadence(cfg, store, day: str) -> dict:
    start, end = metrics.day_bounds(cfg, day)
    rows = store.query(
        "SELECT started_at FROM polls WHERE kind='watch' AND started_at >= ? "
        "AND started_at < ? ORDER BY started_at", (start, end))
    times = [datetime.fromisoformat(r["started_at"]) for r in rows]
    gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:])]
    return {"n_polls": len(times), "mean": statistics.mean(gaps) if gaps else None,
            "p95": sorted(gaps)[int(0.95 * (len(gaps) - 1))] if gaps else None}


def _live_section(cfg, store, day: str) -> dict:
    """LIVE lane (PROMPT_LIVE_LEADLAG A5): always visible, never blended.
    Populations per venue-pair, never pooled — like their pregame counterparts."""
    start, end = metrics.day_bounds(cfg, day)
    pairs = {}
    for b in store.query(
            "SELECT * FROM flat_bets WHERE poll_kind='live' AND created_at >= ? "
            "AND created_at < ?", (start, end)):
        p = pairs.setdefault(b["venue_pair"], {
            "flags": 0, "confirmed": 0, "locked_pnl": Decimal(0),
            "confirmed_pnl": Decimal(0), "lifetimes": []})
        p["flags"] += 1
        if b["pnl_optimistic"] is not None:
            p["locked_pnl"] += Decimal(b["pnl_optimistic"])
        ok, pnl_conf = metrics.bet_confirmed(store, dict(b))
        if ok:
            p["confirmed"] += 1
            if pnl_conf is not None:
                p["confirmed_pnl"] += pnl_conf
        closed = store.query(
            "SELECT created_at FROM bet_events WHERE bet_id=? AND "
            "kind='lifetime_closed'", (b["id"],))
        if closed:
            p["lifetimes"].append(
                (datetime.fromisoformat(closed[0]["created_at"])
                 - datetime.fromisoformat(b["created_at"])).total_seconds())
    for p in pairs.values():
        p["confirm_rate"] = p["confirmed"] / p["flags"] if p["flags"] else None
        p["median_lifetime_secs"] = statistics.median(p["lifetimes"]) if p["lifetimes"] else None
        del p["lifetimes"]
    cad_rows = store.query(
        "SELECT started_at FROM polls WHERE kind='live' AND started_at >= ? "
        "AND started_at < ? ORDER BY started_at", (start, end))
    times = [datetime.fromisoformat(r["started_at"]) for r in cad_rows]
    gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:])]
    cov = [json.loads(r["detail"]).get("coverage") for r in store.query(
        "SELECT detail FROM annotations WHERE kind='live-coverage' AND "
        "created_at >= ? AND created_at < ?", (start, end))]
    cov = [c for c in cov if c is not None]
    return {"pairs": {k: {kk: (str(vv) if isinstance(vv, Decimal) else vv)
                          for kk, vv in v.items()} for k, v in pairs.items()},
            "n_polls": len(times),
            "cadence_mean": statistics.mean(gaps) if gaps else None,
            "coverage_mean": statistics.mean(cov) if cov else None}


def daily_frame(cfg, store, day: str) -> dict:
    start, end = metrics.day_bounds(cfg, day)
    bets = [dict(r) for r in store.query(
        "SELECT * FROM flat_bets WHERE created_at >= ? AND created_at < ? "
        "ORDER BY id", (start, end))]
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
        key = (b["kind"], b["lane"], b["variant"] or "")
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
           WHERE p.started_at >= ? AND p.started_at < ?
           GROUP BY reason ORDER BY n DESC""", (start, end))
    venue_hours = store.query(
        """SELECT pv.venue, strftime('%H', p.started_at) hour,
                  AVG(pv.ok) ok_rate, COUNT(*) n
           FROM poll_venues pv JOIN polls p ON p.id = pv.poll_id
           WHERE p.started_at >= ? AND p.started_at < ?
           GROUP BY pv.venue, hour""", (start, end))
    newest = store.query("SELECT MAX(fetched_at) t FROM quotes")[0]["t"]
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(newest)
           ).total_seconds() if newest else None
    hl, hl_n = headline(cfg, store)
    demoted = [r["pair"] for r in store.query(
        "SELECT DISTINCT pair FROM pair_demotions ORDER BY pair")]
    return {
        "day": day, "per_bet": per_bet, "by_class": by_class,
        "demoted_pairs": demoted,
        "left_on_table": [dict(r) for r in lot],
        "cadence": _cadence(cfg, store, day),
        "venue_hours": [dict(r) for r in venue_hours],
        "headline_pnl": hl, "headline_n": hl_n,
        "capacity": capacity_frame(cfg, store, day),
        "live_lane": _live_section(cfg, store, day),
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


def write_daily(cfg, store, day: str, outdir: str | Path | None = None,
                whale_frame: dict | None = None) -> dict:
    frame = daily_frame(cfg, store, day)
    outdir = Path(outdir if outdir is not None else cfg["report_dir"])
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
    md.append("| class | lane | variant | bets | staked | settled P&L | pending "
              "| missed-confirm |")
    md.append("|---|---|---|---|---|---|---|---|")
    for (kind, lane, variant), a in sorted(frame["by_class"].items()):
        md.append(f"| {kind} | {lane} | {variant} | {a['bets']} | ${a['staked']} | "
                  f"${a['settled_pnl']} | {a['pending']} | {a['missed']} |")
    md.append("")
    # §12 kill rule 2 counts consecutive PRODUCED daily reports; the record of
    # which days those are is the HASHES chain below, read back (never written).
    frame["whale"] = whale_frame if whale_frame is not None else \
        report_whale.whale_frame(cfg, store, hashes_log_path=outdir / HASHES_LOG,
                                 producing_day=day)
    md.extend(report_whale.whale_md(frame["whale"]))
    md.append("")
    md.extend(_capacity_md(frame["capacity"]))
    md.append("")
    live = frame["live_lane"]
    md.append("## LIVE lane (in-play, review-lane forever — never headline)")
    cov = live["coverage_mean"]
    md.append(f"- live polls: {live['n_polls']}; cadence mean "
              f"{live['cadence_mean'] and round(live['cadence_mean']) or '—'}s "
              f"(target {cfg['live_lane']['cadence_secs']}s); shortlist coverage "
              f"{f'{cov:.0%}' if cov is not None else '—'}")
    md.append("| venue pair | flags | confirm rate | median lifetime | locked P&L | confirmed P&L |")
    md.append("|---|---|---|---|---|---|")
    for pair, p in sorted(live["pairs"].items()):
        cr = f"{p['confirm_rate']:.0%}" if p["confirm_rate"] is not None else "—"
        lt = f"{round(p['median_lifetime_secs'])}s" if p["median_lifetime_secs"] else "—"
        md.append(f"| {pair} | {p['flags']} | {cr} | {lt} | ${p['locked_pnl']} | "
                  f"${p['confirmed_pnl']} |")
    if not live["pairs"]:
        md.append("| — | 0 | — | — | $0 | $0 |")
    md.append("")
    md.append("## Left on the table")
    md.append("| reason | flags | summed edge |")
    md.append("|---|---|---|")
    for r in frame["left_on_table"]:
        md.append(f"| {r['reason']} | {r['n']} | {round(r['edge'] or 0, 4)} |")
    (outdir / f"{day}.md").write_text("\n".join(md) + "\n")
    # append-only hash chain: cheap tamper-evidence for the paper track record
    # (writeup verifiability; recomputing a day must reproduce its hash)
    import hashlib
    with (outdir / HASHES_LOG).open("a") as f:
        f.write(f"{day} md:{hashlib.sha256((outdir / f'{day}.md').read_bytes()).hexdigest()} "
                f"csv:{hashlib.sha256(csv_path.read_bytes()).hexdigest()}\n")
    return frame


def print_daily(cfg, store, day: str, whale_frame: dict | None = None) -> None:
    frame = daily_frame(cfg, store, day)
    t = Table(title=f"Daily report {day} — headline ${frame['headline_pnl']} "
                    f"({frame['headline_n']} settled clean bets, gross of carry)")
    for col in ("class", "lane", "variant", "bets", "staked", "settled P&L",
                "pending", "missed"):
        t.add_column(col)
    if not frame["by_class"]:
        t.add_row("—", "—", "", "0", "$0", "$0", "0", "0")
    for (kind, lane, variant), a in sorted(frame["by_class"].items()):
        t.add_row(kind, lane, variant, str(a["bets"]), f"${a['staked']}",
                  f"${a['settled_pnl']}", str(a["pending"]), str(a["missed"]))
    console.print(t)
    wf = whale_frame if whale_frame is not None else report_whale.whale_frame(cfg, store)
    console.print("[dim]" + report_whale.whale_line(wf) + "[/]")
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
    if frame.get("demoted_pairs"):
        console.print("[red]demoted pairs (settlement mismatch, §11.2): "
                      + ", ".join(frame["demoted_pairs"]) + "[/]")
    if frame["left_on_table"]:
        lt = Table(title="Left on the table")
        for col in ("reason", "flags", "summed edge"):
            lt.add_column(col)
        for r in frame["left_on_table"]:
            lt.add_row(r["reason"], str(r["n"]), str(round(r["edge"] or 0, 4)))
        console.print(lt)
