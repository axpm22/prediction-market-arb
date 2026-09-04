"""arb/dashboard_cache.py: single-flight TTL memo behind /api/whale and
/api/whale/traders (live 2026-09-04: concurrent requests each recomputing the
whale frame piled up CPU and ledger handles faster than the page's 60s
refresh could drain them)."""
import threading
import time

from arb import dashboard_cache as dc


import pytest as _pytest


@_pytest.fixture(autouse=True)
def _isolate_cache():
    # the memo is process-global; tests with tiny max_entries would otherwise
    # evict each other's rows and depend on run order (flaked under full-suite
    # load, 2026-09-04)
    import arb.dashboard_cache as dc
    with dc._lock:
        dc._entries.clear()
    yield
    with dc._lock:
        dc._entries.clear()


def test_concurrent_requests_share_one_computation():
    calls = []

    def compute():
        calls.append(1)
        time.sleep(0.05)
        return {"v": len(calls)}

    results = [None, None]

    def worker(i):
        results[i] = dc.get_or_compute(("concurrent-key",), 60, compute)

    t1 = threading.Thread(target=worker, args=(0,))
    t2 = threading.Thread(target=worker, args=(1,))
    t1.start()
    time.sleep(0.01)  # let t1 win the race into compute() before t2 starts
    t2.start()
    t1.join()
    t2.join()

    assert len(calls) == 1                      # compute ran exactly once
    assert results[0]["v"] == results[1]["v"] == 1
    assert results[0]["cached_at"] == results[1]["cached_at"]  # same computation


def test_stale_entry_recomputes_after_ttl():
    calls = []

    def compute():
        calls.append(1)
        return {"v": len(calls)}

    first = dc.get_or_compute(("ttl-key",), 0.05, compute)
    time.sleep(0.06)
    second = dc.get_or_compute(("ttl-key",), 0.05, compute)

    assert len(calls) == 2
    assert (first["v"], second["v"]) == (1, 2)


def test_different_params_are_different_keys():
    calls = []

    def compute():
        calls.append(1)
        return {"v": len(calls)}

    a = dc.get_or_compute(("params-key", "a"), 60, compute)
    b = dc.get_or_compute(("params-key", "b"), 60, compute)

    assert len(calls) == 2
    assert (a["v"], b["v"]) == (1, 2)


def test_fresh_hit_is_not_recomputed_and_carries_age_secs():
    calls = []

    def compute():
        calls.append(1)
        return {"v": len(calls)}

    dc.get_or_compute(("fresh-key",), 60, compute)
    hit = dc.get_or_compute(("fresh-key",), 60, compute)

    assert len(calls) == 1
    assert hit["v"] == 1
    assert hit["age_secs"] >= 0


def test_lru_evicts_least_recently_used_entry_on_insert():
    # review of 9e5951a: /api/whale/traders folds the unvalidated `wallet=`
    # query param into the cache key, so every distinct wallet used to add a
    # permanent _entries row and a permanent _locks row. Cap it: past
    # max_entries, inserting a new key evicts the least-recently-used one —
    # entry AND lock — so a never-expiring TTL still can't make the tables
    # grow without bound.
    calls = {}

    def compute(tag):
        calls[tag] = calls.get(tag, 0) + 1
        return {"v": calls[tag]}

    dc.get_or_compute(("lru-key", "a"), 3600, lambda: compute("a"), max_entries=2)
    dc.get_or_compute(("lru-key", "b"), 3600, lambda: compute("b"), max_entries=2)
    # "a" is now the least-recently-used of the two; inserting "c" evicts it
    dc.get_or_compute(("lru-key", "c"), 3600, lambda: compute("c"), max_entries=2)

    # "a" was evicted -> still "fresh" by TTL alone, but the entry (and its
    # lock) are gone, so this call must recompute rather than hit the cache
    hit_a = dc.get_or_compute(("lru-key", "a"), 3600, lambda: compute("a"), max_entries=2)
    assert calls["a"] == 2

    # "c" was the most recently inserted -> still cached, no recompute
    hit_c = dc.get_or_compute(("lru-key", "c"), 3600, lambda: compute("c"), max_entries=2)
    assert calls["c"] == 1
    assert hit_c["age_secs"] >= 0
    assert hit_a["v"] == 2


def test_lru_touches_recency_on_hit_not_just_insert():
    calls = {}

    def compute(tag):
        calls[tag] = calls.get(tag, 0) + 1
        return {"v": calls[tag]}

    dc.get_or_compute(("lru-touch", "a"), 3600, lambda: compute("a"), max_entries=2)
    dc.get_or_compute(("lru-touch", "b"), 3600, lambda: compute("b"), max_entries=2)
    # re-fetch "a" (a cache hit) so it becomes the most-recently-used
    dc.get_or_compute(("lru-touch", "a"), 3600, lambda: compute("a"), max_entries=2)
    # now "b" is the least-recently-used; inserting "c" should evict "b", not "a"
    dc.get_or_compute(("lru-touch", "c"), 3600, lambda: compute("c"), max_entries=2)

    dc.get_or_compute(("lru-touch", "a"), 3600, lambda: compute("a"), max_entries=2)
    assert calls["a"] == 1  # "a" was never evicted, so still one compute

    dc.get_or_compute(("lru-touch", "b"), 3600, lambda: compute("b"), max_entries=2)
    assert calls["b"] == 2  # "b" was evicted, so this is a recompute


def test_exception_in_compute_is_not_cached_and_releases_lock():
    calls = []

    def compute():
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("boom")
        return {"v": len(calls)}

    try:
        dc.get_or_compute(("error-key",), 60, compute)
        assert False, "expected the ValueError to propagate"
    except ValueError:
        pass

    # nothing was cached from the failed call
    second = dc.get_or_compute(("error-key",), 60, compute)
    assert len(calls) == 2
    assert second["v"] == 2


def test_cap_is_hard_even_while_a_compute_is_in_flight():
    # re-review of a0b94ff: the old per-key lock table skipped locked keys on
    # eviction, so the cap was soft under concurrent refreshes. Now `_entries`
    # is a hard LRU bound and single-flight uses a fixed stripe pool.
    import threading
    import arb.dashboard_cache as dc
    started, release = threading.Event(), threading.Event()

    def slow():
        started.set()
        release.wait(20)
        return {"v": "slow"}

    t = threading.Thread(target=lambda: dc.get_or_compute(("hard", "slow"), 3600, slow, max_entries=2))
    t.start()
    assert started.wait(20)
    for i in range(6):                       # six inserts while the slow compute holds its stripe
        dc.get_or_compute(("hard", i), 3600, lambda i=i: {"v": i}, max_entries=2)
    with dc._lock:
        assert sum(1 for k in dc._entries if k[0] == "hard") <= 2
    release.set()
    t.join(20)
    with dc._lock:                           # the slow compute re-inserted itself, still capped
        assert sum(1 for k in dc._entries if k[0] == "hard") <= 2
    assert dc.get_or_compute(("hard", "slow"), 3600, lambda: {"v": "no"}, max_entries=2)["v"] == "slow"


def test_single_flight_survives_eviction_of_the_key():
    # the race the re-review demonstrated: eviction dropped a key's lock while a
    # caller held a reference to it, and a rival minted a second lock -> two
    # computes. With stripes, the second caller for the same key must wait.
    import threading
    import arb.dashboard_cache as dc
    calls, started, release = [], threading.Event(), threading.Event()

    def compute():
        calls.append(1)
        started.set()
        release.wait(20)
        return {"v": len(calls)}

    key = ("sf", "same")
    a = threading.Thread(target=lambda: dc.get_or_compute(key, 3600, compute, max_entries=1))
    a.start()
    assert started.wait(20)
    dc.get_or_compute(("sf", "other"), 3600, lambda: {"v": 0}, max_entries=1)   # evicts anything else
    b = threading.Thread(target=lambda: dc.get_or_compute(key, 3600, compute, max_entries=1))
    b.start()
    release.set()
    a.join(20); b.join(20)
    assert calls == [1], "the rival started a second compute for the same key"
