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
import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path

from arb import whale_elite, whale_mat, whale_rank
from arb.models import q6
from arb.whale_tape import BACKOFF_FAMILIES
# the RENDER half, split out on size (every §12 table gained a per-root block):
# re-exported so `report_whale.whale_md` / `.whale_line` keep working for every
# existing caller.
from arb.report_whale_md import _root_md, whale_line, whale_md  # noqa: F401
from arb.whale_kill import kill_status
from arb.whale_metrics import (SPORTS_ROOT, exit_stats, exits_recent,
                               is_native, markout_stats, realized_pnl,
                               roi_by_root, roi_cell, roi_rows, roots_first,
                               signals_by_id)
# the tape's read-only open funnel, split out on size (2026-09-05):
# re-exported so `report_whale.open_tape_ro` keeps working for every existing
# caller (dashboard_whale.py, dashboard_whale_bundle.py, commands.py, ...).
from arb.whale_tape_ro import open_tape_ro  # noqa: F401


def whale_frame(cfg, store, now: datetime | None = None, con=None,
                hashes_log_path=None, producing_day: str | None = None) -> dict:
    """Everything §12 asks the daily report to print about L14. `con` is a tape
    handle for tests and callers that already hold one; otherwise the tape is
    opened read-only here and closed before returning — on the error path too.
    `hashes_log_path` is the report chain rule 2's streak is derived from
    (default: the configured `report_dir`; `write_daily` passes its own outdir,
    so a report written elsewhere counts the days written there). `producing_day`
    is the day this call is FOR — not yet in the chain — so rule 2 counts it too
    (see `whale_kill.fast_kill_streak`).

    The daily report's ONLY entry point — always builds its own `state` and
    never consults arb/dashboard_cache.py or arb/dashboard_whale_bundle.py, so
    a report re-run over a past day stays a pure function of the tape and the
    ledger (rule 9). The body that turns a `state` into this frame is
    `_frame_from_state` below, split out so the dashboard's shared bundle can
    build the SAME frame from the ONE score_state pass it already ran for
    /api/whale/traders and /api/positions, instead of a second tape scan."""
    from arb.whale_state import score_state
    now = now or datetime.now(timezone.utc)
    opened = con is None and (con := open_tape_ro(cfg)) is not None
    try:
        # ONE scoring pass over the tape for both readers of it below: the
        # roster and the trader profiles are the same positions, differently
        # summarised, and score_state rebuilds every position from every print
        state = score_state(cfg, con, now) if con is not None else None
        return _frame_from_state(cfg, store, con, now, state,
                                 hashes_log_path, producing_day)
    finally:
        if opened:
            con.close()


def _frame_from_state(cfg, store, con, now, state, hashes_log_path,
                      producing_day: str | None) -> dict:
    """`whale_frame`'s body, given an already-built `state` (or None — no
    tape). Pure with respect to scoring: every further read here is a cheap
    ledger/tape query, never a second score_state pass."""
    fire = cfg["whale"]["fire"]
    day_ago, week_ago = now - timedelta(days=1), now - timedelta(days=7)
    booked = {r["signal_id"] for r in store.query(
        "SELECT DISTINCT signal_id FROM flat_bets WHERE kind='WHALE' "
        "AND signal_id IS NOT NULL")}
    # only the booked ones: every lookup below is by a bet's own signal_id
    signals = signals_by_id(con, booked)
    rows = roi_rows(store)
    qualifying = _qualifying(cfg, state)
    native_of = {sid: is_native(sig) for sid, sig in signals.items()}
    classed = {"native": [], "transfer": []}
    for r in rows:
        cls = native_of.get(r["signal_id"])
        # SPORTS only: this diagnostic split is a property of the primary
        # root's figure, and pooling roots into it is exactly what the
        # 2026-09-04 registration forbids.
        if cls is not None and r["root"] == SPORTS_ROOT:
            classed["native" if cls else "transfer"].append(r)
    counts = _signal_counts(store, con, day_ago, week_ago)
    return {
        "as_of": now.isoformat(timespec="seconds"),
        "tape_ok": con is not None,
        # dashboard headline strip's P&L tile (2026-09-05): dollars, every
        # settled WHALE bet row, ledger-only — deterministic per day, unlike
        # the unrealized mark the dashboard adds client-side from /api/positions.
        "pnl": realized_pnl(store),
        "primary_variant": f"{cfg['whale']['primary_variant']}/flat",
        "wallets": {**_wallets(con),
                    "hygiene_excluded": qualifying.pop("hygiene_excluded", 0)},
        "qualifying": qualifying,
        # read-time diagnostics (design §12 "Trader profiles"): who is
        # good, and where. Never an input to points, sizing or firing.
        "traders": whale_rank.traders_frame(cfg, state, now),
        "signals": counts,
        # every table below is keyed by ROOT first (registration
        # 2026-09-04): the primary metric stays the sports figure and rows
        # from different roots are never pooled.
        "primary_root": SPORTS_ROOT,
        "bets": _bets_by_root(store),
        "roi": roi_by_root(cfg, rows),
        "markouts": markout_stats(store),
        "exits": exit_stats(store, con, signals),
        # headline strip's "exits 24h" tile — exit_stats above is cumulative
        # and per-root; this is the same source column, windowed, root-agnostic.
        "exits_24h": exits_recent(store, day_ago),
        # §7 V5: the percentile bar this lane is currently firing on, and
        # the population behind it. Printed because the elite set moves
        # week to week (§3) — a step change must be attributable.
        "elite": {"pct": int(fire["elite_pct"]),
                  "min_score_lb": cfg["whale"]["min_score_lb"],
                  "cutoffs": whale_elite.latest(con, now) if con else []},
        # both MAX(as_of) reads below are bound to `now` (coordinator
        # review of a42fb14, rule 9 determinism): unbounded, a report
        # re-run over a past day printed today's row instead of the one
        # actually in force on the day being reported.
        "base_rates": [dict(r) for r in con.execute(
            "SELECT root, as_of, value, n FROM base_rates b WHERE as_of = "
            "(SELECT MAX(as_of) FROM base_rates x WHERE x.root=b.root "
            "AND x.as_of <= ?) ORDER BY root", (now.isoformat(),))] if con else [],
        "native": {
            "signals": {k: sum(1 for sid, cls in native_of.items()
                               if sid in booked and cls is (k == "native"))
                        for k in ("native", "transfer")},
            "roi": {k: roi_cell(cfg, v) for k, v in classed.items() if v}},
        "collector": _collector(cfg, con, now),
        "calibration": {
            "min_points": dict(fire["min_points"]),
            "signal_days": con.execute(
                "SELECT COUNT(DISTINCT substr(evaluated_at, 1, 10)) d FROM signals"
            ).fetchone()["d"] if con else 0,
            "calibration_days": int(fire["calibration_days"])},
        # rule 4's count is the SPORTS root's own (whale_kill), while the
        # signals line above prints the lane-wide one — two different
        # questions, so they are two different reads.
        "kill": {"decision_n": int(cfg["whale"]["kill"]["decision_n"]),
                 "null_n": int(cfg["whale"]["kill"]["null_n"]),
                 "min_signals_per_week_alarm":
                     int(cfg["whale"]["kill"]["min_signals_per_week_alarm"]),
                 **kill_status(cfg, store, con, now, hashes_log_path,
                               producing_day)},
    }


def _bets_by_root(store) -> dict:
    """{root: {variant: bets}}, sports first — the booked count behind each
    cell of the per-root variant table."""
    out: dict = {}
    for r in store.query("SELECT category, variant, COUNT(*) c FROM flat_bets "
                         "WHERE kind='WHALE' GROUP BY category, variant"):
        out.setdefault(r["category"] or "-", {})[r["variant"] or "-"] = r["c"]
    return {root: dict(sorted(out[root].items())) for root in roots_first(out)}


def _wallets(con) -> dict:
    if con is None:
        return {"tracked": 0, "backfilled": 0}
    row = con.execute("SELECT COUNT(*) n, COUNT(backfilled_at) done "
                      "FROM wallets").fetchone()
    return {"tracked": row["n"], "backfilled": row["done"]}


def _qualifying(cfg, state) -> dict:
    """The lane's own roster, scored by the lane's own assembly (whale_state),
    never a second implementation of it."""
    if state is None:
        return {"by_root": {}, "by_sport": {}, "top": [], "n": 0,
                "hygiene_excluded": 0}
    from arb.whale_state import qualifying_whales
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


def _collector(cfg, con, now) -> dict:
    """Is the tape being written at all? Status, rows, bad_rows, `at` and
    backoff_until are the lane's OWN last cycle (tape `meta`) — kept in this
    frame for the live dashboard (arb/dashboard_whale.py reads the same
    frame), but `whale_md` must never print them: the collector rewrites
    them on its own ~60s cadence regardless of which day is being reported,
    so they are not a function of the report day and would defeat the hash
    chain (rule 9). The gap is §5 step 2's recorded paging gap, kept only
    when it happened at or before `now` — a day's report must not show a gap
    from AFTER that day. prints/min is measured over the hour before `now`,
    by `observed_at` (when the collector recorded the row — bounded above by
    `now` too) rather than `ts` (the trade's own venue timestamp, which the
    query used to leave with no upper bound at all).

    The ops figure (retention amendment 2026-09-04, proposal §2): prints and
    `holdings` rows over the 24h before `now`, by `observed_at` like the 1h
    count — a function of the report day, printed in the md. `tape_bytes` is a
    stat of the configured file (never an open, so a WAL tape its writer holds
    still reports); `bytes_per_day` is mean bytes per row over the whole file
    (all tables) × rows written in the trailing 24h — an estimate, not a
    measurement, LIVE like tape_bytes; below 24h old (rows_24h == total_rows
    collapses it to `tape_bytes`) it scales by 86400/age_secs off the oldest
    print's age instead, flagged `bytes_per_day_confidence: "low"`.
    `backoff_until` is the COLLECTOR family's own pause (kept under this name
    for callers that only ever cared about the print collector); `backoff`
    surfaces all three families the tape's callers can pause independently
    (whale_tape.BACKOFF_FAMILIES) — a backfill 429 must not read here as the
    collector being down, and vice versa."""
    out = {"status": None, "rows": None, "bad_rows": None, "at": None,
           "duration_secs": None, "last_seen_age_secs": None, "last_gap": None,
           "backoff_until": None, "backoff": {f: None for f in BACKOFF_FAMILIES},
           "prints_per_min": Decimal(0), "prints_24h": 0,
           "holdings_24h": 0, "tape_bytes": None, "bytes_per_day": None,
           "bytes_per_day_confidence": None, "mat": {}, "mat_stale": None}
    path = Path(str(cfg["whale"]["tape_db"]))
    if path.exists():
        out["tape_bytes"] = os.path.getsize(path)
    if con is None:
        return out
    meta = {r["key"]: r["value"] for r in con.execute("SELECT key, value FROM meta")}
    last = json.loads(meta.get("last_cycle") or "{}")
    out.update(status=last.get("status"), rows=last.get("prints"),
               bad_rows=last.get("bad_rows"), at=last.get("at"),
               duration_secs=last.get("duration_secs"),
               backoff_until=meta.get("backoff_until:collector"),
               backoff={f: meta.get(f"backoff_until:{f}") for f in BACKOFF_FAMILIES},
               # matpos (2026-09-05): cached positions, wallets still queued
               # for recompute, and what the last pass cost. LIVE like every
               # other field above — off the lane's own last cycle, so the
               # console line and the dashboard, never the hashed md.
               mat=last.get("mat") or {},
               # ... and, LIVE like the rest of this block, whether the cache
               # those figures describe is current at all (I2). Kept beside
               # `mat` rather than inside it so a cycle that predates the
               # cache still reads as no mat figures at all.
               mat_stale=whale_mat.freshness(con, cfg, now)["note"])
    if meta.get("last_seen_ts"):
        out["last_seen_age_secs"] = int(now.timestamp()) - int(meta["last_seen_ts"])
    gap = json.loads(meta["last_gap"]) if meta.get("last_gap") else None
    if gap and gap.get("at") and datetime.fromisoformat(gap["at"]) <= now:
        out["last_gap"] = gap
    hour, day = ((now - timedelta(hours=h)).isoformat() for h in (1, 24))
    # one pass over the window for both counts: prints has no observed_at index
    row = con.execute("SELECT SUM(observed_at >= ?) h, COUNT(*) d FROM prints "
                      "WHERE observed_at >= ? AND observed_at <= ?",
                      (hour, day, now.isoformat())).fetchone()
    out["prints_per_min"] = q6(Decimal(row["h"] or 0) / 60)
    out["prints_24h"] = row["d"]
    out["holdings_24h"] = con.execute(
        "SELECT COUNT(*) c FROM holdings WHERE observed_at >= ? AND observed_at <= ?",
        (day, now.isoformat())).fetchone()["c"]
    signals_24h = con.execute(
        "SELECT COUNT(*) c FROM signals WHERE evaluated_at >= ? AND evaluated_at <= ?",
        (day, now.isoformat())).fetchone()["c"]
    total_rows = con.execute(
        "SELECT (SELECT COUNT(*) FROM prints) + (SELECT COUNT(*) FROM holdings)"
        " + (SELECT COUNT(*) FROM signals)").fetchone()[0]
    if out["tape_bytes"] is not None and total_rows:
        rows_24h = out["prints_24h"] + out["holdings_24h"] + signals_24h
        est = Decimal(out["tape_bytes"]) * rows_24h / total_rows
        oldest = con.execute("SELECT MIN(observed_at) m FROM prints").fetchone()["m"]
        age = int((now - datetime.fromisoformat(oldest)).total_seconds()) if oldest else 86400
        if age < 86400:                    # rows_24h == total_rows for a tape this young
            est = (est * 86400 / Decimal(max(age, 1))).quantize(Decimal(1), ROUND_HALF_EVEN)
        out["bytes_per_day"] = int(est)
        out["bytes_per_day_confidence"] = "low" if age < 86400 else "ok"
    return out

