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
LRU (`OrderedDict`, oldest-first) with a HARD cap: once it holds more than
`max_entries`, inserting a new key evicts the least-recently-used ones until
it is back at the cap, in-flight or not (a compute still running simply
re-inserts its key when it finishes). Single-flight locks are a fixed pool
of `_STRIPES` locks indexed by `hash(key)` (re-review of a0b94ff): a
per-key lock table that evicted its rows could drop a lock a caller already
held a reference to and orphan two concurrent computes for one key; a
fixed stripe pool is bounded by construction and can never be evicted from
under anyone. Two different keys that share a stripe serialise — harmless
for a monitor panel.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Callable

_STRIPES = 64                                     # fixed single-flight lock pool (see module doc)
_lock = threading.Lock()                          # guards `_entries`
_entries: "OrderedDict[tuple, tuple[float, dict]]" = OrderedDict()
                                                    # key -> (monotonic computed_at, payload)
                                                    # ordered oldest-used -> newest-used
_stripes = [threading.Lock() for _ in range(_STRIPES)]


def _stripe(key: tuple) -> threading.Lock:
    return _stripes[hash(key) % _STRIPES]


def _fresh(hit, ttl_secs: float, now: float):
    if hit is None:
        return None
    computed_at, payload = hit
    if now - computed_at >= ttl_secs:
        return None
    return {**payload, "age_secs": round(now - computed_at, 3)}


def _evict_over_cap(max_entries: int) -> None:
    """Call with `_lock` held, after inserting into `_entries`. Drops
    oldest-first until back at the cap — a hard bound. An evicted key whose
    compute() is still in flight elsewhere loses nothing: that compute
    re-inserts its result when it finishes, and its stripe lock still
    serialises rivals for the same key."""
    while len(_entries) > max_entries:
        _entries.popitem(last=False)


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
    klock = _stripe(key)
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
