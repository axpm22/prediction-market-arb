"""Measurement over the WHALE ledger: the settled-ROI clusters, the pregame
markouts, and the exit-vs-hold counterfactual (design 2026-09-03 §12).

Shared by the daily report's section (arb/report_whale.py) and the L14 bench
lane (arb/bench_whale.py) so the two can never drift into two different
"V2/flat ROI". Pure reads: a Store-like `.query` for the ledger, an optional
read-only tape handle for the facts only the collector has.

Every interval is ONE-SIDED (§12): "upper 90% bound" is the 90th percentile of
the cluster bootstrap of the mean, "lower 80% bound" its 20th. Clusters are
`event_key` (= `pm:<event_slug>` on every WHALE row).
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from arb import metrics, regrade
from arb.markout import HORIZONS
from arb.models import q6

# The two PREGAME horizons §12 names, straight off arb/markout.py's own tuple
# (30, 120, 600, 1800) — no second copy of the numbers to drift out of sync.
H_2M, H_10M = HORIZONS[1], HORIZONS[2]
UB90, LB80 = (0.10, 0.90), (0.20, 0.80)     # one-sided percentile pairs (§12)
SETTLED_SQL = """
    SELECT b.* FROM flat_bets b WHERE b.kind='WHALE' AND EXISTS
      (SELECT 1 FROM bet_events e WHERE e.bet_id=b.id AND e.kind='settled')
    ORDER BY b.id"""


def _d(v) -> Decimal:
    return Decimal(str(v or 0))


def cluster_ci(cfg, by_event: dict) -> dict:
    """Mean plus the one-sided bounds §12 reads: `lb80` (20th percentile) is the
    success test, `ub90` (90th) the kill test. Bootstrap internals are floats
    (rule 5's stated exception) and are re-quantized here. The resample itself
    is `arb.regrade._bootstrap_means` — the one cluster-bootstrap loop, shared
    rather than copied, so `cluster_ci` reads every percentile it needs off
    the same sorted draw `_bootstrap_mean_ci` would produce for this
    `by_event`/`seed`/`n_boot`."""
    seed, n_boot = int(cfg["regrade"]["bootstrap_seed"]), int(cfg["regrade"]["bootstrap_n"])
    all_vals = [v for vs in by_event.values() for v in vs]
    mean = statistics.mean(all_vals) if all_vals else 0.0
    means = regrade._bootstrap_means(by_event, seed, n_boot)

    def pct(p):
        return means[int(p * len(means))] if means else mean

    lo90, ub90, lb80, hi80 = pct(UB90[0]), pct(UB90[1]), pct(LB80[0]), pct(LB80[1])
    return {"n": sum(len(v) for v in by_event.values()), "n_events": len(by_event),
            "mean": q6(Decimal(mean)), "lo90": q6(Decimal(lo90)),
            "ub90": q6(Decimal(ub90)), "lb80": q6(Decimal(lb80)),
            "hi80": q6(Decimal(hi80))}


def roi_rows(store) -> list[dict]:
    """One row per settled WHALE bet: its variant, cluster, signal and ROI per
    dollar staked. An EXITED bet counts — the exit P&L is realized (§9)."""
    out = []
    for r in store.query(SETTLED_SQL):
        bet = dict(r)
        pnl = metrics.bet_pnl_optimistic(store, bet)
        stake = _d(bet["stake_usd"])
        if pnl is None or not stake:
            continue
        out.append({"variant": bet["variant"] or "-", "event_key": bet["event_key"],
                    "signal_id": bet["signal_id"], "roi": pnl / stake, "bet": bet})
    return out


def roi_cell(cfg, rows: list[dict]) -> dict:
    by_event: dict = {}
    for r in rows:
        by_event.setdefault(r["event_key"], []).append(float(r["roi"]))
    cell = cluster_ci(cfg, by_event)
    wins = sum(1 for r in rows if r["roi"] > 0)
    cell["win_rate"] = q6(Decimal(wins) / len(rows)) if rows else Decimal(0)
    return cell


def markout_stats(store) -> dict:
    """+2m and +10m PREGAME coverage and mean drift (D_h = mark mid - mid0 on
    the leg), plus the clusters the bench needs for the one-sided bound."""
    n_bets = store.query(
        "SELECT COUNT(*) c FROM flat_bets WHERE kind='WHALE'")[0]["c"]
    out = {h: {"n": 0, "coverage": Decimal(0), "mean_drift": None, "by_event": {}}
           for h in (H_2M, H_10M)}
    for r in store.query(
            """SELECT m.horizon_secs h, m.mark_cost, b.legs_json, b.event_key
               FROM markouts m JOIN flat_bets b ON b.id = m.bet_id
               WHERE b.kind='WHALE' AND m.in_play=0 AND m.leg_index=0
                 AND m.horizon_secs IN (?, ?)""", (H_2M, H_10M)):
        mid0 = (json.loads(r["legs_json"])[0] or {}).get("mid0")
        if mid0 is None:
            continue
        cell = out[r["h"]]
        cell["n"] += 1
        cell["by_event"].setdefault(r["event_key"], []).append(
            float(_d(r["mark_cost"]) - _d(mid0)))
    for cell in out.values():
        drifts = [v for vs in cell["by_event"].values() for v in vs]
        cell["coverage"] = q6(Decimal(cell["n"]) / n_bets) if n_bets else Decimal(0)
        if drifts:
            cell["mean_drift"] = q6(Decimal(sum(drifts) / len(drifts)))
    return out


def signals_by_id(con) -> dict:
    """Tape `signals` by id, with the fired side's whale list parsed once."""
    if con is None:
        return {}
    return {r["id"]: {"side": r["side"], "variant": r["variant"],
                      "fired": r["fired"], "condition_id": r["condition_id"],
                      "whales": json.loads(r["whale_ids_json"] or "[]")}
            for r in con.execute("SELECT * FROM signals")}


def is_native(sig: dict) -> bool | None:
    """§7: native iff EVERY contributing whale on the FIRED side qualified on
    its own bucket. A whale on the other side never makes a signal transfer,
    and neither does a sybil-dropped one — `points()` (arb/whale_signal.py)
    already gives it zero weight, so it never contributed to the fire."""
    on_side = [w for w in sig["whales"] if str(w.get("side")) == str(sig["side"])
               and not w.get("sybil_dropped")]
    return all(bool(w.get("native")) for w in on_side) if on_side else None


def exit_stats(store, con, signals: dict) -> dict:
    """Exits by reason with the exit-vs-hold counterfactual: hold = what the
    market's own resolution would have paid the same contracts (§12)."""
    resolutions = {r["condition_id"]: dict(r) for r in
                   con.execute("SELECT * FROM resolutions")} if con else {}
    out: dict = {}
    for r in store.query(SETTLED_SQL):
        bet = dict(r)
        payload = json.loads(store.query(
            "SELECT payload_json FROM bet_events WHERE bet_id=? AND kind='settled' "
            "ORDER BY id LIMIT 1", (bet["id"],))[0]["payload_json"])
        source = str(payload.get("source") or "")
        if not source.startswith("exit:"):
            continue
        cell = out.setdefault(source.split(":", 1)[1], {
            "n": 0, "exit_pnl": Decimal(0), "hold_pnl": Decimal(0), "n_hold": 0})
        cell["n"] += 1
        cell["exit_pnl"] = q6(cell["exit_pnl"] + _d(payload.get("pnl")))
        sig = signals.get(bet["signal_id"])
        res = resolutions.get(sig["condition_id"]) if sig else None
        if not sig or not res or res.get("status") not in ("resolved", "void"):
            continue
        if res["status"] == "void":
            hold = Decimal(0)          # ledger convention: VOID contributes 0
        else:
            stake = _d(bet["stake_usd"])
            contracts = stake / metrics.bet_effective_cost(bet)
            payout = Decimal(1) if res["winner_outcome_index"] == int(sig["side"]) \
                else Decimal(0)
            hold = payout * contracts - stake
        cell["hold_pnl"] = q6(cell["hold_pnl"] + hold)
        cell["n_hold"] += 1
    return out


# ------------------------------------------------------------- kill rules

FAST_KILL_KIND = "whale-fast-kill"      # one annotation per PRODUCED daily report
FAST_KILL_DAYS = 30                     # §12 rule 2's rolling window
FAST_KILL_STREAK = 2                    # "on two consecutive produced daily reports"


def _state(fired: bool, armed: bool) -> str:
    return "FIRED" if fired else ("armed" if armed else "not yet")


def _or(v, dash="—"):
    return dash if v is None else v


def _fast_kill_window(store, now) -> dict:
    """§12 rule 2's inputs over the rolling 30 days: mean pregame D_2m, mean
    `flagged_edge` of the same marked bets, and how many bets those are."""
    since = (now - timedelta(days=FAST_KILL_DAYS)).isoformat()
    per_bet: dict = {}
    for r in store.query(
            """SELECT b.id, b.legs_json, b.edge_points, m.mark_cost
               FROM markouts m JOIN flat_bets b ON b.id = m.bet_id
               WHERE b.kind='WHALE' AND m.in_play=0 AND m.leg_index=0
                 AND m.horizon_secs=? AND b.created_at >= ?""", (H_2M, since)):
        mid0 = (json.loads(r["legs_json"])[0] or {}).get("mid0")
        if mid0 is None:
            continue
        per_bet[r["id"]] = (_d(r["mark_cost"]) - _d(mid0), _d(r["edge_points"]))
    n = len(per_bet)
    if not n:
        return {"n_marked": 0, "mean_d2m": None, "mean_flagged_edge": None,
                "threshold": None}
    drift = q6(sum(v[0] for v in per_bet.values()) / n)
    edge = q6(sum(v[1] for v in per_bet.values()) / n)
    return {"n_marked": n, "mean_d2m": drift, "mean_flagged_edge": edge,
            "threshold": q6(Decimal("-0.5") * edge)}


def _held(fk: dict, min_marked: int) -> bool:
    """Rule 2's condition on ONE day's rolling window: enough marked bets, and
    mean pregame D_2m below -0.5 x mean flagged_edge."""
    return bool(fk["n_marked"] >= min_marked and fk["mean_d2m"] is not None
                and fk["mean_d2m"] < fk["threshold"])


def fast_kill_streak(store, before_day: str | None = None) -> int:
    """Consecutive produced daily reports on which rule 2's condition held,
    counting back from `before_day` (exclusive). Only produced reports have a
    row, so an outage day PAUSES the streak instead of resetting it (§12), and
    a re-run of a day cannot extend it: the caller excludes today and adds
    today's live verdict itself, so the count is the same however often a
    report is regenerated. Latest row per day wins."""
    streak, seen = 0, set()
    for r in store.query(
            "SELECT target_id day, detail FROM annotations WHERE kind=? "
            "ORDER BY target_id DESC, id DESC", (FAST_KILL_KIND,)):
        if r["day"] in seen or (before_day is not None and r["day"] >= before_day):
            continue
        seen.add(r["day"])
        if r["detail"] != "held":
            break
        streak += 1
    return streak


def record_fast_kill(cfg, store, day: str, now=None) -> bool | None:
    """Append this PRODUCED daily report's rule-2 verdict and return it.
    Append-only (rule 9): the streak is DERIVED from these rows, never stored
    as a counter. Re-running an OLD day records nothing — the window is
    today's, and dating it back would rewrite a streak out of data that day
    never saw."""
    now = now or datetime.now(timezone.utc)
    if day != now.astimezone(ZoneInfo(str(cfg["timezone"]))).date().isoformat():
        return None
    held = _held(_fast_kill_window(store, now),
                 int(cfg["whale"]["kill"]["fast_kill_min_marked"]))
    store.insert("annotations", {
        "created_at": now.isoformat(), "target_kind": "report-day",
        "target_id": day, "kind": FAST_KILL_KIND,
        "detail": "held" if held else "clear"})
    return held


def kill_status(cfg, store, con=None, now=None) -> dict:
    """§12's four kill rules, each `armed` / `not yet` / `FIRED` with the
    numbers behind it. Pure read; the daily whale section and the dashboard
    print the same block from this one computation."""
    w, now = cfg["whale"], now or datetime.now(timezone.utc)
    kill, primary = w["kill"], f"{w['primary_variant']}/flat"
    rows = [r for r in roi_rows(store) if r["variant"] == primary]
    cell = roi_cell(cfg, rows) if rows else None
    n = len({r["signal_id"] for r in rows})
    decision_n, null_n = int(kill["decision_n"]), int(kill["null_n"])
    ub90 = cell["ub90"] if cell else None
    lb80 = cell["lb80"] if cell else None

    fk = _fast_kill_window(store, now)
    min_marked = int(kill["fast_kill_min_marked"])
    held = _held(fk, min_marked)
    # today's row may or may not be written yet (the report appends it as it is
    # produced), so the streak is counted STRICTLY BEFORE today and today's live
    # verdict is added here — the same answer before and after a re-run.
    today = now.astimezone(ZoneInfo(str(cfg["timezone"]))).date().isoformat()
    streak = fast_kill_streak(store, before_day=today) + (1 if held else 0)

    week_ago = (now - timedelta(days=7)).isoformat()
    per_week = store.query(
        "SELECT COUNT(DISTINCT signal_id) c FROM flat_bets WHERE kind='WHALE' "
        "AND signal_id IS NOT NULL AND created_at >= ?", (week_ago,))[0]["c"]
    oldest = con.execute("SELECT MIN(observed_at) t FROM prints").fetchone()["t"] \
        if con is not None else None
    collecting = (now - datetime.fromisoformat(oldest)) if oldest else timedelta(0)
    starving_armed = collecting >= timedelta(weeks=4)
    alarm = int(kill["min_signals_per_week_alarm"])

    return {"as_of": now.isoformat(timespec="seconds"), "variant": primary,
            "rules": [
        {"rule": 1, "name": "decision", "n": n, "target_n": decision_n,
         "ub90": ub90, "mean": cell["mean"] if cell else None,
         "state": _state(n >= decision_n and ub90 is not None and ub90 < 0,
                         n >= decision_n),
         "says": f"n={n}/{decision_n} settled {primary} signals; "
                 f"upper 90% bound {_or(ub90)} (< 0 kills)"},
        {"rule": 2, "name": "fast kill", "streak": streak,
         "streak_target": FAST_KILL_STREAK, "held_today": held,
         "n_marked": fk["n_marked"], "min_marked": min_marked,
         "mean_d2m": fk["mean_d2m"], "threshold": fk["threshold"],
         "state": _state(streak >= FAST_KILL_STREAK, fk["n_marked"] >= min_marked),
         "says": f"mean D_2m {_or(fk['mean_d2m'])} vs {_or(fk['threshold'])} over "
                 f"{fk['n_marked']}/{min_marked} marked bets ({FAST_KILL_DAYS}d); "
                 f"streak {streak}/{FAST_KILL_STREAK} produced reports"},
        {"rule": 3, "name": "null", "n": n, "target_n": null_n, "lb80": lb80,
         "state": _state(n >= null_n and not (lb80 is not None and lb80 > 0),
                         n >= null_n),
         "says": f"n={n}/{null_n}; lower 80% bound {_or(lb80)} "
                 f"(not > 0 at n retires the lane)"},
        {"rule": 4, "name": "starvation", "fired_7d": per_week, "alarm": alarm,
         "collecting_days": collecting.days,
         "state": _state(starving_armed and per_week < alarm, starving_armed),
         "says": f"{per_week} fired signals in 7d vs {alarm}/week floor; "
                 f"{collecting.days}d of collection (armed at 28d)"},
    ]}
