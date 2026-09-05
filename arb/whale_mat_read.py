"""matpos READ side, split out of arb/whale_mat.py on size (2026-09-05).

Owns every READ of the two cache tables and their staleness: `load_positions`
(positions_mat -> `Position` objects), `load_hygiene` (hygiene_mat ->
`Hygiene`), `freshness`/`warn_if_stale` (how current the cache is) and
`cache_partial` (whether a full rebuild is still draining). `POSITION_COLS`/
`POSITION_ORDER` describe `positions_mat`'s shape and are shared with
`whale_mat.py`'s own INSERT, which imports them from here.

`arb/whale_mat.py` owns the WRITE side — the DDL, the dirty queue, and
`materialize`/`_wallet` which fill these same two tables — and re-exports
every name below so existing `whale_mat.load_positions` etc. callers are
unaffected by the split.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from arb.logutil import get_logger
from arb.whale_hygiene import Hygiene
from arb.whale_score import Position
from arb.whale_tape import _meta

log = get_logger("arb.whale", "logs/whale.log")

# `positions_mat` IS the Position dataclass, in its field order.
POSITION_COLS = ("proxy_wallet, condition_id, outcome_index, avg_price, size,"
                 " notional_usd, entry_ts, last_ts, pregame, sport, market_type,"
                 " root, source, sold_size, sell_value")
# The order positions_from_prints itself produced: SQLite hands prints back in
# PRINT_ORDER, so groups were visited in (wallet, cid, outcome_index) order and
# a group's windows appended in ts order. Same collation (BINARY / UTF-8 bytes
# == code points), so this reproduces that list exactly — order matters because
# the equivalence tests compare `state.positions` as a LIST.
POSITION_ORDER = "ORDER BY proxy_wallet, condition_id, outcome_index, entry_ts"


def cache_partial(con) -> bool:
    """True while a FULL rebuild is still draining — `--rebuild`, a
    `MAT_VERSION` bump, or the first fill of an empty cache. NOT "some wallets
    are dirty": in steady state a few always are and every other wallet is
    present, so the population is the tape's.

    It matters because V5's bar is a PERCENTILE of the materialized population
    (arb/whale_elite.py): a partial population does not merely under-count, it
    can ADMIT a whale the full one denies. The lane hands this to
    `whale_signal.evaluate`, which pins V5 `cache-partial`. Measurement and
    registration: KNOWN_ISSUES.md #34, "Correction 2026-09-05".
    """
    if str(_meta(con, "mat_rebuilding") or "0") != "1":
        return False
    return bool(con.execute("SELECT EXISTS(SELECT 1 FROM mat_dirty)").fetchone()[0])


def freshness(con, cfg, now=None) -> dict:
    """How CURRENT the cache is, for a reader that cannot make it current.

    `open_tape_ro` probed that `positions_mat` EXISTS; the report, the
    dashboard, `whale-score` and `whale-rank` then scored off it with no way
    to tell a one-cycle-old cache from a day-old one (I2, review of 783301f).
    Two numbers say it: prints above the materialization watermark, and how
    long the queue has been non-empty (`mat_clean_at` is the last pass that
    emptied it; never having emptied reads as stale — that is the first fill).
    """
    mat = cfg["whale"]["mat"]
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:                      # every stamp on the tape is UTC
        now = now.replace(tzinfo=timezone.utc)
    behind = (con.execute("SELECT MAX(id) FROM prints").fetchone()[0] or 0) \
        - int(_meta(con, "mat_watermark") or 0)
    dirty = con.execute("SELECT COUNT(*) c FROM mat_dirty").fetchone()[0]
    clean_at = _meta(con, "mat_clean_at")
    secs = None if not clean_at else int(
        (now - datetime.fromisoformat(clean_at)).total_seconds())
    stale = bool(behind > int(mat["stale_prints"])
                 or (dirty and (secs is None or secs > int(mat["stale_secs"]))))
    return {"behind": behind, "dirty": dirty, "at": _meta(con, "mat_at"),
            "dirty_secs": secs if dirty else 0, "stale": stale,
            "note": f"cache: stale ({behind} behind, {dirty} dirty)" if stale else None}


def warn_if_stale(con, cfg, now=None) -> dict:
    """`freshness`, logged. Called from the ONE place every read-only reader
    goes through (`report_whale.open_tape_ro`), so no reader can forget it.
    ponytail: no throttle — a stale cache is an alarm state, not a steady one;
    add one here if a dashboard poll ever floods the log."""
    fresh = freshness(con, cfg, now)
    if fresh["stale"]:
        log.warning("whale positions cache is STALE: %s print(s) above the "
                    "materialization watermark, %s wallet(s) queued, last pass "
                    "%s. Scores read off it are computed over what the last "
                    "cycle materialized — check the whale lane, or run "
                    "`arb whale-mat --rebuild`.",
                    fresh["behind"], fresh["dirty"], fresh["at"] or "never")
    return fresh


def load_positions(con) -> list[Position]:
    """The cache, as `positions_from_prints` would have returned it."""
    cur = con.cursor()
    cur.row_factory = None
    return [Position(
        wallet=r[0], condition_id=r[1], outcome_index=r[2], avg_price=Decimal(r[3]),
        size=Decimal(r[4]), notional_usd=Decimal(r[5]), entry_ts=r[6], last_ts=r[7],
        pregame=bool(r[8]), sport=r[9], market_type=r[10], root=r[11], source=r[12],
        sold_size=Decimal(r[13]), sell_value=Decimal(r[14]))
        for r in cur.execute(f"SELECT {POSITION_COLS} FROM positions_mat {POSITION_ORDER}")]


def load_hygiene(con) -> Hygiene:
    """The cache, as `hygiene_flags` would have returned it. A wallet with no
    flags simply has no row — `Hygiene.excludes` reads a missing key and an
    empty set the same way, so this is the same object for every caller."""
    wallets: dict[str, set[str]] = {}
    markets: dict[tuple[str, str], set[str]] = {}
    for wallet, cid, flag in con.execute(
            "SELECT proxy_wallet, condition_id, flag FROM hygiene_mat"):
        target = wallets.setdefault(wallet, set()) if cid is None \
            else markets.setdefault((wallet, cid), set())
        target.add(flag)
    return Hygiene(wallets=wallets, markets=markets)
