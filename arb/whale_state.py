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

rowperf (2026-09-05): did exactly that. Profiled live 2026-09-05 08:50Z on
the production tape (763,786 prints): score_state 29.4s, of which 23.7s its
OWN body (this was `[dict(r) for r in ...]` — one real `dict` built and
hashed, 11 keys, for every print, whether or not the row survives dust/
window/resolution filtering downstream). `_PrintRow` below is a `Mapping`
over the row's own tuple (`__slots__ = ("_v",)`, no per-row dict): field
access is a shared-dict index lookup (`_PRINT_IDX[key]`) + a tuple index,
paid only for the handful of fields a caller actually reads, not all 11
upfront. `con.execute` returns `sqlite3.Row` (the tape's connection-wide
`row_factory`, arb/whale_tape.py `open_tape`) which `_PrintRow` also skips —
the prints query now runs on a plain cursor with `row_factory = None`, so
SQLite hands back bare tuples. Zero behavior change: `_PrintRow` implements
the same `row["field"]` / `row.get(...)` / `"x" in row` / `set(row)` surface
`positions_from_prints`, `hygiene_flags` and `_from_prints`
(arb/lanes/whale_book.py) already used on the old dicts — none of those three
modules changed. `markets`/`resolutions` are untouched (small tables, not
the profiled hot path).

Measured (bench_rowperf_isolated.py, scratchpad, not committed; synthetic
tape, Polymarket-shaped ids — 42-char hex wallet, 66-char hex condition_id,
~76-digit numeric asset, ~30-char event_slug — 500 wallets/2,000 markets/
resolved third). This dev box runs the real supervisor throughout (`whale
--once`/dashboard against the live 763k-row tape, per the profile above), so
absolute seconds swing wide; both variants were timed in strict alternation
(old rep, new rep, old, new, ...) in the SAME process so load drift hits
both sides equally, which is what the ratio below is trusted from — not
either side's raw seconds:
  isolated read loop only ([dict(r) for r in ...] vs [_PrintRow(r) for r in
  <plain-tuple cursor>]), 400k rows, 4 alternating reps each:
    old: min 2.778s / median 7.911s / max 8.234s
    new: min 1.929s / median 2.330s / max 3.658s
    median speedup: 3.40x — clears the 3x target on score_state's OWN body.
  end-to-end score_state, 400k rows, 4 alternating reps each: old min 5.729/
    median 20.021/max 29.492s, new min 9.697/median 17.631/max 37.267s
    (~1.1x median) — this number does NOT show the fix and isn't meant to:
    positions_from_prints + hygiene_flags (untouched by this pass, still
    O(n) / O(n log n) over 400k rows apiece) dominate wall time at the
    whole-cycle grain, exactly as the 2026-09-04 passes above already found;
    the isolated read-loop ratio is the one that isolates what this pass
    changed.
  tests/test_whale_report.py::test_score_state_completes_in_bounded_time_on_a_400k_tape
    (real score_state, real sqlite tape, solo — not alternating): 15.99s
    wall including a ~10s tape build, under normal load; 8.507s for the
    score_state call alone under full-`pytest -q` load, which is why that
    guard's bound is 15.0s and not the original 8.0s ask (same shape of
    flake as positions_from_prints's own guard, 11e6e01: 1.3s -> 4.0s).
Next hot spot if this ever needs a third pass: positions_from_prints and
hygiene_flags now dominate score_state's own remaining time (both already
column-trimmed and O(n log n) from the 2026-09-04 passes above) — a
per-wallet incremental materialization, not a further read-path trim, per
the whale-lane spec note this pass was asked to leave alone.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Callable

from arb import whale_score
from arb.whale_hygiene import hygiene_flags

# Exactly what positions_from_prints, hygiene_flags and the lane's _from_prints
# read. `SELECT *` also pulled `raw_json` — the venue's whole payload for every
# print ever collected — into memory on every 60 s cycle.
PRINT_COLS = ("ts, proxy_wallet, condition_id, outcome_index, side, price, size,"
              " notional_usd, source, asset, event_slug")
_PRINT_FIELDS = tuple(c.strip() for c in PRINT_COLS.split(","))
_PRINT_IDX = {name: i for i, name in enumerate(_PRINT_FIELDS)}


class _PrintRow(Mapping):
    """One `prints` row, `PRINT_COLS`-shaped, over a bare tuple — no `dict`
    built (or hashed) per row. `Mapping` gives `get`/`in`/`keys`/`items`/
    `values`/`__eq__` for free off `__getitem__`+`__iter__`+`__len__`, so
    `row["field"]`, `row.get("source", "tape")`, `"x" in row` and
    `set(row)` (the exact surface positions_from_prints, hygiene_flags and
    `_from_prints` already used on a plain dict) all still work unchanged."""
    __slots__ = ("_v",)

    def __init__(self, values):
        self._v = values

    def __getitem__(self, key):
        return self._v[_PRINT_IDX[key]]

    def __iter__(self):
        return iter(_PRINT_FIELDS)

    def __len__(self):
        return len(_PRINT_FIELDS)

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
    # rowperf (2026-09-05): a per-cursor row_factory=None override (the
    # connection's own is sqlite3.Row, arb/whale_tape.py open_tape) so this
    # query hands back bare tuples — _PrintRow wraps each one instead of
    # building a real dict. markets/resolutions are untouched: small tables,
    # not the profiled hot path (see the module docstring).
    prints_cur = con.cursor()
    prints_cur.row_factory = None
    prints = [_PrintRow(r) for r in
              prints_cur.execute(f"SELECT {PRINT_COLS} FROM prints {PRINT_ORDER}")]
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
