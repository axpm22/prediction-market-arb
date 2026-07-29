"""Shared HTTP for venue adapters (SPEC §13.1 fetch discipline): per-call timeout,
exponential backoff with jitter on 429/5xx, at most venue_max_retries retries.
Only public unauthenticated GETs/POSTs (rule 1)."""
from __future__ import annotations

import fcntl
import os
import random
import threading
import time
from pathlib import Path

import requests

from arb.logutil import get_logger

log = get_logger("arb.venues", "logs/scan.log")

# Non-retryable TLS failures (ANJ regulator DNS poisoning serves a mismatched
# cert, first seen 2026-07-16): a cert error cannot succeed on retry, so the
# host fast-fails for execution.tls_block_cache_secs. Cache, not ban — the
# host is re-probed after the window, so recovery needs no restart. Lock
# because Phase 6 fans get_json out across threads.
_tls_block: dict = {}  # host -> monotonic expiry
_tls_lock = threading.Lock()

# One requests.Session per THREAD (H7): the fetch fan-out makes dozens of
# sequential GETs per venue; without a Session each paid a fresh TCP+TLS
# handshake. Sessions are not thread-safe to share, so keep one per thread.
_local = threading.local()


def _session() -> requests.Session:
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        _local.session = s
    return s


class VenueError(Exception):
    pass


class PartialFetch(VenueError):
    """Raised when a per-venue deadline truncates a fetch (H7). Carries the
    quotes collected so far so the poll persists them (rule 7) while the venue
    is marked not-ok, so §10.2 lifetime-carry never mass-closes on truncation."""

    def __init__(self, quotes):
        super().__init__("venue deadline exceeded; partial fetch")
        self.quotes = quotes


class Deadline:
    """Per-venue wall-clock budget, checked between pagination/chunk requests
    AND inside _request (trims the per-call timeout, skips retries)."""

    def __init__(self, secs: float):
        self._end = time.monotonic() + float(secs)

    def expired(self) -> bool:
        return time.monotonic() >= self._end

    def remaining(self) -> float:
        return self._end - time.monotonic()


def is_live(start_utc, now=None) -> bool:
    """One shared rule (LIVE lane A1): live = start known AND start_utc <= now.
    Start-unknown is never live (non-actionable everywhere, unchanged). Adapters
    never FILTER live quotes — rule 7 persists everything."""
    from datetime import datetime, timezone
    if start_utc is None:
        return False
    return start_utc <= (now or datetime.now(timezone.utc))


# Cross-PROCESS request pacing. The rate limit is per IP, not per process, and
# the supervisor runs each lane as its own one-shot child, so an in-process
# token bucket coordinates nothing — six lanes each politely pacing themselves
# still burst six-way at the venue. Measured 2026-07-29: a bare loop at ~3 req/s
# took a 429 within two calls, while a paced client held ~2.3 req/s across
# 47,000 requests with 3 total 429s.
#
# One file, one flock, one timestamp. A caller holds the lock across its wait so
# every other process queues behind it — that serialisation IS the pacing. The
# lock is released by the OS if a holder dies, so a crashed lane cannot wedge
# the fleet.
PACE_FILE = Path(".locks/request_pace")


def _pace_global(cfg) -> None:
    # Rule 2: tests never make live venue calls, so pacing there is pure
    # flakiness — it sleeps real seconds and is indistinguishable from the
    # retry backoff ladder that timing tests assert on. Same env-var plumbing
    # convention as ARB_LOG_DIR in tests/conftest.py.
    if os.environ.get("ARB_PACE_DISABLED"):
        return
    # missing key => disabled, never a crash: pacing is a courtesy to the venue,
    # and a partial cfg must not take a fetch down with it
    rate = float(cfg.get("execution", {}).get("max_requests_per_sec", 0) or 0)
    if rate <= 0:
        return
    gap = 1.0 / rate
    try:
        PACE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(PACE_FILE, "a+") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                fh.seek(0)
                last = float(fh.read().strip() or 0.0)
                now = time.time()
                wait = last + gap - now
                if wait > 0:
                    time.sleep(wait)
                    now = last + gap
                fh.seek(0)
                fh.truncate()
                fh.write(repr(now))
                fh.flush()
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError as e:
        log.warning("request pacing unavailable (%s) — proceeding unpaced", e)


def _request(method: str, url: str, cfg, deadline=None, **kw):
    from urllib.parse import urlsplit
    host = urlsplit(url).hostname or url
    with _tls_lock:
        expiry = _tls_block.get(host)
        if expiry is not None and time.monotonic() < expiry:
            raise VenueError(f"{host} TLS-blocked (fast-fail cache)")
        if expiry is not None:
            del _tls_block[host]  # window over: probe again
    timeout = float(cfg["execution"]["venue_fetch_timeout_secs"])
    retries = int(cfg["execution"]["venue_max_retries"])
    last = None
    for attempt in range(retries + 1):
        _pace_global(cfg)      # every outbound venue call, retries included
        if deadline is not None:
            remaining = deadline.remaining()
            if remaining <= 0:
                break  # venue budget spent: a retry ladder can't help (H7)
            timeout = min(timeout, remaining)
        try:
            resp = _session().request(method, url, timeout=timeout, **kw)
            if resp.status_code == 429 or resp.status_code >= 500:
                last = VenueError(f"{resp.status_code} from {url.split('?')[0]}")
            elif resp.status_code >= 400:
                # rule 3: 404/4xx means stop and report, never guess alternatives
                raise VenueError(f"{resp.status_code} from {url.split('?')[0]} — "
                                 "endpoint drift? stop and report (rule 3)")
            else:
                return resp.json()
        except requests.exceptions.SSLError as e:
            # cert mismatch never heals on retry: block the host, one log
            # line per host per window (logged only here, at block set)
            with _tls_lock:
                _tls_block[host] = time.monotonic() + \
                    float(cfg["execution"]["tls_block_cache_secs"])
            log.warning("%s: non-retryable TLS failure — venue blocked? "
                        "(regulator/MITM): %s", host, e)
            raise VenueError(f"TLS failure from {host}: {e}") from e
        except (requests.RequestException, ValueError) as e:
            last = e
        if attempt < retries and (deadline is None or not deadline.expired()):
            time.sleep((2 ** attempt) * 0.5 + random.uniform(0, 0.3))
    raise VenueError(str(last) if last is not None else
                     f"venue deadline expired before request to {url.split('?')[0]}")


def get_json(url: str, cfg, headers=None, deadline=None):
    return _request("GET", url, cfg, deadline=deadline, headers=headers)


def post_json(url: str, cfg, payload, deadline=None):
    return _request("POST", url, cfg, deadline=deadline, json=payload)
