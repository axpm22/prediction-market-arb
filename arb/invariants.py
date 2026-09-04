"""arb check-invariants (SPEC §10.4): the six ledger invariants. Silent when
clean; CRITICAL log + annotation when not. Run after every settle cycle and in CI."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from arb.logutil import get_logger
from arb.models import RESOLUTION_SETTLED_KINDS

log = get_logger("arb.invariants", "logs/scan.log")

EXCLUSION_ANNOTATIONS = ("settle-tripwire", "INVALID_AFTER_FINAL")


def _exited(payload_json) -> bool:
    """A `settled` row written by a lane's own exit, not by an oracle."""
    try:
        return str(json.loads(payload_json or "{}").get("source") or "") \
            .startswith("exit:")
    except (ValueError, AttributeError):
        return False


def check(cfg, store) -> list[str]:
    v: list[str] = []

    # (1) no bet settled before placement + (6) at most one settled row, none early
    rows = store.query(
        """SELECT be.bet_id, COUNT(*) n, MIN(be.created_at) first_settle, fb.created_at placed
           FROM bet_events be JOIN flat_bets fb ON fb.id = be.bet_id
           WHERE be.kind = 'settled' GROUP BY be.bet_id""")
    for r in rows:
        if r["n"] > 1:
            v.append(f"invariant-6: bet {r['bet_id']} has {r['n']} settled rows")
        if r["first_settle"] < r["placed"]:
            v.append(f"invariant-1/6: bet {r['bet_id']} settled before placement")

    # (2) no bet placed after its event's final (start + typical sport duration)
    for r in store.query(
            "SELECT id, sport, created_at, event_start_utc FROM flat_bets "
            "WHERE event_start_utc IS NOT NULL"):
        dur = cfg["sport_typical_duration_hours"].get(r["sport"])
        if dur is None:
            continue
        start = datetime.fromisoformat(r["event_start_utc"])
        placed = datetime.fromisoformat(r["created_at"])
        if (placed - start).total_seconds() > float(dur) * 3600:
            v.append(f"invariant-2: bet {r['id']} placed after estimated final")

    # (3) resolution-settled rows: event in the past at settle; locked rows:
    # pregame at placement
    for r in store.query(
            """SELECT fb.id, fb.kind, fb.created_at, fb.event_start_utc,
                      be.created_at settled_at, be.payload_json
               FROM flat_bets fb JOIN bet_events be
                 ON be.bet_id = fb.id AND be.kind = 'settled'
               WHERE fb.event_start_utc IS NOT NULL"""):
        start = datetime.fromisoformat(r["event_start_utc"])
        if _exited(r["payload_json"]):
            # L14 §9: an exit settles a bet PREGAME by design — that is the
            # whole point of the exit rule. Only an ORACLE settlement before
            # the event can be early, so this row is not invariant 3's business
            # (the tripwire exempts the same `exit:` prefix).
            continue
        if r["kind"] in RESOLUTION_SETTLED_KINDS:
            if datetime.fromisoformat(r["settled_at"]) < start:
                v.append(f"invariant-3: bet {r['id']} resolution-settled before start")
        else:
            if datetime.fromisoformat(r["created_at"]) > start:
                v.append(f"invariant-3: locked bet {r['id']} placed in-play")

    # (4) reappearance bound un-annotated. Pass a Python ISO bound, not
    # datetime('now','-1 day'): SQLite's space-separated string vs the
    # T-separated ISO created_at compares lexicographically to a date-granularity
    # window (off by up to a day). Match how tracker.py computes the 24h bound.
    limit = int(cfg["tracker"]["max_reappearances"])
    bound = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    for r in store.query(
            """SELECT fingerprint, COUNT(*) n FROM flat_bets
               WHERE created_at > ?
               GROUP BY fingerprint HAVING n > ?""", (bound, limit)):
        noted = store.query(
            "SELECT 1 FROM annotations WHERE kind='reappearance-storm' AND detail LIKE ?",
            (f"%{r['fingerprint']}%",))
        if not noted:
            v.append(f"invariant-4: fingerprint {r['fingerprint']} has {r['n']} rows/24h")

    # (5) no P&L cell includes an annotated-excluded row — metrics must skip these;
    # verified here by asserting every excluded bet is visible to the exclusion query
    # metrics uses (same annotation kinds, same target coding)
    for r in store.query(
            """SELECT DISTINCT a.target_id FROM annotations a
               WHERE a.target_kind = 'bet' AND a.kind IN (?, ?)""",
            EXCLUSION_ANNOTATIONS):
        if not store.query("SELECT 1 FROM flat_bets WHERE id = ?", (r["target_id"],)):
            v.append(f"invariant-5: exclusion annotation targets missing bet "
                     f"{r['target_id']}")

    for msg in v:
        log.critical(msg)
        store.insert("annotations", {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "target_kind": "day", "target_id": datetime.now(timezone.utc)
            .date().isoformat(), "kind": "invariant-violation", "detail": msg})
    return v
