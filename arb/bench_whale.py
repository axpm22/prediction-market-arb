"""L14 — whale-consensus lane, scored for `arb bench` (design 2026-09-03 §12).

The pre-registered primary is the mean settled ROI per dollar staked on the
V2/flat bookings, clustered by `event_key`, at the ONE-SIDED 10th/90th
percentiles of the cluster bootstrap. Everything is read from the ledger the
bench already opened read-only; the tape is opened read-only too, and only for
the native/transfer split, which lives nowhere else.

With ZERO settled V2/flat rows the lane is BLOCKED, not UNDERPOWERED: on the
shared ladder BLOCKED means "cannot be computed at all" (arb/bench.py), and with
no sample there is no interval to report. `LaneResult.verdict` defaults to
BLOCKED, so the early return below leaves it there with the reason in `note`.
UNDERPOWERED is the 0 < n < `decision_n` case: an interval exists, it is
reported, and the registration says not to read it yet.

Three pre-registered rules decide the verdict, in this order:

1. `n < whale.kill.decision_n` -> UNDERPOWERED, whatever the interval says. The
   registration fixed the decision point before any data existed; a lucky
   six-signal interval that excludes zero is not the lane clearing its bar.
2. Upper 90% bound < 0 at or past that n -> the lane is KILLED (§12 kill rule
   1). It is reported as NULL with the kill named: a decisive negative retires
   the lane, and ranking it as "SIGNAL adverse" would read as a finding worth
   pursuing.
3. Otherwise the shared verdict ladder, on the one-sided bounds.

n is the count of DISTINCT `signal_id` values booked under V2/flat — the unit
the registration counts ("at n >= 100 signals"), not the row count, and never
`signals.fired`, which counts re-evaluations.
"""
from __future__ import annotations

from arb.bench import LaneResult, verdict_for
from arb.whale_metrics import (H_10M, SPORTS_ROOT, cluster_ci, is_native,
                               markout_stats, roi_cell, roi_rows, signals_by_id)


class _S:                            # metrics/whale_metrics want a Store-like reader
    def __init__(self, con):
        self.con = con

    def query(self, sql, params=()):
        return self.con.execute(sql, params).fetchall()


def l14_whale(cfg, con, hist) -> LaneResult:
    """`con` is the bench's read-only ledger handle; `hist` is unused (this
    lane's evidence is our own paper bookings, not the Kalshi corpus)."""
    w = cfg["whale"]
    primary = f"{w['primary_variant']}/flat"
    r = LaneResult(lane="L14", name="whale-consensus follower", unit="signal",
                   primary=f"mean settled ROI per $ staked, {primary}, sports "
                           f"root, event_key-clustered, one-sided 90% bounds")
    store = _S(con)
    # SPORTS only, both here and in every detail line below: the registered
    # primary metric IS the sports-root figure, and rows from different roots
    # are never pooled (amendment 2026-09-04).
    rows = [x for x in roi_rows(store) if x["root"] == SPORTS_ROOT]
    mine = [x for x in rows if x["variant"] == primary]
    if not mine:
        r.note = (f"no settled {primary} booking yet — the lane collects before "
                  f"it can be scored")
        return r
    cell = roi_cell(cfg, mine)
    r.n = len({x["signal_id"] for x in mine})
    r.estimate, r.lo, r.hi = cell["mean"], cell["lo90"], cell["ub90"]
    decision_n = int(w["kill"]["decision_n"])
    if r.n < decision_n:
        r.verdict = "UNDERPOWERED"
        r.note = (f"n={r.n} signals below the pre-registered decision_n="
                  f"{decision_n}; the interval is reported, not read")
    elif r.hi < 0:
        r.verdict = "NULL"
        r.note = (f"KILLED (§12 kill rule 1): upper 90% bound {r.hi} < 0 at "
                  f"n={r.n} >= decision_n={decision_n} — booking stops, "
                  f"collection continues")
    else:
        r.verdict = verdict_for(cfg, r.estimate, r.lo, r.hi, r.n, r.null_band)
        r.note = (f"lower 80% bound {cell['lb80']} (success needs > 0 at "
                  f"n >= {decision_n}); NULL kill at n={w['kill']['null_n']}")
    # the shared table header says "95% CI" for every lane; this one is not
    r.detail.append("the interval column is this lane's ONE-SIDED [10th, 90th] "
                    "cluster-bootstrap percentiles (§12), not a 95% CI")
    r.detail.append(f"{cell['n']} settled bets over {cell['n_events']} events; "
                    f"win rate {cell['win_rate']}; lb80 {cell['lb80']}")
    r.detail.append(_markout_detail(cfg, store))
    r.detail.append("per-variant ROI/$: " + (", ".join(
        f"{v} {roi_cell(cfg, [x for x in rows if x['variant'] == v])['mean']} "
        f"(n={sum(1 for x in rows if x['variant'] == v)})"
        for v in sorted({x["variant"] for x in rows}))))
    r.detail.append(_native_detail(cfg, rows))
    return r


def _markout_detail(cfg, store) -> str:
    """§12's powered early metric: the +10m pregame markout, one-sided adverse
    (the upper 90% bound below zero is what 'significantly adverse' means)."""
    cell = markout_stats(store).get(SPORTS_ROOT, {}).get(H_10M)
    if not cell or not cell["n"]:
        return "+10m pregame markout: no marks yet"
    ci = cluster_ci(cfg, cell["by_event"])
    adverse = " ADVERSE" if ci["ub90"] < 0 else ""
    return (f"+10m pregame markout mean {ci['mean']} (ub90 {ci['ub90']}{adverse}), "
            f"{cell['n']} marks, coverage {cell['coverage']}")


def _native_detail(cfg, rows) -> str:
    """Native vs transfer ROI (§7): if the operator's transfer assumption is
    wrong, this split says so instead of diluting the aggregate."""
    from arb.report_whale import open_tape_ro
    con = open_tape_ro(cfg)
    if con is None:
        return "native/transfer ROI: whale tape unavailable"
    try:
        native_of = {sid: is_native(sig) for sid, sig in
                     signals_by_id(con, {x["signal_id"] for x in rows}).items()}
    finally:
        con.close()
    split: dict[str, list] = {"native": [], "transfer": []}
    for x in rows:
        cls = native_of.get(x["signal_id"])
        if cls is not None:
            split["native" if cls else "transfer"].append(x)
    if not any(split.values()):
        return "native/transfer ROI: no signal carries its whale list"
    return "native/transfer ROI/$: " + ", ".join(
        f"{k} {roi_cell(cfg, v)['mean']} (n={len(v)})"
        for k, v in split.items() if v)
