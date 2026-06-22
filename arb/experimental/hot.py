"""Hot watchers — a dedicated re-checker per bet, at ~1-second resolution.

The wide sweep finds a bet; the flash verifier gives one ~10s verdict; this module gives
each fresh ARB its own thread that re-quotes the bet's exact books every second until
the edge dies or the window expires. Output per bet: a high-resolution sample track
(hot_samples table) and one summary LIFETIME row in the ledger — how long the arb
actually lived, and the best/last edge seen. Arb lifetime is THE capture question.

Watcher count is capped: the limit is venue rate budgets, not CPU.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable

from arb.pricing import q6
from arb.store import Store

from arb.experimental.verify import Fetcher, live_fetchers

import json

SAMPLES_SCHEMA = """
CREATE TABLE IF NOT EXISTS hot_samples(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bet_id INTEGER NOT NULL,
    checked_at TEXT NOT NULL,
    total_cost TEXT,
    edge TEXT,
    FOREIGN KEY(bet_id) REFERENCES flat_bets(id)
);
"""

Clock = Callable[[], float]


def unwatched_bets(store: Store, now: datetime, max_age_secs: int) -> list[sqlite3.Row]:
    cutoff = (now - timedelta(seconds=max_age_secs)).isoformat()
    with store.connect() as con:
        con.executescript(SAMPLES_SCHEMA)
        return list(
            con.execute(
                """
                SELECT * FROM flat_bets b
                 WHERE b.kind = 'ARB' AND b.status = 'OPEN' AND b.placed_at >= ?
                   AND b.notes NOT LIKE '%research-unverifiable%'
                   AND NOT EXISTS (SELECT 1 FROM flat_bets s
                                    WHERE s.reappearance_of = b.id AND s.status = 'LIFETIME')
                 ORDER BY b.id
                """,
                (cutoff,),
            )
        )


def watch_bet(store: Store, bet: sqlite3.Row, fetchers: dict[str, Fetcher], hcfg: dict[str, Any],
              sleeper: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Re-quote one bet's legs on a tight cadence until the edge dies; append the track."""
    legs = json.loads(bet["legs_json"])
    interval = float(hcfg["check_interval_secs"])
    deadline = time.monotonic() + float(hcfg["watch_duration_secs"])
    dead_needed = int(hcfg["dead_checks"])
    checks = 0
    dead_streak = 0
    first_edge: Decimal | None = None
    best_edge: Decimal | None = None
    last_edge: Decimal | None = None
    born = time.monotonic()
    died_after: float | None = None

    while time.monotonic() < deadline:
        total = _requote_total(legs, fetchers)
        checks += 1
        edge = None if total is None or total <= 0 else q6(Decimal("1") / total - Decimal("1"))
        _record_sample(store, int(bet["id"]), total, edge)
        if edge is not None and total < Decimal("1"):
            dead_streak = 0
            first_edge = first_edge if first_edge is not None else edge
            best_edge = edge if best_edge is None or edge > best_edge else best_edge
            last_edge = edge
        else:
            dead_streak += 1
            if dead_streak >= dead_needed:
                died_after = time.monotonic() - born - (dead_needed - 1) * interval
                break
        sleeper(interval)

    alive_secs = died_after if died_after is not None else time.monotonic() - born
    summary = {
        "checks": checks,
        "alive_secs": max(0.0, alive_secs),
        "survived_window": died_after is None,
        "first_edge": first_edge,
        "best_edge": best_edge,
        "last_edge": last_edge,
    }
    _record_lifetime(store, bet, summary)
    return summary


def run_hot_pass(store: Store, config, hcfg: dict[str, Any],
                 fetchers: dict[str, Fetcher] | None = None,
                 sleeper: Callable[[float], None] = time.sleep) -> int:
    """Adopt fresh bets up to max_watchers and watch them to completion (threaded)."""
    fetchers = fetchers if fetchers is not None else live_fetchers(config)
    bets = unwatched_bets(store, datetime.now(timezone.utc), int(hcfg["watch_duration_secs"]))
    bets = bets[: int(hcfg["max_watchers"])]
    if not bets:
        return 0
    threads = [
        threading.Thread(target=watch_bet, args=(store, bet, fetchers, hcfg, sleeper), daemon=True)
        for bet in bets
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return len(bets)


def _requote_total(legs: list[dict[str, Any]], fetchers: dict[str, Fetcher]) -> Decimal | None:
    total = Decimal("0")
    for leg in legs:
        fetcher = fetchers.get(str(leg["venue"]))
        if fetcher is None:
            return None
        try:
            current = fetcher(leg)
        except Exception:  # noqa: BLE001 — a failed check is a missed sample, not a crash
            return None
        if current is None:
            return None
        total += current
    return total


def _record_sample(store: Store, bet_id: int, total: Decimal | None, edge: Decimal | None) -> None:
    with store.connect() as con:
        con.execute(
            "INSERT INTO hot_samples(bet_id, checked_at, total_cost, edge) VALUES (?, ?, ?, ?)",
            (bet_id, datetime.now(timezone.utc).isoformat(),
             None if total is None else str(total), None if edge is None else str(edge)),
        )


def _record_lifetime(store: Store, bet: sqlite3.Row, summary: dict[str, Any]) -> None:
    # 'censored' makes it unmistakable that the edge OUTLIVED the watch window, so its
    # alive_secs is a lower bound, not the true lifetime — lifetime stats must filter these
    # out or treat them as right-censored, never average them as if they died at the cap.
    censored = summary["survived_window"]
    note = (
        f"hot-watch: alive={summary['alive_secs']:.1f}s checks={summary['checks']} "
        f"censored={censored} survived_window={censored} first={summary['first_edge']} "
        f"best={summary['best_edge']} last={summary['last_edge']}"
    )
    with store.connect() as con:
        con.execute(
            """
            INSERT INTO flat_bets(fingerprint, reappearance_of, opened_poll_id, category, sport, kind,
                                  trusted, live, edge, stake_usd, legs_json, status, placed_at, notes)
            VALUES (?, ?, ?, ?, ?, 'ARB', ?, ?, ?, '0', ?, 'LIFETIME', ?, ?)
            """,
            (
                bet["fingerprint"], bet["id"], bet["opened_poll_id"], bet["category"], bet["sport"],
                bet["trusted"], bet["live"],
                str(summary["best_edge"]) if summary["best_edge"] is not None else bet["edge"],
                bet["legs_json"], datetime.now(timezone.utc).isoformat(), note,
            ),
        )
