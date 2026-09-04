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
from decimal import Decimal

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
