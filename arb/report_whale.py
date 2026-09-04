"""The daily report's L14 whale-consensus section (design 2026-09-03 §12).

Read-only everywhere: the ledger through the caller's `Store`, the tape through
its own `mode=ro` handle. Nothing here polls, fetches a book, or reads a
resolution from the venue — the section describes what the collector and the
lane have already persisted, and a report that made network calls could not be
re-run over an old day.

Fires are counted as DISTINCT `flat_bets.signal_id`, never `signals.fired`: a
fire re-evaluates every cycle until its prints age out, so the tape holds about
five fired rows per booking.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from arb.models import q6
from arb.replay import open_ro
from arb.whale_kill import kill_status
from arb.whale_metrics import (exit_stats, is_native, markout_stats, roi_cell,
                               roi_rows, signals_by_id)


def open_tape_ro(cfg) -> sqlite3.Connection | None:
    """The tape, read-only. None when the collector has never run here, or when
    the file cannot be opened read-only (a WAL DB under an active writer) — the
    section then reports what the ledger alone knows and says the tape is out."""
    path = Path(str(cfg["whale"]["tape_db"]))
    if not path.exists():
        return None
    try:
        con = open_ro(path)
        con.execute("SELECT 1 FROM meta LIMIT 1")
        return con
    except sqlite3.Error:
        return None



def whale_frame(cfg, store, now: datetime | None = None, con=None,
                hashes_log_path=None) -> dict:
    """Everything §12 asks the daily report to print about L14. `con` is a tape
    handle for tests and callers that already hold one; otherwise the tape is
    opened read-only here and closed before returning — on the error path too.
    `hashes_log_path` is the report chain rule 2's streak is derived from
    (default: the configured `report_dir`; `write_daily` passes its own outdir,
    so a report written elsewhere counts the days written there)."""
    now = now or datetime.now(timezone.utc)
    opened = con is None and (con := open_tape_ro(cfg)) is not None
    try:
        fire = cfg["whale"]["fire"]
        day_ago, week_ago = now - timedelta(days=1), now - timedelta(days=7)
        signals = signals_by_id(con)
        booked = {r["signal_id"] for r in store.query(
            "SELECT DISTINCT signal_id FROM flat_bets WHERE kind='WHALE' "
            "AND signal_id IS NOT NULL")}
        rows = roi_rows(store)
        qualifying = _qualifying(cfg, con, now)
        native_of = {sid: is_native(sig) for sid, sig in signals.items()}
        classed = {"native": [], "transfer": []}
        for r in rows:
            cls = native_of.get(r["signal_id"])
            if cls is not None:
                classed["native" if cls else "transfer"].append(r)
        counts = _signal_counts(store, con, day_ago, week_ago)
        return {
            "as_of": now.isoformat(timespec="seconds"),
            "tape_ok": con is not None,
            "primary_variant": f"{cfg['whale']['primary_variant']}/flat",
            "wallets": {**_wallets(con),
                        "hygiene_excluded": qualifying.pop("hygiene_excluded", 0)},
            "qualifying": qualifying,
            "signals": counts,
            "bets": {r["variant"] or "-": r["c"] for r in store.query(
                "SELECT variant, COUNT(*) c FROM flat_bets WHERE kind='WHALE' "
                "GROUP BY variant ORDER BY variant")},
            "roi": {v: roi_cell(cfg, [r for r in rows if r["variant"] == v])
                    for v in sorted({r["variant"] for r in rows})},
            "markouts": markout_stats(store),
            "exits": exit_stats(store, con, signals),
            "base_rates": [dict(r) for r in con.execute(
                "SELECT root, as_of, value, n FROM base_rates b WHERE as_of = "
                "(SELECT MAX(as_of) FROM base_rates x WHERE x.root=b.root) "
                "ORDER BY root")] if con else [],
            "native": {
                "signals": {k: sum(1 for sid, cls in native_of.items()
                                   if sid in booked and cls is (k == "native"))
                            for k in ("native", "transfer")},
                "roi": {k: roi_cell(cfg, v) for k, v in classed.items() if v}},
            "collector": _collector(con, now),
            "calibration": {
                "min_points": dict(fire["min_points"]),
                "signal_days": con.execute(
                    "SELECT COUNT(DISTINCT substr(evaluated_at, 1, 10)) d FROM signals"
                ).fetchone()["d"] if con else 0,
                "calibration_days": int(fire["calibration_days"])},
            # rule 4's 7d count is `counts` own — computed once, printed twice
            "kill": {"decision_n": int(cfg["whale"]["kill"]["decision_n"]),
                     "null_n": int(cfg["whale"]["kill"]["null_n"]),
                     "min_signals_per_week_alarm":
                         int(cfg["whale"]["kill"]["min_signals_per_week_alarm"]),
                     **kill_status(cfg, store, con, now, hashes_log_path,
                                   counts["fired_7d"])},
        }
    finally:
        if opened:
            con.close()


def _wallets(con) -> dict:
    if con is None:
        return {"tracked": 0, "backfilled": 0}
    row = con.execute("SELECT COUNT(*) n, COUNT(backfilled_at) done "
                      "FROM wallets").fetchone()
    return {"tracked": row["n"], "backfilled": row["done"]}


def _qualifying(cfg, con, now) -> dict:
    """The lane's own roster, scored by the lane's own assembly (whale_state),
    never a second implementation of it."""
    empty = {"by_root": {}, "by_sport": {}, "top": [], "n": 0}
    if con is None:
        return empty
    from arb.whale_state import qualifying_whales, score_state
    state = score_state(cfg, con, now)
    whales = qualifying_whales(cfg, state)
    return {"by_root": dict(Counter(w["root"] for w in whales)),
            "by_sport": dict(Counter(w["sport"] for w in whales)),
            "top": whales[:20], "n": len(whales),
            "hygiene_excluded": state.hygiene_excluded}


def _signal_counts(store, con, day_ago, week_ago) -> dict:
    """Evaluations come from the tape; FIRES come from the ledger, as distinct
    `signal_id` values — `signals.fired` counts re-evaluations, not bookings."""
    def fires(since):
        return store.query(
            "SELECT COUNT(DISTINCT signal_id) c FROM flat_bets WHERE kind='WHALE' "
            "AND signal_id IS NOT NULL AND created_at >= ?", (since.isoformat(),)
        )[0]["c"]

    recent = [dict(r) for r in con.execute(
        "SELECT variant, fired, suppressed_reason FROM signals WHERE evaluated_at >= ?",
        (day_ago.isoformat(),))] if con else []
    return {
        "evaluated_24h": len(recent),
        "by_variant": dict(Counter(r["variant"] for r in recent)),
        "suppressed": Counter((r["suppressed_reason"], r["variant"])
                              for r in recent if not r["fired"]),
        "fired_24h": fires(day_ago), "fired_7d": fires(week_ago)}


def _collector(con, now) -> dict:
    """Is the tape being written at all? Status, rows and bad_rows are the
    lane's own last cycle (tape `meta`); the gap is §5 step 2's recorded
    paging gap; prints/min is measured over the last hour."""
    out = {"status": None, "rows": None, "bad_rows": None, "at": None,
           "last_seen_age_secs": None, "last_gap": None, "backoff_until": None,
           "prints_per_min": Decimal(0)}
    if con is None:
        return out
    meta = {r["key"]: r["value"] for r in con.execute("SELECT key, value FROM meta")}
    last = json.loads(meta.get("last_cycle") or "{}")
    out.update(status=last.get("status"), rows=last.get("prints"),
               bad_rows=last.get("bad_rows"), at=last.get("at"),
               backoff_until=meta.get("backoff_until"))
    if meta.get("last_seen_ts"):
        out["last_seen_age_secs"] = int(now.timestamp()) - int(meta["last_seen_ts"])
    if meta.get("last_gap"):
        out["last_gap"] = json.loads(meta["last_gap"])
    n = con.execute("SELECT COUNT(*) c FROM prints WHERE ts >= ?",
                    (int((now - timedelta(hours=1)).timestamp()),)).fetchone()["c"]
    out["prints_per_min"] = q6(Decimal(n) / 60)
    return out


# ------------------------------------------------------------------ render

def _n(v, dash="—") -> str:
    return dash if v is None else str(v)


def whale_md(frame: dict) -> list[str]:
    """The section, as markdown. Cumulative lane-to-date except where a line
    names its own window — the class table above it is the day's."""
    w, s, c = frame["wallets"], frame["signals"], frame["collector"]
    cal, q = frame["calibration"], frame["qualifying"]
    md = ["## L14 whale-consensus lane (REVIEW lane, never headline)",
          f"- as of {frame['as_of']}"
          + ("" if frame["tape_ok"] else "  — **whale tape unavailable**: "
                                         "collector never ran here, or the file "
                                         "could not be opened read-only"),
          f"- wallets: {w['tracked']} tracked, {w['backfilled']} backfilled, "
          f"{w['hygiene_excluded']} positions hygiene-excluded",
          f"- whales scored >= lb80 (ungated roster): {q['n']} "
          + (", ".join(f"{k} {v}" for k, v in sorted(q["by_root"].items())) or "—")
          + " | by sport: "
          + (", ".join(f"{k} {v}" for k, v in sorted(q["by_sport"].items())) or "—"),
          f"- signals 24h: {s['evaluated_24h']} evaluated, {s['fired_24h']} fired "
          f"(distinct signal_id booked); 7d fired {s['fired_7d']} "
          f"(starvation alarm below {frame['kill']['min_signals_per_week_alarm']}/week)",
          "- suppressed 24h: " + (", ".join(
              f"{reason or '—'}/{variant} {n}"
              for (reason, variant), n in sorted(s["suppressed"].items(),
                                                 key=lambda kv: -kv[1])) or "none"),
          f"- calibration: min_points "
          + " ".join(f"{k}={_n(v, 'null')}" for k, v in sorted(cal["min_points"].items()))
          + f"; {cal['signal_days']} of {cal['calibration_days']} signal days",
          "- base rate: " + (", ".join(
              f"{r['root']} {r['value']} (n={r['n']})" for r in frame["base_rates"])
              or "—"),
          f"- collector: last cycle {_n(c['status'])} rows={_n(c['rows'])} "
          f"bad_rows={_n(c['bad_rows'])} at {_n(c['at'])}; newest print "
          f"{_n(c['last_seen_age_secs'])}s ago; {c['prints_per_min']} prints/min (1h)"
          + (f"; **gap** {c['last_gap'].get('secs')}s at {c['last_gap'].get('at')}"
             if c["last_gap"] else "; no recorded gap")
          + (f"; **backing off until {c['backoff_until']}**"
             if c["backoff_until"] else ""),
          "",
          "| kill rule (§12) | state | numbers |", "|---|---|---|"]
    for r in frame["kill"]["rules"]:
        md.append(f"| {r['rule']} {r['name']} | "
                  + (f"**{r['state']}**" if r["state"] == "FIRED" else r["state"])
                  + f" | {r['says']} |")
    md += ["",
           f"Primary: mean settled ROI per $, {frame['primary_variant']}, clustered "
           f"by event_key. Bounds are ONE-SIDED (§12): lb80 = 20th percentile, "
           f"ub90 = 90th. Decision at n >= {frame['kill']['decision_n']} signals; "
           f"upper 90% bound < 0 kills the lane.",
           "",
           "| variant | bets | settled | events | ROI/$ | lb80 | ub90 | win rate |",
           "|---|---|---|---|---|---|---|---|"]
    for variant in sorted(set(frame["bets"]) | set(frame["roi"])):
        r = frame["roi"].get(variant)
        star = " **(primary)**" if variant == frame["primary_variant"] else ""
        md.append(f"| {variant}{star} | {frame['bets'].get(variant, 0)} | "
                  + (f"{r['n']} | {r['n_events']} | {r['mean']} | {r['lb80']} | "
                     f"{r['ub90']} | {r['win_rate']} |" if r else "0 | 0 | — | — | — | — |"))
    if not frame["bets"] and not frame["roi"]:
        md.append("| — | 0 | 0 | 0 | — | — | — | — |")
    md += ["", "| markout (pregame) | coverage | marks | mean drift |", "|---|---|---|---|"]
    for h, cell in sorted(frame["markouts"].items()):
        md.append(f"| +{h // 60}m | {cell['coverage']} | {cell['n']} | "
                  f"{_n(cell['mean_drift'])} |")
    md += ["", "| exit reason | exits | exit P&L | hold P&L (counterfactual) | resolved |",
           "|---|---|---|---|---|"]
    for reason, cell in sorted(frame["exits"].items()):
        md.append(f"| {reason} | {cell['n']} | ${cell['exit_pnl']} | "
                  f"${cell['hold_pnl']} | {cell['n_hold']} |")
    if not frame["exits"]:
        md.append("| — | 0 | $0 | $0 | 0 |")
    md += ["", "| whales on the fired side | signals booked | ROI/$ | lb80 | ub90 |",
           "|---|---|---|---|---|"]
    for k in ("native", "transfer"):
        r = frame["native"]["roi"].get(k)
        md.append(f"| {k} | {frame['native']['signals'][k]} | "
                  + (f"{r['mean']} | {r['lb80']} | {r['ub90']} |" if r else "— | — | — |"))
    if q["top"]:
        md += ["", "| whale | bucket | lb80 | n_bucket | evidence |", "|---|---|---|---|---|"]
        for row in q["top"]:
            md.append(f"| {row['wallet']} | {row['bucket']} | {row['lb']} | "
                      f"{row['n_bucket']} | "
                      f"{'native' if row['native'] else 'transfer'} |")
    return md


def whale_line(frame: dict) -> str:
    """One console line for `arb report --daily` (the md carries the section)."""
    primary = frame["roi"].get(frame["primary_variant"])
    roi = (f"{primary['mean']} [lb80 {primary['lb80']}, ub90 {primary['ub90']}] "
           f"n={primary['n']}/{primary['n_events']} events" if primary else "no sample")
    return (f"L14 whale: {frame['signals']['evaluated_24h']} signals/24h, "
            f"{frame['signals']['fired_24h']} fired; {frame['primary_variant']} "
            f"ROI/$ {roi}; collector {_n(frame['collector']['status'])}"
            + ("" if frame["tape_ok"] else "; TAPE UNAVAILABLE"))
