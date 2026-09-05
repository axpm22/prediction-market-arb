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
# its original relative position. Operator 2026-09-05: headline (P&L +
# general stats) first, then ROI/exits, open positions, traders, errors, and
# the supervisor lamp strip last (supersedes 2026-09-04's whale1/whale2/
# positions/lamps/errsec order).
WHALE_FRONT = ("headline", "whale1", "positions", "whale2", "errsec", "lamps")

# arb.watch logs every venue fetch under one logger, name embedded in the
# message ("venue fetch failed: sx: 400 from ..."); these loggers instead
# carry the venue in their own name. arb.oddsapi is NOT here — it's never
# filtered at all (rule 2, see _keep_error_line) and oddsapi can never be a
# member of watch_venues (the config loader restricts that to kalshi/
# polymarket/sx), so routing it through this venue check hid it forever.
_VENUE_LOGGERS = {"arb.kalshi": "kalshi", "arb.sx": "sx",
                   "arb.polymarket": "polymarket"}
_WATCH_VENUE = re.compile(r"venue fetch \w+: (\w+):")

# Logger name -> supervisor.processes lane, explicit rather than guessed from
# the dotted name: arb.kalshi_events (nonsports/kalshi_events.py) IS the
# negrisk lane's non-sports leg, but "kalshi_events" is not a processes key,
# so a name-derived guess let it through even with negrisk disabled.
_LANE_LOGGERS = {"arb.negrisk": "negrisk", "arb.kalshi_events": "negrisk",
                  "arb.consensus": "scan"}
_LANE_PREFIXES = (("arb.political", "political"), ("arb.mapped", "mapped"),
                   ("arb.verify", "verify"), ("arb.live", "live"),
                   ("arb.scan", "scan"))

# lanes core to whale mode: always visible in the errors panel regardless of
# supervisor.processes (M6, review of 798aae6: was inline in _keep_error_line).
_WHALE_CORE = ("watch", "whale", "whale-backfill")


def _lane_of(source: str) -> str | None:
    """The supervisor.processes lane `source` belongs to, or None when it
    can't be classified. Callers must KEEP a line whose lane is None — a
    hidden real error is worse than a noisy unclassified one."""
    if source in _LANE_LOGGERS:
        return _LANE_LOGGERS[source]
    for prefix, lane in _LANE_PREFIXES:
        if source.startswith(prefix):
            return lane
    return None


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
    a lane supervisor.processes has switched off. arb.whale*/arb.supervisor/
    arb.oddsapi lines (rule 2: the Odds API low-credit WARNING must always be
    visible), arb.watch lines about an enabled venue, and any logger this
    module can't classify into a lane, are never dropped. child:<name> maps
    to lane <name> (capture filename IS the processes key, or an unlisted
    clock command that is therefore never "disabled"); watch/whale/whale-
    backfill are core to whale mode and stay visible even if switched off."""
    if source.startswith("arb.whale") or source in ("arb.supervisor", "arb.oddsapi"):
        return True
    if source.startswith("child:"):
        name = source[len("child:"):]
        return name in _WHALE_CORE or name not in _disabled_lanes(cfg)
    if source == "arb.watch":
        m = _WATCH_VENUE.search(msg)
        return m is None or m.group(1) in cfg["watch_venues"]
    venue = _VENUE_LOGGERS.get(source)
    if venue is not None:
        return venue in cfg["watch_venues"]
    lane = _lane_of(source)
    return lane is None or lane not in _disabled_lanes(cfg)


def filter_errors(cfg, errors: list[dict]) -> list[dict]:
    """Full mode: no filtering (unchanged today)."""
    if str(cfg["dashboard"]["mode"]) != "whale":
        return errors
    return [e for e in errors if _keep_error_line(cfg, e["source"], e["msg"])]


def tail_child_errors(cfg) -> list[dict]:
    """Traceback/Error lines from arb/heartbeat.py's per-child capture files
    (supervisor.child_log_dir) — `redact` again here too, defence in depth on
    top of arb/child_log.py's drain thread already redacting on the way to
    disk. Filenames are date-stamped (M8: <name>.<YYYY-MM-DD>.log, so an
    active lane's file goes inactive daily and log_retention_days' purge can
    reclaim it) — several dated files can exist for one child name within
    retention, so read only the NEWEST per name, by mtime (a lexical
    filename compare always picked a legacy undated <name>.log over
    <name>.<date>.log — review of 70058b9, CRITICAL)."""
    from datetime import datetime, timezone

    from arb.logutil import redact, resolve_log_path
    d = resolve_log_path(cfg["supervisor"]["child_log_dir"])
    if not d.is_dir():
        return []
    newest: dict[str, Path] = {}
    for p in d.glob("*.log"):
        name = p.name.rsplit(".", 2)[0]  # "<name>.<date>.log" -> "<name>"
        if name not in newest or p.stat().st_mtime > newest[name].stat().st_mtime:
            newest[name] = p
    now_iso = datetime.now(timezone.utc).isoformat()
    out = []
    for name, p in sorted(newest.items()):
        with p.open("rb") as f:
            f.seek(max(0, p.stat().st_size - 65536))
            lines = f.read().decode("utf-8", "replace").splitlines()
        out += [{"at": now_iso, "level": "ERROR", "source": f"child:{name}",
                "msg": redact(ln)[:300], "file": f"children/{p.name}"}
               for ln in lines if "Traceback" in ln or "Error" in ln]
    return out


def lane_timing(cfg, store, day_start: str) -> list[dict]:
    """Per-lane cadence: configured interval vs last poll actually persisted.
    `enabled` labels a lane supervisor.processes has switched off — its
    history can still show (e.g. after a mode change) without pretending
    it's running today."""
    from arb.heartbeat import lane_interval
    last = {r["kind"]: dict(r) for r in store.query(
        """SELECT kind, MAX(started_at) last_poll, COUNT(*) polls_today,
                  COALESCE(SUM(n_quotes),0) quotes_today
           FROM polls WHERE started_at >= ? GROUP BY kind""", (day_start,))}
    procs = cfg["supervisor"]["processes"]
    lanes = [k for k, v in procs.items() if v]
    lanes += [k for k in last if k not in lanes]
    return [{"name": k, "enabled": bool(procs.get(k, False)),
             "interval_secs": lane_interval(cfg, k),
             "last_poll": (last.get(k) or {}).get("last_poll"),
             "polls_today": (last.get(k) or {}).get("polls_today", 0),
             "quotes_today": (last.get(k) or {}).get("quotes_today", 0)}
            for k in lanes]
