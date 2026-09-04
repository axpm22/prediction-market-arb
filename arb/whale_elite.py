"""L14 variant V5 "elite" (design §7, registered 2026-09-04): a RELATIVE
qualification bar instead of an absolute one.

`min_score_lb` (0.0) is *below* the prior mean — with the sports base rate at
+0.0428 a wallet with no history at all posts lb ~= -0.009, so one decent
resolved position clears the bar — and the bar's meaning drifts every cycle
with `m0` (it moved from +0.0464 to +0.0428 in six hours on 2026-09-04). V5
asks a question that is invariant to both: is this whale in the top
`fire.elite_pct` of EVERY trader we have scored in this root?

The reference population is pinned by the registration and is not a tunable:
every wallet with at least one resolved position in that root as of the
evaluation (point-in-time, the same `resolved_at < as_of` rule the scores
obey), MINUS the hygiene-excluded wallets, each scored into the signal's own
bucket by the ordinary transfer chain whether or not it has ever traded that
bucket. So the population's membership is identical in every bucket of the
root and only the values differ. It is emphatically NOT the qualifying roster:
a wallet below `min_score_lb` is still in the denominator.

Both halves come free from what the cycle already built: `score_state`
aggregates `open_positions`, i.e. hygiene-filtered and point-in-time, so
`agg`'s `(wallet, root)` keys ARE the population.

Split out of arb/whale_signal.py on size alone (that module is at its ~300
line cap). The arithmetic is PURE — no clock, no venue; `persist` is the one
write, to the tape's own file, and the only reason this module knows sqlite.

Rule 4: `fire.elite_pct` and `min_score_lb` from config. Rule 5: the cutoff is
        a Decimal `lb` selected by ORDER, never averaged — no float anywhere.
"""
from __future__ import annotations

from decimal import Decimal

from arb.logutil import get_logger
from arb.whale_merge import d
from arb.whale_score import root_of

log = get_logger("arb.whale", "logs/whale.log")


# ------------------------------------------------------------------- points

def point(whale, ctx) -> Decimal:
    """V5's points for ONE whale on its own side: 1 above this bucket's cutoff.

    A missing cutoff — an empty population, or a caller with no scoring state
    such as the read-only dashboard — is NO consensus reading, never a free
    point for every whale. That is the direction that books fewer signals and
    the one that cannot flip a position out of an exit recount.
    """
    cut = (ctx or {}).get("elite")
    return Decimal(1) if cut is not None and d(whale["lb"]) >= d(cut) else Decimal(0)


# ------------------------------------------------------------------- cutoff

def population(agg, root) -> list[str]:
    """Every wallet with a resolved outcome in `root`, straight off the
    aggregation the cycle already built (`whale_score.aggregate` writes a
    2-tuple `(wallet, root)` key per outcome). Hygiene-excluded wallets are
    absent because `score_state` aggregates `open_positions`."""
    return sorted({k[0] for k in agg if len(k) == 2 and k[1] == root})


def cutoff(cfg, agg, lookup, sport, market_type) -> tuple[Decimal | None, int]:
    """(the top-`elite_pct` cutoff of `lb` in this bucket, population size).

    Nearest-rank from the top, floored: at `elite_pct` 20 over 10 wallets the
    elite are the top 2 and the cutoff is the 2nd-highest lb, compared with
    `>=`. A population too small to have one elite member (fewer than
    100/`elite_pct` wallets) returns None — nobody is elite and V5 does not
    fire, which is the honest reading of "the top 20% of four traders".
    """
    root = root_of(sport, market_type)
    scores = (lookup(w, sport, market_type) for w in population(agg, root)) \
        if root else ()
    values = sorted((s.lb for s in scores if s is not None), reverse=True)
    k = int(len(values) * d(cfg["whale"]["fire"]["elite_pct"]) / 100)
    return (values[k - 1] if k else None), len(values)


class Index:
    """One cutoff per bucket per cycle, computed on first use.

    The population is ~170 wallets and a live cycle sees a handful of distinct
    buckets, so this is ~1k posterior evaluations of pure arithmetic — but it
    is per bucket, not per market, and the memo is what keeps it that way.
    """

    def __init__(self, cfg, state):
        self.cfg, self.state, self.cells = cfg, state, {}

    def __call__(self, sport, market_type) -> Decimal | None:
        return self.cell(sport, market_type)[0]

    def cell(self, sport, market_type) -> tuple[Decimal | None, int]:
        key = (sport, market_type)
        if key not in self.cells:
            self.cells[key] = cutoff(self.cfg, self.state.agg, self.state.lookup,
                                     sport, market_type)
        return self.cells[key]


def index(cfg, state) -> Index:
    """The cycle's index, over the scoring pass it already ran."""
    return Index(cfg, state)


# -------------------------------------------------------------- the record

def persist(con, index, cfg, now) -> None:
    """Every cutoff this cycle actually used, with the population behind it.

    §3's stated instability — rescoring the contributing whales as of today
    instead of as of their signal collapses top-20% retention from 15 fires to
    4 — means V5's n accumulates against a MOVING definition. That is not to be
    fixed by freezing the set; it is to be visible, which is this table and the
    report line that reads it.
    """
    bar = d(cfg["whale"]["min_score_lb"])
    degenerate = []
    for (sport, market_type), (cut, n) in sorted(index.cells.items(),
                                                 key=lambda kv: str(kv[0])):
        root = root_of(sport, market_type)
        if root is None:
            continue                       # an unmappable market has no population
        bucket = f"{sport}:{market_type}"
        con.execute("INSERT OR REPLACE INTO elite_cutoffs (root, bucket, as_of,"
                    " cutoff, n) VALUES (?,?,?,?,?)",
                    (root, bucket, now.isoformat(),
                     None if cut is None else str(cut), n))
        if cut is not None and cut <= bar:
            degenerate.append(f"{bucket} {cut} (pop {n})")
    con.commit()
    if degenerate:
        _warn_degenerate(con, degenerate, cfg, now)


def _warn_degenerate(con, buckets, cfg, now) -> None:
    """Measured 2026-09-04: at `elite_pct` 40 the cutoff (-0.0046) falls BELOW
    `min_score_lb`, so every qualifying whale is automatically elite and V5 is
    a copy of V1 (56/56 fires retained — the tell). Once per UTC day, because
    it is a config-shaped fact and the cycle runs every 60 s."""
    from arb.whale_tape import _meta, _set_meta
    day = now.date().isoformat()
    if _meta(con, "elite_degenerate_day") == day:
        return
    _set_meta(con, "elite_degenerate_day", day)
    log.warning("V5 elite cutoff is at or below whale.min_score_lb (%s) in %s — "
                "every qualifying whale is elite, so V5 is a COPY of V1 and not a "
                "distinct variant. elite_pct=%s (design §7; 30 is the pre-declared "
                "fallback, a change is a dated amendment)",
                cfg["whale"]["min_score_lb"], ", ".join(buckets),
                cfg["whale"]["fire"]["elite_pct"])


def latest(con) -> list[dict]:
    """The newest cutoff row per bucket — what the daily report prints, the
    same shape `base_rates` is read in. An older tape that predates the table
    reads as no rows rather than an error."""
    import sqlite3
    try:
        return [dict(r) for r in con.execute(
            "SELECT root, bucket, as_of, cutoff, n FROM elite_cutoffs e WHERE as_of ="
            " (SELECT MAX(as_of) FROM elite_cutoffs x WHERE x.bucket=e.bucket)"
            " ORDER BY bucket")]
    except sqlite3.Error:
        return []
