"""L14 resolution sweep (spec §5 step 5): the backlog reader for markets a
whale traded that have aged out of the 60 s cycle's own universe.

Split out of arb/whale_resolve.py on size alone (2026-09-04 V4 fix pass;
whale_resolve.py was 318 lines) — same rules: rule 1 (public GETs only,
through `read_resolutions`), rule 9 (the tape file, never the ledger).
Previously re-exported as `whale_backfill.resolve_sweep`; callers now import
this module directly (`arb/commands.py`'s `cmd_whale_backfill`, and the
tests).
"""
from __future__ import annotations

from datetime import timedelta

from arb.venues.base import get_json
from arb.whale_resolve import read_resolutions, resolution_for
from arb.whale_tape import _set_meta, log

# Markets a whale actually traded that still have no verdict, oldest start
# first. Epoch comparison, never string compare: `start_utc` is whatever ISO
# form the venue sent (`discover_wallets` reads it the same way). Sport/type
# NULL means the market was never mapped and can never be scored (never worth
# a sweep slot). The `swept` LEFT JOIN is the per-day skip marker below: a
# market swept and left unresolved today is not asked again until tomorrow,
# so the FIFO advances past it instead of re-asking the same oldest ids
# forever.
SWEEP_SQL = """
    SELECT m.condition_id FROM markets m
     LEFT JOIN meta sw ON sw.key = 'swept:' || m.condition_id
     WHERE m.start_utc IS NOT NULL
       AND m.sport IS NOT NULL AND m.market_type IS NOT NULL
       AND CAST(strftime('%s', m.start_utc) AS INTEGER) <= ?
       AND CAST(strftime('%s', m.start_utc) AS INTEGER) > ?
       AND (sw.value IS NULL OR sw.value <> ?)
       AND NOT EXISTS (SELECT 1 FROM resolutions r
                       WHERE r.condition_id = m.condition_id)
       AND EXISTS (SELECT 1 FROM prints p
                   WHERE p.condition_id = m.condition_id AND p.side = 'BUY')
     ORDER BY CAST(strftime('%s', m.start_utc) AS INTEGER), m.condition_id
     LIMIT ?"""


def resolve_sweep(cfg, con, now, get=get_json) -> int:
    """Spec §5 step 5 (final review 2026-09-04): the 60 s cycle reads
    resolutions only for its CURRENT universe (`start_utc` within
    [-lookback_hours, +lookahead_hours]) and for its open bets, so a market a
    whale traded that has aged out — every backfilled one — was never read at
    all and no wallet could ever score. This sweeps them, FIFO by start,
    `backfill.resolve_per_cycle` a cycle, through the SAME reader: the shared
    token bucket, the shared backoff, two ids per Gamma call.

    Head-of-line blocking fix (2026-09-04): `read_resolutions` persists
    nothing for a market Gamma reports open or omits, so without a marker the
    same oldest ids would be re-asked every cycle forever and nothing past
    them would ever be swept. Every id still unresolved after the call gets a
    `swept:<condition_id>` meta row dated today (mirroring `_due`'s
    `giveup:` marker), and `SWEEP_SQL` skips markets swept today — one ask a
    day per market, and the FIFO advances within the same cycle budget.

    # ponytail: no watermark beyond that. `resolutions` is itself the "done"
    # marker and a market past resolve_giveup_days drops out of the query, so
    # the backlog drains without a second piece of state to get out of sync.
    """
    w = cfg["whale"]
    due = int((now - timedelta(hours=float(w["resolve_after_hours"]))).timestamp())
    floor = int((now - timedelta(days=int(w["resolve_giveup_days"]))).timestamp())
    today = now.strftime("%Y-%m-%d")
    ids = [r[0] for r in con.execute(
        SWEEP_SQL, (due, floor, today, int(w["backfill"]["resolve_per_cycle"])))]
    if not ids:
        return 0
    log.info("whale resolution sweep: %d market(s) due", len(ids))
    n = read_resolutions(cfg, con, ids, now, get=get)
    for cond in ids:
        if resolution_for(con, cond) is None:
            _set_meta(con, f"swept:{cond}", today)
    return n
