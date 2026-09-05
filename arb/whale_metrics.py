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

# The lane's ROOT is the bet's own `category` (§8, amended 2026-09-04) and
# `sports` is the PRIMARY one: the registration keeps the primary metric on the
# sports figure, each non-sports root accumulates its own n against the same
# kill rules, and "rows from different roots are never pooled". Every table
# below is therefore keyed by root before it is keyed by anything else.
SPORTS_ROOT = "sports"
SETTLED_SQL = """
    SELECT b.* FROM flat_bets b WHERE b.kind='WHALE' AND EXISTS
      (SELECT 1 FROM bet_events e WHERE e.bet_id=b.id AND e.kind='settled')
    ORDER BY b.id"""


def _d(v) -> Decimal:
    return Decimal(str(v or 0))


def roots_first(*keyed) -> list[str]:
    """Every root present across `keyed` dicts, `sports` leading — the primary
    root heads every table, and the rest are alphabetical so a report re-run
    reproduces its own bytes (rule 9)."""
    keys = {k for d in keyed for k in d}
    return ([SPORTS_ROOT] if SPORTS_ROOT in keys else []) + sorted(keys - {SPORTS_ROOT})


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
    dollar staked. An EXITED bet counts — the exit P&L is realized (§9).

    NOT deduped by signal_id, unlike `pregame_drifts` below: the sizing
    comparison (§8) is the point of this table, so `V*/flat`, `V*/conf` and
    `V*/kelly` must stay three separate rows of one fired signal, each with
    its own stake and its own ROI."""
    out = []
    for r in store.query(SETTLED_SQL):
        bet = dict(r)
        pnl = metrics.bet_pnl_optimistic(store, bet)
        stake = _d(bet["stake_usd"])
        if pnl is None or not stake:
            continue
        out.append({"root": bet["category"] or "-",
                    "variant": bet["variant"] or "-", "event_key": bet["event_key"],
                    "signal_id": bet["signal_id"], "roi": pnl / stake, "bet": bet})
    return out


def realized_pnl(store) -> dict:
    """Σ dollar pnl over every settled WHALE bet row — every sizing variant
    (V*/flat, V*/conf, V*/kelly) is its own real paper stake, so all three
    count, unlike `roi_rows`' per-$1 ROI (grouped by root/variant for the CI
    tables below). The dashboard headline strip's first tile (2026-09-05);
    ledger-only, no tape, so deterministic per day like everything else here."""
    total, n = Decimal(0), 0
    for r in store.query(SETTLED_SQL):
        pnl = metrics.bet_pnl_optimistic(store, dict(r))
        if pnl is None:
            continue
        total += pnl
        n += 1
    return {"realized": q6(total), "realized_n": n}


def exits_recent(store, since) -> dict:
    """{"flip": n, "left": n} WHALE exits since `since` — the headline strip's
    24h count. `exit_stats` below is the cumulative, per-root/reason table;
    this is a cheap separate windowed read of the same source column (a 24h
    count needs no root split)."""
    out = {"flip": 0, "left": 0}
    for r in store.query(
            """SELECT be.payload_json FROM bet_events be JOIN flat_bets b
                 ON b.id = be.bet_id
                WHERE b.kind='WHALE' AND be.kind='settled' AND be.created_at >= ?""",
            (since.isoformat(),)):
        source = str(json.loads(r["payload_json"]).get("source") or "")
        if source.startswith("exit:"):
            reason = source.split(":", 1)[1]
            out[reason] = out.get(reason, 0) + 1
    return out


def roi_by_root(cfg, rows: list[dict]) -> dict:
    """{root: {variant: cell}}, sports first. Keying by root BEFORE variant is
    what makes pooling across roots impossible: no bootstrap ever sees two
    roots' ROIs in one `by_event`."""
    grouped: dict = {}
    for r in rows:
        grouped.setdefault(r["root"], {}).setdefault(r["variant"], []).append(r)
    return {root: {v: roi_cell(cfg, rs) for v, rs in sorted(grouped[root].items())}
            for root in roots_first(grouped)}


def roi_cell(cfg, rows: list[dict]) -> dict:
    by_event: dict = {}
    for r in rows:
        by_event.setdefault(r["event_key"], []).append(float(r["roi"]))
    cell = cluster_ci(cfg, by_event)
    wins = sum(1 for r in rows if r["roi"] > 0)
    cell["win_rate"] = q6(Decimal(wins) / len(rows)) if rows else Decimal(0)
    return cell


DRIFT_SQL = """
    SELECT m.horizon_secs h, m.mark_cost, b.id bet_id, b.legs_json, b.event_key,
           b.edge_points, b.category, b.signal_id, b.variant
      FROM markouts m JOIN flat_bets b ON b.id = m.bet_id
     WHERE b.kind='WHALE' AND m.in_play=0 AND m.leg_index=0"""


def pregame_drifts(store, horizons, since=None, until=None, root=None) -> list[dict]:
    """D_h = mark mid - mid0 on leg 0, one row per (SIGNAL, horizon) that
    carries a mid0. THE definition of the pregame drift — `markout_stats` and
    the §12 fast-kill window read it from here rather than each writing the
    JOIN.

    DEDUPED on `signal_id` (fix, review of 9e4158d): a fired signal books
    three sizing rows (`V*/flat`, `V*/conf`, `V*/kelly`, §8) that share one
    signal_id and the identical leg/price, so without this a marked signal was
    counted three times in every reader below — the evidence `fast_kill_min_marked`
    gates on was a third of what it looked like. The `/flat` row is kept as the
    representative (the registered "$1 per flag" convention), falling back to
    whichever sizing marked when flat did not. A row with no signal_id (legacy,
    pre-`variant`-column data) has no sibling sizing rows to collapse into and
    keeps its own bet_id as the dedup key — one row per bet, exactly as before.

    `until` bounds the BOOKING and the MARK: a day's window may only see what
    that day could see. `root` bounds it to one root — §7 item 5 reports the
    markout per root and runs kill rule 2 per root, because a non-sports close
    gate excludes far less than a sports kickoff does."""
    sql = DRIFT_SQL + f" AND m.horizon_secs IN ({','.join('?' * len(horizons))})"
    params = list(horizons)
    if root is not None:
        sql, params = sql + " AND b.category = ?", params + [root]
    if since is not None:
        sql, params = sql + " AND b.created_at >= ?", params + [since]
    if until is not None:
        sql += " AND b.created_at < ? AND m.marked_at < ?"
        params += [until, until]
    sql += " ORDER BY b.id"           # deterministic dedup pick (rule 9)
    best: dict[tuple, tuple[bool, dict]] = {}
    for r in store.query(sql, tuple(params)):
        mid0 = (json.loads(r["legs_json"])[0] or {}).get("mid0")
        if mid0 is None:
            continue
        sid = r["signal_id"]
        key = (sid, r["h"]) if sid is not None else ("bet", r["bet_id"], r["h"])
        is_flat = str(r["variant"] or "").endswith("/flat")
        cur = best.get(key)
        if cur is not None and not (is_flat and not cur[0]):
            continue                    # already have this signal's representative
        best[key] = (is_flat, {"bet_id": r["bet_id"], "h": r["h"],
                               "drift": _d(r["mark_cost"]) - _d(mid0),
                               "event_key": r["event_key"],
                               "root": r["category"] or "-",
                               "edge_points": _d(r["edge_points"])})
    return [row for _, row in best.values()]


def fired_signal_counts(store) -> dict:
    """{root: n} distinct FIRED signals booked, kind='WHALE' — one count per
    `signal_id` (its `V*/flat`+`V*/conf`+`V*/kelly` sizing rows collapse to
    one, same population `pregame_drifts` dedupes on the marked side), a
    legacy NULL-`signal_id` row counted once by its own `id` since it has no
    sibling sizing rows. This is `markout_stats`' coverage DENOMINATOR — both
    sides of that ratio must count signals, never booking rows."""
    seen: dict[str, set] = {}
    for r in store.query(
            "SELECT id, category, signal_id FROM flat_bets WHERE kind='WHALE'"):
        key = r["signal_id"] if r["signal_id"] is not None else ("bet", r["id"])
        seen.setdefault(r["category"] or "-", set()).add(key)
    return {root: len(keys) for root, keys in seen.items()}


def markout_stats(store) -> dict:
    """{root: {+2m, +10m}} PREGAME coverage and mean drift, plus the clusters
    the bench needs for the one-sided bound. Per root (§7 item 5): a root's
    coverage is its OWN marks over its OWN fired signals, so one root's thin
    marking never dilutes another's number.

    `cell["n"]` is `pregame_drifts`' deduped count — one per marked SIGNAL,
    not per booked row. `coverage` divides that by `fired_signal_counts`
    (fix: was `n_bets`, a raw row count across all three sizings — a signal
    that books three sizings but marks on only one of them used to read
    1-in-3 coverage instead of 1.0, because the numerator was deduped and the
    denominator was not). Both sides now count the same population: marked
    signals over FIRED signals, per horizon."""
    n_signals = fired_signal_counts(store)
    out = {root: {h: {"n": 0, "coverage": Decimal(0), "mean_drift": None,
                      "by_event": {}} for h in (H_2M, H_10M)}
           for root in roots_first(n_signals)}
    for r in pregame_drifts(store, (H_2M, H_10M)):
        cell = out[r["root"]][r["h"]]
        cell["n"] += 1
        cell["by_event"].setdefault(r["event_key"], []).append(float(r["drift"]))
    for root, cells in out.items():
        for cell in cells.values():
            drifts = [v for vs in cell["by_event"].values() for v in vs]
            cell["coverage"] = (q6(Decimal(cell["n"]) / n_signals[root])
                                if n_signals.get(root) else Decimal(0))
            if drifts:
                cell["mean_drift"] = q6(Decimal(sum(drifts) / len(drifts)))
    return out


def signals_by_id(con, ids) -> dict:
    """Tape `signals` by id, with the fired side's whale list parsed once.

    `ids` is the ids the caller will actually look up — the report and the
    bench only ever ask about signals the ledger booked, and json-parsing every
    row the collector has ever written to answer that is the whole tape's work
    for a handful of rows. `None` means all of them (the calibration run)."""
    if con is None:
        return {}
    if ids is None:
        rows = con.execute("SELECT * FROM signals").fetchall()
    else:
        ids = [i for i in ids if i is not None]
        if not ids:
            return {}
        from arb.dashboard_data import CHUNK      # local: avoids the report* import cycle
        rows = []
        for i in range(0, len(ids), CHUNK):          # SQLite's variable limit
            chunk = ids[i:i + CHUNK]
            rows += con.execute(f"SELECT * FROM signals WHERE id IN "
                                f"({','.join('?' * len(chunk))})", chunk).fetchall()
    return {r["id"]: {"side": r["side"], "variant": r["variant"],
                      "fired": r["fired"], "condition_id": r["condition_id"],
                      "whales": json.loads(r["whale_ids_json"] or "[]")}
            for r in rows}


def is_native(sig: dict) -> bool | None:
    """§7: native iff EVERY contributing whale on the FIRED side qualified on
    its own bucket. A whale on the other side never makes a signal transfer,
    and neither does a sybil-dropped one — `points()` (arb/whale_signal.py)
    already gives it zero weight, so it never contributed to the fire."""
    on_side = [w for w in sig["whales"] if str(w.get("side")) == str(sig["side"])
               and not w.get("sybil_dropped")]
    return all(bool(w.get("native")) for w in on_side) if on_side else None


def exit_stats(store, con, signals: dict) -> dict:
    """{root: {reason: cell}} — exits by reason with the exit-vs-hold
    counterfactual: hold = what the market's own resolution would have paid the
    same contracts (§12). Per root, like every other table here."""
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
        # `left` has two readings since the L14 amendment of 2026-09-04 — the
        # print tape and the venue's own positions?user= — and they are
        # separate rows here: each carries its own exit-vs-hold comparison, so
        # a rule that turns out to be worse than holding is visible on its own.
        via = (payload.get("detail") or {}).get("via")
        reason = source.split(":", 1)[1]
        cell = out.setdefault(bet["category"] or "-", {}).setdefault(
            f"{reason} ({via})" if via else reason,
            {"n": 0, "exit_pnl": Decimal(0), "hold_pnl": Decimal(0), "n_hold": 0})
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
    return {root: out[root] for root in roots_first(out)}
