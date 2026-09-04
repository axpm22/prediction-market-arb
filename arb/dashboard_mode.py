"""dashboard.mode layout/filtering helpers, split out of dashboard.py to keep
the HTTP handler under ~300 lines (fix pass on 5c9bbb5). Everything here is
about WHAT whale mode shows, never how the page is served — dashboard.py
calls in thin."""
from __future__ import annotations

import re

# dashboard.mode (2026-09-04 operator: PM whales only, for now — SCHEMA/
# _check_invariants in arb/config.py restrict the value to these two keys).
TITLES = {"whale": "Whale lane — instrument panel",
          "full": "ArbitrageModel — instrument panel"}
# blocks moved to the top, in order, when mode is whale — everything else
# (including the arb-only pieces the page's own templated MODE hides) keeps
# its original relative position.
WHALE_FRONT = ("whale1", "whale2", "positions", "lamps", "errsec")

# arb.watch logs every venue fetch under one logger, name embedded in the
# message ("venue fetch failed: sx: 400 from ..."); these loggers instead
# carry the venue in their own name.
_VENUE_LOGGERS = {"arb.kalshi": "kalshi", "arb.sx": "sx",
                   "arb.polymarket": "polymarket", "arb.oddsapi": "oddsapi"}
_WATCH_VENUE = re.compile(r"venue fetch \w+: (\w+):")


def extract_marked(html: str, name: str) -> tuple[str, str]:
    """Pull one `<!--WM:name-->...<!--/WM-->` block out of dashboard.html —
    returns (inner_html, html_with_the_marked_region_removed). Raises if the
    marker is missing rather than silently dropping a section (rule 3 style)."""
    m = re.search(rf"<!--WM:{name}-->(.*?)<!--/WM-->", html, re.DOTALL)
    if not m:
        raise ValueError(f"dashboard.html: missing marker WM:{name}")
    return m.group(1).strip(), html[:m.start()] + html[m.end():]


def whale_first(html: str) -> str:
    """Cut-and-splice the WHALE_FRONT blocks to right after `<header>`. A
    second HTML file would double-maintain the whole page; `dashboard.mode:
    full` never calls this function, so today's layout can never regress."""
    blocks = []
    for name in WHALE_FRONT:
        block, html = extract_marked(html, name)
        blocks.append(block)
    return html.replace("</header>", "</header>\n" + "\n".join(blocks), 1)


def _disabled_lanes(cfg) -> set[str]:
    """supervisor.processes keys switched off, plus `scan` when scan_times is
    empty (§13.1's Odds API scan has no processes flag of its own)."""
    disabled = {k for k, v in cfg["supervisor"]["processes"].items() if not v}
    if not cfg["scan_times"]:
        disabled.add("scan")
    return disabled


def _keep_error_line(cfg, source: str, msg: str) -> bool:
    """Whale mode only: drop a log line about a venue outside watch_venues or
    a lane supervisor.processes has switched off. arb.whale*/arb.supervisor
    lines, and arb.watch lines about an enabled venue, are never dropped."""
    if source.startswith("arb.whale") or source == "arb.supervisor":
        return True
    if source == "arb.watch":
        m = _WATCH_VENUE.search(msg)
        return m is None or m.group(1) in cfg["watch_venues"]
    venue = _VENUE_LOGGERS.get(source)
    if venue is not None:
        return venue in cfg["watch_venues"]
    lane = source.split(".", 1)[1] if source.startswith("arb.") else source
    return lane not in _disabled_lanes(cfg)


def filter_errors(cfg, errors: list[dict]) -> list[dict]:
    """Full mode: no filtering (unchanged today)."""
    if str(cfg["dashboard"]["mode"]) != "whale":
        return errors
    return [e for e in errors if _keep_error_line(cfg, e["source"], e["msg"])]
