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

`/api/whale/traders?wallet=` folds the caller-supplied wallet into the key,
so the key space is NOT bounded by endpoint count — it's bounded by however
many distinct wallets get looked up (review of 9e5951a). `_entries` is an
LRU (`OrderedDict`, oldest-first); once it holds more than `max_entries`,
inserting a new key evicts the least-recently-used one, dropping its
`_locks` row along with it, so neither table outlives its usefulness.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Callable

_lock = threading.Lock()                          # guards the two tables below
_entries: "OrderedDict[tuple, tuple[float, dict]]" = OrderedDict()
                                                    # key -> (monotonic computed_at, payload)
                                                    # ordered oldest-used -> newest-used
_locks: dict[tuple, threading.Lock] = {}           # key -> single-flight lock


def _fresh(hit, ttl_secs: float, now: float):
    if hit is None:
        return None
    computed_at, payload = hit
    if now - computed_at >= ttl_secs:
        return None
    return {**payload, "age_secs": round(now - computed_at, 3)}


def _evict_over_cap(max_entries: int) -> None:
    """Call with `_lock` held, after inserting into `_entries`. Drops
    oldest-first until at the cap, skipping any key whose lock is currently
    `.locked()` — that means a compute() for it is in flight elsewhere, and
    dropping its lock would let a fresh caller start a second, concurrent
    compute for the same key rather than joining the one running."""
    if len(_entries) <= max_entries:
        return
    for stale_key in list(_entries.keys()):
        if len(_entries) <= max_entries:
            return
        klock = _locks.get(stale_key)
        if klock is not None and klock.locked():
            continue
        del _entries[stale_key]
        _locks.pop(stale_key, None)


def get_or_compute(key: tuple, ttl_secs: float, compute: Callable[[], dict],
                    max_entries: int = 256) -> dict:
    """Return the cached payload for `key`, recomputing via `compute()` if
    missing or older than `ttl_secs`. Concurrent callers for the same key
    block on one `compute()` call and share its result. `compute` must return
    a dict; a raised exception is not cached and propagates to the caller, so
    the next caller retries. `_entries` is capped at `max_entries` (LRU)."""
    now = time.monotonic()
    with _lock:
        fresh = _fresh(_entries.get(key), ttl_secs, now)
        if fresh is not None:
            _entries.move_to_end(key)
            return fresh
        klock = _locks.setdefault(key, threading.Lock())
    with klock:
        # a rival may have refreshed this key while we waited for klock
        now = time.monotonic()
        with _lock:
            fresh = _fresh(_entries.get(key), ttl_secs, now)
            if fresh is not None:
                _entries.move_to_end(key)
        if fresh is not None:
            return fresh
        result = compute()
        payload = {**result,
                   "cached_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        with _lock:
            _entries[key] = (time.monotonic(), payload)
            _entries.move_to_end(key)
            _evict_over_cap(max_entries)
        return {**payload, "age_secs": 0.0}
