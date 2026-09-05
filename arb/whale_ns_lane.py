"""L14 the 60 s lane's non-sports half: a pure READ of the tape's `markets`.

Split out of `arb/whale_nonsports.py` on size (fix pass 2, 2026-09-05), along
the boundary that file's docstring already named: the Gamma half roots a market
and decides whether it is ours, this half decides which of the already-rooted
rows the CYCLE may price right now. Nothing here imports GAMMA, `_call` or the
token bucket, and that is the invariant worth having a file for — a lane cycle
costs no Gamma call for its non-sports markets.

Rules as everywhere else: rule 4 (every threshold from
`cfg["whale"]["nonsports"]`), rule 5 (the volume floor compares Decimals),
rule 9 (this file only ever reads).
"""
from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

from arb.whale_nonsports import _volume, two_sided
from arb.whale_universe import MARKET_COLS, Market

# The lane's own half of the universe. Epoch comparison through strftime, the
# idiom whale_sweep.SWEEP_SQL already uses on this column — never a string
# compare, because `start_utc` is whatever ISO form its source wrote.
#
# The `resolutions` guard is I2 (fix pass 2): the window's lower bound already
# drops a market whose deadline has passed, but a market can be RESOLVED while
# its stored deadline is still ahead of us — Polymarket resolves early, and the
# mapper's own `closed=true` pass writes those rows (`record_resolution`,
# source='mapper') for markets it roots non-sports in the very same call. Both
# halves are checked, because neither implies the other.
TAPE_SQL = f"""
    SELECT {MARKET_COLS} FROM markets
     WHERE sport IN ({{roots}}) AND start_utc IS NOT NULL AND market_type IS NOT NULL
       AND CAST(strftime('%s', start_utc) AS INTEGER) BETWEEN ? AND ?
       AND NOT EXISTS (SELECT 1 FROM resolutions r
                        WHERE r.condition_id = markets.condition_id)
     ORDER BY CAST(strftime('%s', start_utc) AS INTEGER), condition_id"""


def tape_universe(con, cfg, now) -> list[Market]:
    """Non-sports-rooted `markets` rows whose resolution deadline is still
    ahead of us and inside `whale.nonsports.max_days` — the same horizon
    `arb/tracker.py` applies to a WHALE row at booking, so the lane never
    prices a market it could not book — which have not already resolved, whose
    measured 24h volume clears `min_volume_24h_usd`, and which have exactly two
    distinguishable outcomes.

    Why the last three are applied HERE and not only in `markets_of`
    (amendment 2026-09-05): `nonsports_universe` is no longer the only writer
    of a non-sports `markets` row. `whale_map._event_roots` roots a market from
    its event's tags so a whale's HISTORICAL politics positions can be scored,
    and it deliberately applies no volume floor — a resolved market's
    `volume24hr` is no reason to refuse to score the whale who traded it. Most
    such rows are in the past and the window already excludes them, but not
    all: measured on the live tape 2026-09-05, the first 100-row sample of the
    revisit queue held a politics market with a future `endDate` and a NULL
    `volume24hr`. This read is the one place the LANE's universe is decided, so
    the registered floors belong here — a null volume failing exactly as it
    does in `markets_of` (design §5 step 1b).
    """
    ns = cfg["whale"]["nonsports"]
    roots = sorted(set(ns["tags"].values()))
    if not ns["enabled"] or not roots:
        return []
    hi = now + timedelta(days=int(ns["max_days"]))
    floor = Decimal(str(ns["min_volume_24h_usd"]))
    rows = con.execute(TAPE_SQL.format(roots=",".join("?" * len(roots))),
                       (*roots, int(now.timestamp()), int(hi.timestamp())))
    out = []
    for r in rows:
        volume = _volume(r["volume_24h"])
        tokens = two_sided(json.loads(r["tokens_json"] or "[]"))
        if volume is None or volume < floor or tokens is None:
            continue
        out.append(Market(condition_id=r["condition_id"], sport=r["sport"],
                          market_type=r["market_type"], line=r["line"],
                          start_utc=r["start_utc"], event_key=r["event_key"],
                          tokens=tokens, volume_24h=r["volume_24h"]))
    return out


def seed_volumes(volumes, markets) -> dict:
    """Fill V6's denominator for a market the watch lane never quotes.

    The lane builds `volumes` from the ledger's own watch quotes, which cover
    Polymarket SPORTS only — a non-sports market would then read `None`, which
    `whale_dominance.point` treats as `dominance.min_volume_usd` ($5,000) and a
    $20M/day politics market would come out maximally dominant. `setdefault`
    keeps the ledger authoritative wherever it has an answer; this only fills
    the gap, from the Gamma figure `markets.volume_24h` already stores."""
    for m in markets:
        if m.volume_24h is not None:
            for token, _ in m.tokens:
                volumes.setdefault(f"{m.condition_id}:{token}", Decimal(m.volume_24h))
    return volumes
