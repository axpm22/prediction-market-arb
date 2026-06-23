from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .lifecycle import active_rows_via_index
from .store import Store


@dataclass(frozen=True)
class SettlementHealth:
    open_count: int
    overdue_count: int
    oldest_overdue_event_day: date | None
    last_settlement_timestamp: str | None
    overdue_event_days: list[date]

    @property
    def healthy(self) -> bool:
        return self.overdue_count == 0


def settle_health(store: Store, timezone_name: str, grace_hours: int, now: datetime | None = None) -> SettlementHealth:
    tz = ZoneInfo(timezone_name)
    local_now = (now or datetime.now(tz)).astimezone(tz)
    overdue_cutoff_day = (local_now - timedelta(hours=grace_hours)).date()
    active = _active_sports_bets(store)
    overdue: list[date] = []
    for row in active:
        event_day = _event_day(row["legs_json"])
        if event_day is not None and event_day < overdue_cutoff_day:
            overdue.append(event_day)
    with store.connect() as con:
        last_settlement = con.execute("SELECT max(settled_at) FROM flat_bets WHERE status = 'SETTLED'").fetchone()[0]
    return SettlementHealth(
        open_count=len(active),
        overdue_count=len(overdue),
        oldest_overdue_event_day=min(overdue) if overdue else None,
        last_settlement_timestamp=last_settlement,
        overdue_event_days=sorted(overdue),
    )


def format_settle_health(health: SettlementHealth) -> str:
    lines = [
        f"open bets: {health.open_count} | OVERDUE (event day past grace window, still open): {health.overdue_count}",
    ]
    if health.oldest_overdue_event_day is not None:
        lines.append(f"oldest overdue event day: {health.oldest_overdue_event_day.isoformat()}  (investigate if not 0)")
    lines.append(f"last settlement timestamp: {health.last_settlement_timestamp}")
    return "\n".join(lines)


def _active_sports_bets(store: Store):
    # S1: indexed latest-lifecycle read (was a full kind/status scan + Python derivation).
    with store.connect() as con:
        return list(active_rows_via_index(con, kinds=("EV", "ARB", "CLOSE")).values())


def _event_day(legs_json: str) -> date | None:
    try:
        legs = json.loads(legs_json)
        event_key = str(legs[0].get("event_key", ""))
        parts = event_key.split(":")
        if len(parts) < 2:
            return None
        return date.fromisoformat(parts[1])
    except (ValueError, TypeError, IndexError, json.JSONDecodeError):
        return None
