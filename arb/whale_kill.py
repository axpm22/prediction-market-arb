"""§12's four kill rules for the L14 whale lane (split out of whale_metrics.py).

Everything here is a READ. Rule 2 counts "consecutive PRODUCED daily reports",
and the record of which days were produced is the report chain itself —
`<report_dir>/HASHES.log`, the append-only `<day> md:<sha> csv:<sha>` line
`write_daily` already writes. The streak is DERIVED from it at read time, on
each day's own 30-day window as of that day's end, so the report never has to
write a verdict row back to say what it did (rule 9: "Daily reports are always
recomputed from the raw log"). Consequences, all tested: an outage day pauses
the streak rather than resetting it, and re-running any day changes nothing.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from arb import metrics
from arb.models import q6
from arb.whale_metrics import (H_2M, SPORTS_ROOT, pregame_drifts, roi_cell,
                               roi_rows)

FAST_KILL_DAYS = 30                     # §12 rule 2's rolling window
FAST_KILL_STREAK = 2                    # "on two consecutive produced daily reports"
HASHES_LOG = "HASHES.log"               # write_daily's append-only report chain
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


def _state(fired: bool, armed: bool) -> str:
    return "FIRED" if fired else ("armed" if armed else "not yet")


def _or(v, dash="—"):
    return dash if v is None else v


def _fast_kill_window(store, since: str, until: str, root=SPORTS_ROOT) -> dict:
    """§12 rule 2's inputs over the 30 days ending at `until`: mean pregame
    D_2m, mean `flagged_edge` of the same marked bets, and how many bets those
    are. Keyed by bet, so a bet marked twice at +2m still counts once. Bounded
    to ONE root — rule 2 runs per root (§7 item 5) and the primary decision is
    the sports one."""
    per_bet = {r["bet_id"]: (r["drift"], r["edge_points"])
               for r in pregame_drifts(store, (H_2M,), since, until, root)}
    n = len(per_bet)
    if not n:
        return {"n_marked": 0, "mean_d2m": None, "mean_flagged_edge": None,
                "threshold": None}
    drift = q6(sum(v[0] for v in per_bet.values()) / n)
    edge = q6(sum(v[1] for v in per_bet.values()) / n)
    return {"n_marked": n, "mean_d2m": drift, "mean_flagged_edge": edge,
            "threshold": q6(Decimal("-0.5") * edge)}


def _window_at(store, until: str, root=SPORTS_ROOT) -> dict:
    since = (datetime.fromisoformat(until)
             - timedelta(days=FAST_KILL_DAYS)).isoformat()
    return _fast_kill_window(store, since, until, root)


def _held(fk: dict, min_marked: int) -> bool:
    """Rule 2's condition on ONE window: enough marked bets, and mean pregame
    D_2m below -0.5 x mean flagged_edge."""
    return bool(fk["n_marked"] >= min_marked and fk["mean_d2m"] is not None
                and fk["mean_d2m"] < fk["threshold"])


def produced_days(path) -> list[str]:
    """The days a daily report was actually PRODUCED, newest first, off the
    hash chain. Re-running a day appends another line for it; it is still one
    day. A missing chain means no report was ever produced here."""
    path = Path(path)
    if not path.exists():
        return []
    days = {ln.split(" ", 1)[0] for ln in path.read_text().splitlines()}
    return sorted((d for d in days if _DAY.fullmatch(d)), reverse=True)


def fast_kill_streak(cfg, store, hashes_log_path, now,
                     producing_day: str | None = None,
                     root: str = SPORTS_ROOT) -> dict:
    """Consecutive produced daily reports, newest first, on which rule 2's
    condition held — each evaluated AS OF that day's end (§12). A day with no
    line in the chain is neither counted nor a break: an outage PAUSES the
    streak. Nothing is stored, so a re-run cannot inflate it.

    `producing_day` is the day the CALLER is producing right now: its
    HASHES.log line does not exist yet (it is appended after the report is
    built), so without this it would silently miss its own day and the
    printed streak would lag the dashboard's live view by one. Unioned into
    the chain's days — re-running the same day once its line exists changes
    nothing."""
    min_marked = int(cfg["whale"]["kill"]["fast_kill_min_marked"])
    today = now.astimezone(ZoneInfo(str(cfg["timezone"]))).date().isoformat()
    days = set(produced_days(hashes_log_path))
    if producing_day is not None:
        days.add(producing_day)
    streak, newest = 0, None
    for day in sorted(days, reverse=True):
        if day > today:
            continue                    # a future-dated line is not evidence yet
        newest = newest or day
        if not _held(_window_at(store, metrics.day_bounds(cfg, day)[1], root),
                     min_marked):
            break
        streak += 1
    return {"streak": streak, "last_produced": newest}


def _root_status(cfg, store, all_rows, hashes_log_path, now, producing_day,
                 root) -> dict:
    """One NON-SPORTS root's own numbers, reported beside the primary decision:
    its own n, its own one-sided bounds and its own rule-2 streak. Never pooled
    into the sports figures above, and never the decision (registration
    2026-09-04)."""
    rows = [r for r in all_rows if r["root"] == root]
    cell = roi_cell(cfg, rows) if rows else None
    return {"n": len({r["signal_id"] for r in rows}),
            "mean": cell["mean"] if cell else None,
            "lb80": cell["lb80"] if cell else None,
            "ub90": cell["ub90"] if cell else None,
            "streak": fast_kill_streak(cfg, store, hashes_log_path, now,
                                       producing_day, root)["streak"]}


def kill_status(cfg, store, con=None, now=None, hashes_log_path=None,
                producing_day: str | None = None) -> dict:
    """§12's four kill rules, each `armed` / `not yet` / `FIRED` with the
    numbers behind it. Pure read; the daily whale section and the dashboard
    print the same block from this one computation.

    The DECISION is the sports root's (registration 2026-09-04: the primary
    metric stays the sports figure, and rows from different roots are never
    pooled) — every rule below, rule 4's signals/week floor included, counts
    sports rows only. Each non-sports root's own n / bounds / streak is
    reported beside it under `roots`."""
    w, now = cfg["whale"], now or datetime.now(timezone.utc)
    kill, primary = w["kill"], f"{w['primary_variant']}/flat"
    all_rows = [r for r in roi_rows(store) if r["variant"] == primary]
    rows = [r for r in all_rows if r["root"] == SPORTS_ROOT]
    cell = roi_cell(cfg, rows) if rows else None
    n = len({r["signal_id"] for r in rows})
    decision_n, null_n = int(kill["decision_n"]), int(kill["null_n"])
    ub90 = cell["ub90"] if cell else None
    lb80 = cell["lb80"] if cell else None

    # the LIVE window takes no upper bound: "as of now" is everything there is,
    # and a bet booked this instant is in it (a day-end window is exclusive)
    fk = _fast_kill_window(
        store, (now - timedelta(days=FAST_KILL_DAYS)).isoformat(), None,
        SPORTS_ROOT)
    min_marked = int(kill["fast_kill_min_marked"])
    held = _held(fk, min_marked)
    hashes = hashes_log_path or Path(str(cfg["report_dir"])) / HASHES_LOG
    fks = fast_kill_streak(cfg, store, hashes, now,
                           producing_day=producing_day, root=SPORTS_ROOT)
    streak = fks["streak"]

    # rule 4 counts the SPORTS root only, like every other rule here — a
    # non-sports root is thin by construction (5 politics markets over $1k at
    # the 7-day horizon, measured 2026-09-04) and would drag the lane's
    # starvation alarm down with it. The lane-wide count is still printed, by
    # the report's own signals line.
    fired_7d = store.query(
        "SELECT COUNT(DISTINCT signal_id) c FROM flat_bets WHERE kind='WHALE' "
        "AND signal_id IS NOT NULL AND category=? AND created_at >= ?",
        (SPORTS_ROOT, (now - timedelta(days=7)).isoformat()))[0]["c"]
    # bounded by `now`: an unbounded MIN would let a print the collector
    # recorded AFTER this report day (routine — the collector never stops for
    # a past day) count toward "days of collection", breaking the same purity
    # rule 9 needs from every other line in this table.
    oldest = con.execute("SELECT MIN(observed_at) t FROM prints WHERE observed_at <= ?",
                         (now.isoformat(),)).fetchone()["t"] \
        if con is not None else None
    collecting = (now - datetime.fromisoformat(oldest)) if oldest else timedelta(0)
    starving_armed = collecting >= timedelta(weeks=4)
    alarm = int(kill["min_signals_per_week_alarm"])

    other = (set(w["nonsports"]["tags"].values())
             | {r["root"] for r in all_rows}) - {SPORTS_ROOT}
    return {"as_of": now.isoformat(timespec="seconds"), "variant": primary,
            "root": SPORTS_ROOT,
            "roots": {root: _root_status(cfg, store, all_rows, hashes, now,
                                         producing_day, root)
                      for root in sorted(other)},
            "rules": [
        {"rule": 1, "name": "decision", "n": n, "target_n": decision_n,
         "ub90": ub90, "mean": cell["mean"] if cell else None,
         "state": _state(n >= decision_n and ub90 is not None and ub90 < 0,
                         n >= decision_n),
         "says": f"n={n}/{decision_n} settled sports {primary} signals; "
                 f"upper 90% bound {_or(ub90)} (< 0 kills)"},
        {"rule": 2, "name": "fast kill", "streak": streak,
         "streak_target": FAST_KILL_STREAK, "held_today": held,
         "last_produced": fks["last_produced"],
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
        {"rule": 4, "name": "starvation", "fired_7d": fired_7d, "alarm": alarm,
         "collecting_days": collecting.days,
         "state": _state(starving_armed and fired_7d < alarm, starving_armed),
         "says": f"{fired_7d} fired sports signals in 7d vs {alarm}/week floor; "
                 f"{collecting.days}d of collection (armed at 28d)"},
    ]}
