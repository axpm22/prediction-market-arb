"""Cross-process request pacing. The venue rate limit is per IP, and the
supervisor runs each lane as its own child process, so pacing has to be shared
across processes or it coordinates nothing."""
import multiprocessing as mp
import os
import time
from pathlib import Path

import pytest

from arb.config import load
from arb.venues.base import _pace_global

ROOT = Path(__file__).resolve().parent.parent
CFG = load(ROOT / "config.yaml")


@pytest.fixture(autouse=True)
def _enable_pacing(monkeypatch):
    """conftest disables pacing suite-wide (rule 2: no live calls). These tests
    exist to prove the pacer works, so they opt back in."""
    monkeypatch.delenv("ARB_PACE_DISABLED", raising=False)


def _cfg(rate):
    c = dict(CFG)
    c["execution"] = {**CFG["execution"], "max_requests_per_sec": rate}
    return c


def test_pacing_spaces_calls_in_one_process(tmp_path, monkeypatch):
    import arb.venues.base as base
    monkeypatch.setattr(base, "PACE_FILE", tmp_path / "pace")
    cfg = _cfg(20.0)                      # 50ms apart
    t0 = time.time()
    for _ in range(5):
        base._pace_global(cfg)
    assert time.time() - t0 >= 0.15, "5 calls at 20/s must take at least ~0.2s"


def _burst(pace_file, rate, n, q):
    os.environ.pop("ARB_PACE_DISABLED", None)   # child process inherits the env
    import arb.venues.base as base
    base.PACE_FILE = Path(pace_file)
    c = dict(CFG); c["execution"] = {**CFG["execution"], "max_requests_per_sec": rate}
    t0 = time.time()
    for _ in range(n):
        base._pace_global(c)
    q.put(time.time() - t0)


def test_pacing_is_shared_ACROSS_processes(tmp_path):
    """The whole point. Two lanes pacing themselves independently still burst
    two-way at the venue; sharing the file makes them queue behind each other."""
    pf = str(tmp_path / "pace")
    q = mp.Queue()
    procs = [mp.Process(target=_burst, args=(pf, 10.0, 5, q)) for _ in range(2)]
    t0 = time.time()
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
    elapsed = time.time() - t0
    # 10 calls total at 10/s must take ~0.9s even though they run concurrently
    assert elapsed >= 0.7, f"processes did not queue: {elapsed:.2f}s"


def test_rate_zero_disables_pacing(tmp_path, monkeypatch):
    import arb.venues.base as base
    monkeypatch.setattr(base, "PACE_FILE", tmp_path / "pace")
    t0 = time.time()
    for _ in range(50):
        base._pace_global(_cfg(0))
    assert time.time() - t0 < 0.2


def test_missing_lock_dir_does_not_break_fetching(tmp_path, monkeypatch):
    """Pacing is a courtesy, not a gate: if the lock file cannot be created the
    request must still go out rather than the lane dying."""
    import arb.venues.base as base
    monkeypatch.setattr(base, "PACE_FILE", Path("/proc/nonexistent/pace"))
    base._pace_global(_cfg(20.0))          # must not raise
