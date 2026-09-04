"""The primary watch cycle (SPEC §13.1). Venue fetches run CONCURRENTLY (threads),
each under venue_fetch_timeout_secs — sequential fetching self-inflicts inter-leg
skew against the 10s actionable bound and is forbidden."""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

from arb.logutil import get_logger
from arb.venues import kalshi, polymarket, sx
from arb.venues.base import PartialFetch

log = get_logger("arb.watch", "logs/scan.log")

VENUES = {
    "kalshi": kalshi.fetch_markets,
    "polymarket": polymarket.fetch_markets,
    "sx": sx.fetch_markets,
}


def fetch_all_venues(cfg) -> tuple[dict, dict]:
    """-> (quotes_by_venue, status_by_venue). A venue that fails marks the poll
    partial for that venue (§10.2) — it must never mass-close lifetimes.

    A venue not listed in config `watch_venues` (e.g. kalshi, dropped 2026-09-04:
    whale-only mode's watch lane exists only to name the Polymarket universe,
    and nothing reads kalshi's ~1.3M quote rows/day) is neither fetched nor
    threaded. It reports exactly the shape a fetch failure already takes —
    ok:False, n:0, empty quotes — plus `skipped: True` so a consumer that wants
    to tell "config says skip" apart from "venue is down" can, while everything
    else (§10.2's never-mass-close path) treats it identically to an outage."""
    active = list(cfg["watch_venues"])
    quotes, status = {}, {}
    for name in VENUES:
        if name not in active:
            quotes[name] = []
            status[name] = {"ok": False, "skipped": True, "n": 0,
                            "latency_ms": 0.0, "error": "not in watch_venues"}

    def one(name):
        t0 = time.monotonic()
        try:
            qs = VENUES[name](cfg)
            return name, qs, {"ok": True, "n": len(qs),
                              "latency_ms": (time.monotonic() - t0) * 1000}
        except PartialFetch as e:
            # truncated by the per-venue deadline (H7): KEEP the collected quotes
            # (rule 7) but mark not-ok so §10.2 never mass-closes lifetimes.
            log.warning("venue fetch truncated: %s: %s partial quotes",
                        name, len(e.quotes))
            return name, e.quotes, {"ok": False, "partial": True, "n": len(e.quotes),
                                    "latency_ms": (time.monotonic() - t0) * 1000,
                                    "error": "deadline"}
        except Exception as e:  # noqa: BLE001 — a venue outage is data, not a crash
            log.warning("venue fetch failed: %s: %s", name, e)
            return name, [], {"ok": False, "n": 0,
                              "latency_ms": (time.monotonic() - t0) * 1000,
                              "error": str(e)}

    with ThreadPoolExecutor(max_workers=len(active)) as pool:
        for name, qs, st in pool.map(one, active):
            quotes[name], status[name] = qs, st
    return quotes, status
