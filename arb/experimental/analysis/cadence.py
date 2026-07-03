"""MODEL_FOUNDATIONS Phase F — loop-cadence honesty.

The config says "60 s loop"; measured poll gaps say ~163 s. Rather than trust the config number,
report the truth: planned vs actual interval per loop, p50/p95 cycle time, from the polls table.
The p95 printed here is the loop's real spec — claims about "we scan every N seconds" cite it.
"""
from __future__ import annotations

from datetime import datetime, timezone


def _parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, int(round(q * (len(s) - 1))))
    return round(s[idx], 1)


def cadence_stats(store, kind: str = "watch", planned_secs: int | None = None) -> dict:
    """Actual inter-poll gaps (seconds) for a loop `kind` vs its planned interval."""
    with store.connect() as con:
        starts = [_parse(r["started_at"]) for r in con.execute(
            "SELECT started_at FROM polls WHERE kind=? ORDER BY started_at", (kind,))]
    gaps = [(b - a).total_seconds() for a, b in zip(starts, starts[1:])]
    return {
        "kind": kind, "n_polls": len(starts), "n_gaps": len(gaps),
        "planned_secs": planned_secs,
        "p50_secs": _percentile(gaps, 0.50),
        "p95_secs": _percentile(gaps, 0.95),
        "mean_secs": round(sum(gaps) / len(gaps), 1) if gaps else 0.0,
    }


def format_cadence(stats: dict) -> str:
    drift = ""
    if stats["planned_secs"] and stats["p50_secs"]:
        drift = f" ({stats['p50_secs'] / stats['planned_secs']:.1f}x planned)"
    return (f"cadence[{stats['kind']}]: planned {stats['planned_secs']}s, "
            f"actual p50 {stats['p50_secs']}s{drift} / p95 {stats['p95_secs']}s "
            f"over {stats['n_gaps']} gaps")
