"""Per-process memo for the dashboard's expensive read-time computations
(the whale panel and trader rankings — a single frame over ~200k tape prints).

Concurrent requests hammering the same key used to each start their own
recompute; under load that piled up CPU and open ledger handles faster than
the page's own auto-refresh could drain them. This module makes N concurrent
callers for the same key wait on ONE `compute()` and share its result, and
skips recomputing altogether inside `dashboard.cache_secs` of the last one —
a read-only monitor panel lagging the tape by at most one refresh (SPEC
§14.3) is the deliberate trade.

Keyed by whatever hashable tuple the caller builds (endpoint + sorted query
params). `time.monotonic()` gates the TTL so a wall-clock step can't wedge or
flush the cache; `cached_at` in the returned payload is wall-clock, for the
page to display.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Callable

_lock = threading.Lock()                          # guards the two tables below
_entries: dict[tuple, tuple[float, dict]] = {}     # key -> (monotonic computed_at, payload)
_locks: dict[tuple, threading.Lock] = {}           # key -> single-flight lock
# ponytail: _locks never evicts. Key space is a handful of dashboard endpoints
# x query-param combos, not user-cardinality, so this never grows unbounded
# in practice. Add a bound if that stops being true.


def _fresh(hit, ttl_secs: float, now: float):
    if hit is None:
        return None
    computed_at, payload = hit
    if now - computed_at >= ttl_secs:
        return None
    return {**payload, "age_secs": round(now - computed_at, 3)}


def get_or_compute(key: tuple, ttl_secs: float, compute: Callable[[], dict]) -> dict:
    """Return the cached payload for `key`, recomputing via `compute()` if
    missing or older than `ttl_secs`. Concurrent callers for the same key
    block on one `compute()` call and share its result. `compute` must return
    a dict; a raised exception is not cached, so the next caller retries."""
    now = time.monotonic()
    with _lock:
        fresh = _fresh(_entries.get(key), ttl_secs, now)
        if fresh is not None:
            return fresh
        klock = _locks.setdefault(key, threading.Lock())
    with klock:
        # a rival may have refreshed this key while we waited for klock
        now = time.monotonic()
        with _lock:
            fresh = _fresh(_entries.get(key), ttl_secs, now)
        if fresh is not None:
            return fresh
        result = compute()
        payload = {**result,
                   "cached_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        with _lock:
            _entries[key] = (time.monotonic(), payload)
        return {**payload, "age_secs": 0.0}
