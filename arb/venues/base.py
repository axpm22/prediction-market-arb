"""Shared HTTP for venue adapters (SPEC §13.1 fetch discipline): per-call timeout,
exponential backoff with jitter on 429/5xx, at most venue_max_retries retries.
Only public unauthenticated GETs/POSTs (rule 1)."""
from __future__ import annotations

import random
import threading
import time

import requests

from arb.logutil import get_logger

log = get_logger("arb.venues", "logs/scan.log")

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
    """Per-venue wall-clock budget, checked between pagination/chunk requests."""

    def __init__(self, secs: float):
        self._end = time.monotonic() + float(secs)

    def expired(self) -> bool:
        return time.monotonic() >= self._end


def is_live(start_utc, now=None) -> bool:
    """One shared rule (LIVE lane A1): live = start known AND start_utc <= now.
    Start-unknown is never live (non-actionable everywhere, unchanged). Adapters
    never FILTER live quotes — rule 7 persists everything."""
    from datetime import datetime, timezone
    if start_utc is None:
        return False
    return start_utc <= (now or datetime.now(timezone.utc))


def _request(method: str, url: str, cfg, **kw):
    timeout = float(cfg["execution"]["venue_fetch_timeout_secs"])
    retries = int(cfg["execution"]["venue_max_retries"])
    last = None
    for attempt in range(retries + 1):
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
        except (requests.RequestException, ValueError) as e:
            last = e
        if attempt < retries:
            time.sleep((2 ** attempt) * 0.5 + random.uniform(0, 0.3))
    raise VenueError(str(last))


def get_json(url: str, cfg, headers=None):
    return _request("GET", url, cfg, headers=headers)


def post_json(url: str, cfg, payload):
    return _request("POST", url, cfg, json=payload)
