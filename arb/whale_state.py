"""L14 scoring assembly: the tape's prints -> positions -> hygiene -> scores.

Lifted out of `arb/lanes/whale.py` (Task 9) so the daily report can price the
same whales the lane prices, from the same tape, WITHOUT any of the lane's
network steps. There is exactly one assembly; duplicating it in the report is
how two "qualifying whale" counts start disagreeing.

Pure read. Persisting the base rates is the LANE's business (spec §6 wants a
score reproducible from a stored `as_of`), so it stays in the caller and a
read-only tape handle is enough here.

Retention regrade 2026-09-04 (score_state read trim, part 2 — part 1 was the
`prints` trim, 2534680): `markets`/`resolutions` were still `SELECT *`,
carrying `resolutions.raw_json` (the venue's whole resolution payload) and
markets/resolutions columns nothing reads back through `state`
(first_seen/last_seen/volume_24h; source/fetched_at). Measured on a synthetic
400k-print/2,000-market tape (~700 B raw_json/print, ~300 B/resolution,
tests/../scratchpad bench script, not committed):
  score_state (this fix only, prints already trimmed): 7.4-7.6s -> 6.8-6.9s
Still > 4s at 400k rows: cProfile'd once (not fixed here) — the next hot spot
is `whale_score.positions_from_prints`, ~3.5s of its own time (vs. ~1.4s in
score_state's own reads+dict() and ~1.3s in hygiene_flags' own time), from the
full-tape `sorted()` up front plus a `Decimal(str(...))` conversion of
`notional_usd` done twice per non-dust print (once for the dust check, again
into `cur["notional"]`).

posperf (2026-09-04): fixed exactly that — see `whale_score.positions_from_prints`'s
own docstring for the change. `arb/whale_tape.py`'s SCHEMA gained
idx_prints_score(proxy_wallet, condition_id, outcome_index, ts) and `score_state`
now asks for that order (PRINT_ORDER below), so positions_from_prints groups
in whatever order SQLite hands it and only re-sorts a group whose own ts goes
backwards, instead of sorting the whole tape. `notional_usd` is parsed to
Decimal once per row (was up to 3x for a dust row: once for price, once for
size, once for notional itself — dust is checked first now, off notional
alone, before price/size are touched at all).

Measured (tests/../scratchpad bench scripts, not committed; same 400k-print/
2,000-market/500-wallet synthetic tape as above, same machine):
  positions_from_prints alone, isolated (bench_pfp2.py: one sqlite read, N
  in-process calls, min/median/max reported to damp this host's load jitter):
    200k rows: before 0.99-1.58s (median ~1.25s) -> after 0.83-1.04s (median ~0.93s)
    400k rows: before 2.33-3.26s (median ~2.74s) -> after 1.66-2.06s (median ~1.82s)
  score_state end-to-end (bench_score_state.py), 400k rows, 6 runs each side:
    before 4.67-5.75s, after 4.49-6.30s — this host's background load swings
    wider than the isolated function's own win at the whole-cycle grain (I/O +
    hygiene_flags dominate the noise budget here), so this number alone does
    NOT show the fix; the isolated positions_from_prints numbers above and the
    cProfile split below do.
Still > 4s at 400k: cProfile'd once more (not fixed here). positions_from_prints
dropped from the largest self-time consumer to the second-largest: a fresh
cProfile run (instrumentation inflates absolute seconds, not the ratios) put
score_state's OWN body (the SQL fetch + dict() conversion across all three
tables) at 3.43s tottime, positions_from_prints at 2.04s, hygiene_flags at
0.62s own / 2.09s cumulative. The next hot spot, if this ever needs a third
pass, is score_state's own read+dict() body — already column-trimmed (the
2026-09-04 regrade above); further reduction would mean not materializing a
`dict` per sqlite3.Row at all, a bigger read-path change than this pass.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from arb import whale_score
from arb.whale_hygiene import hygiene_flags

# Exactly what positions_from_prints, hygiene_flags and the lane's _from_prints
# read. `SELECT *` also pulled `raw_json` — the venue's whole payload for every
# print ever collected — into memory on every 60 s cycle.
PRINT_COLS = ("ts, proxy_wallet, condition_id, outcome_index, side, price, size,"
              " notional_usd, source, asset, event_slug")

# posperf (2026-09-04): positions_from_prints groups by (proxy_wallet,
# condition_id, outcome_index) and needs each group's own rows in ascending
# ts. Ordering here, off idx_prints_score (arb/whale_tape.py SCHEMA), means
# SQLite delivers rows already in that order — positions_from_prints no
# longer sorts the whole tape in Python.
PRINT_ORDER = "ORDER BY proxy_wallet, condition_id, outcome_index, ts"

# Retention regrade 2026-09-04 (§ score_state read trim): everything
# positions_from_prints, _tape_market/_event_key (arb/lanes/whale_book.py,
# reached off state.markets when a held bet's market has aged out of the
# universe) and dashboard_whale/whale.py's exit path read off a `markets` row.
# `first_seen`/`last_seen`/`volume_24h` are stored but never read back through
# `state.markets` (the lane's own V6 volume comes from watch quotes, not the
# tape) — `SELECT *` carried them anyway.
MARKET_COLS = ("condition_id, sport, market_type, line, start_utc, event_key,"
               " tokens_json, slug")

# Everything resolved_outcomes (whale_score.py) and whale_rank.py read off a
# `resolutions` row. `source`/`fetched_at` and `raw_json` — the venue's whole
# resolution payload — are never read back through `state.resolutions`.
RESOLUTION_COLS = "condition_id, resolved_at, winner_outcome_index, status"


@dataclass(frozen=True)
class WhaleState:
    prints: list
    markets: dict            # condition_id -> tape `markets` row
    resolutions: dict        # condition_id -> tape `resolutions` row
    positions: list          # every position the tape supports
    open_positions: list     # ... minus the hygiene-excluded ones
    hygiene_excluded: int
    rates: dict              # root -> base rate in force at `as_of`
    rate_n: dict             # root -> the n behind it
    agg: dict                # whale_score.aggregate over resolved outcomes
    lookup: Callable         # (wallet, sport, market_type) -> Score


def score_state(cfg, con, now) -> WhaleState:
    """One scoring pass over the whole tape, `as_of` = now.

    # ponytail: rebuilt from every print each call, exactly as the lane does it
    # every 60 s. If a backfilled tape ever makes this outrun the report, cache
    # the aggregation per wallet — not a second, divergent assembly.
    """
    prints = [dict(r) for r in con.execute(f"SELECT {PRINT_COLS} FROM prints {PRINT_ORDER}")]
    markets = {r["condition_id"]: dict(r)
               for r in con.execute(f"SELECT {MARKET_COLS} FROM markets")}
    resolutions = {r["condition_id"]: dict(r)
                   for r in con.execute(f"SELECT {RESOLUTION_COLS} FROM resolutions")}
    positions = whale_score.positions_from_prints(prints, markets, cfg)
    hyg = hygiene_flags(prints, positions, cfg)
    open_pos = [p for p in positions if not hyg.excludes(p)]
    outcomes = whale_score.resolved_outcomes(open_pos, resolutions, now)
    agg = whale_score.aggregate(outcomes)
    rates, rate_n = {}, {}
    for root in {o.root for o in outcomes} | {"sports"}:
        rates[root], rate_n[root] = whale_score.base_rate(outcomes, root, now, cfg)

    def lookup(wallet, sport, market_type):
        return whale_score.score_for(agg, wallet, sport, market_type, cfg, rates)

    return WhaleState(prints=prints, markets=markets, resolutions=resolutions,
                      positions=positions, open_positions=open_pos,
                      hygiene_excluded=len(positions) - len(open_pos),
                      rates=rates, rate_n=rate_n, agg=agg, lookup=lookup)


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
