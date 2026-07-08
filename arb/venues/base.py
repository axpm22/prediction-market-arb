"""Shared HTTP for venue adapters (SPEC §13.1 fetch discipline): per-call timeout,
exponential backoff with jitter on 429/5xx, at most venue_max_retries retries.
Only public unauthenticated GETs/POSTs (rule 1)."""
from __future__ import annotations

import random
import time

import requests

from arb.logutil import get_logger

log = get_logger("arb.venues", "logs/scan.log")


class VenueError(Exception):
    pass


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
            resp = requests.request(method, url, timeout=timeout, **kw)
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
