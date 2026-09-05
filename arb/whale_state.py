"""L14 scoring assembly: the tape's positions -> hygiene -> scores.

Lifted out of `arb/lanes/whale.py` (Task 9) so the daily report can price the
same whales the lane prices, from the same tape, WITHOUT any of the lane's
network steps. There is exactly one assembly; duplicating it in the report is
how two "qualifying whale" counts start disagreeing.

Pure read. Persisting the base rates is the LANE's business (spec §6 wants a
score reproducible from a stored `as_of`), so it stays in the caller and a
read-only tape handle is enough here.

matpos (2026-09-05) — the structural fix, and the end of a series of read-path
passes on this function. The history, short: `SELECT *` -> column trims
(2026-09-04, prints then markets/resolutions), `sorted()` of the whole tape ->
`idx_prints_score` order (posperf, same day), O(n^2) hygiene scans -> sliding
windows (arb/whale_hygiene.py, same day), `dict(r)` per print -> `_PrintRow`
over the row's tuple (rowperf, 2026-09-05, ~3.4x on this function's own read
loop). Each bought a constant. None changed the shape: every one of them
rebuilt EVERY wallet's positions and hygiene flags from EVERY print, every
60 s cycle. Profiled live 2026-09-05 08:50Z on the production tape (763,786
prints): 29.4 s and 57,290 positions, against a 60 s cadence, a child cap
raised 120 -> 300 s as insurance, and ~700k new rows a day while 2,700 wallet
histories backfill — linear growth, so no constant was ever going to hold.

So this function no longer reads `prints` at all. `arb/whale_mat.py`
recomputes the wallets whose prints actually changed — calling the SAME
`positions_from_prints` and `hygiene_flags`, per wallet, never a second
implementation — and caches the result in `positions_mat`/`hygiene_mat`; this
reads the cache. Cost here is now O(cached positions), independent of how many
prints the tape holds. The FIFO and hygiene semantics are unchanged, which
tests/test_whale_mat.py asserts against the pre-materialization body directly
(fresh prints, backfilled older prints, a hygiene flag flipping, a MAT_VERSION
bump, a `start_utc` arriving late).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from arb import whale_bands, whale_mat, whale_score
# matpos (2026-09-05): the prints read, the row object over it and the market
# columns moved to arb/whale_mat.py, which is now the only module that reads
# `prints` for scoring. Re-exported because they are still the constants of
# THIS assembly's contract (tests introspect them here) and because
# `whale_mat` and this module must read identical `markets` columns.
from arb.whale_mat import MARKET_COLS, PRINT_COLS, PRINT_ORDER, _PrintRow  # noqa: F401

# Everything resolved_outcomes (whale_score.py) and whale_rank.py read off a
# `resolutions` row. `source`/`fetched_at` and `raw_json` — the venue's whole
# resolution payload — are never read back through `state.resolutions`.
RESOLUTION_COLS = "condition_id, resolved_at, winner_outcome_index, status"


@dataclass(frozen=True)
class WhaleState:
    # matpos (2026-09-05): no `prints` field any more. It only ever served the
    # lane's `_from_prints` (the token and event_slug maps), which now reads
    # the handful of markets it actually prices — arb/lanes/whale_book.py
    # `market_tokens`. Carrying the whole tape here was the read this pass
    # exists to remove.
    markets: dict            # condition_id -> tape `markets` row
    resolutions: dict        # condition_id -> tape `resolutions` row
    positions: list          # every position the tape supports
    open_positions: list     # ... minus the hygiene-excluded ones
    hygiene_excluded: int
    rates: dict              # root -> base rate in force at `as_of`
    rate_n: dict             # root -> the n behind it
    agg: dict                # whale_score.aggregate over resolved outcomes
    lookup: Callable         # (wallet, sport, market_type) -> Score
    # root -> band -> (what that price band paid everyone, n). Empty when
    # `whale.score.band_adjust` is false, and empty is the no-op everywhere.
    bands: dict = field(default_factory=dict)


def score_state(cfg, con, now) -> WhaleState:
    """One scoring pass over the tape, `as_of` = now.

    matpos (2026-09-05): reads the MATERIALIZED positions and hygiene flags
    (arb/whale_mat.py) instead of rebuilding both from every print. The FIFO
    and hygiene logic is unchanged and still lives in exactly one place —
    `whale_mat.materialize` calls `positions_from_prints`/`hygiene_flags` per
    dirty wallet, and this reads what they produced. Pure READ, so a
    read-only handle is still enough (`report_whale.open_tape_ro`, the
    dashboard): whoever holds the write handle — the lane cycle — is what
    keeps the cache current, and a stale-by-one-cycle cache reads here as
    "that wallet's newest prints have not landed yet", never as a wrong number.
    """
    markets = {r["condition_id"]: dict(r)
               for r in con.execute(f"SELECT {MARKET_COLS} FROM markets")}
    resolutions = {r["condition_id"]: dict(r)
                   for r in con.execute(f"SELECT {RESOLUTION_COLS} FROM resolutions")}
    positions = whale_mat.load_positions(con)
    hyg = whale_mat.load_hygiene(con)
    open_pos = [p for p in positions if not hyg.excludes(p)]
    outcomes = whale_score.resolved_outcomes(open_pos, resolutions, now)
    # Band grading (amendment 2026-09-05), and the reason it is HERE: one
    # subtraction before `aggregate` is the whole change, so score_for, V5's
    # elite cutoffs, V6's excess x notional and the V2/V3 weights all read
    # band-adjusted skill without a branch. `rates` below is then m0 over the
    # SAME graded edges — the EB prior mean it always was, now centred near
    # zero because what the root pays lives in the band table.
    bands = whale_bands.table(outcomes, now, cfg)
    outcomes = whale_bands.adjust(outcomes, bands, cfg)
    agg = whale_score.aggregate(outcomes)
    rates, rate_n = {}, {}
    for root in {o.root for o in outcomes} | {"sports"}:
        rates[root], rate_n[root] = whale_score.base_rate(outcomes, root, now, cfg)

    def lookup(wallet, sport, market_type):
        return whale_score.score_for(agg, wallet, sport, market_type, cfg, rates)

    return WhaleState(markets=markets, resolutions=resolutions,
                      positions=positions, open_positions=open_pos,
                      hygiene_excluded=len(positions) - len(open_pos),
                      rates=rates, rate_n=rate_n, agg=agg, lookup=lookup,
                      bands=bands)


def qualifying_whales(cfg, state) -> list[dict]:
    """One row per (wallet, bucket) with an open position whose score clears
    `min_score_lb` — the lane's own bar (arb/whale_merge.qualifying), read
    without a market or a signal window because the report is describing the
    roster, not firing on it. `lb` is the lb80 the fire rule uses."""
    from decimal import Decimal
    bar = Decimal(str(cfg["whale"]["min_score_lb"]))
    seen, out = set(), []
    for p in state.open_positions:
        key = (p.wallet, p.bucket)
        if key in seen:
            continue
        seen.add(key)
        score = state.lookup(p.wallet, p.sport, p.market_type)
        if score is None or score.lb < bar:
            continue
        out.append({"wallet": p.wallet, "root": p.root, "sport": p.sport,
                    "bucket": p.bucket, "lb": score.lb, "n_bucket": score.n_bucket,
                    "native": score.native})
    return sorted(out, key=lambda r: r["lb"], reverse=True)
