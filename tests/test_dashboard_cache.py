"""arb/dashboard_cache.py: single-flight TTL memo behind /api/whale and
/api/whale/traders (live 2026-09-04: concurrent requests each recomputing the
whale frame piled up CPU and ledger handles faster than the page's 60s
refresh could drain them)."""
import threading
import time

from arb import dashboard_cache as dc


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
