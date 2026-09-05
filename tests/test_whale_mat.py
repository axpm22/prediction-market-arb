"""L14 per-wallet materialization of positions + hygiene (matpos, 2026-09-05).

`whale_state.score_state` used to rebuild EVERY wallet's positions and hygiene
flags from EVERY print on every 60 s cycle — O(prints), 29.4 s on the 763,786
prints the tape held on 2026-09-05, growing ~700k rows/day while 2,700 wallet
histories backfill. `arb/whale_mat.py` recomputes only the wallets whose prints
changed and caches the result in `positions_mat`/`hygiene_mat`; `score_state`
reads the cache.

The cache is DERIVED: `prints` is still the record (rule 7) and the cache may
be dropped and rebuilt at any time, which is why DELETE+INSERT on these two
tables is not a rule-9 concern (that rule binds `flat_bets`/`bet_events` in the
ledger, and this is the tape).

Every test below is an EQUIVALENCE test against `_reference_score_state` — the
body of `score_state` frozen at "read every print, compute in memory" — because
the one thing this change must not do is move a number. `_reference_score_state`
is written out here rather than imported so it cannot rot into comparing the
real function against itself.
"""
import copy
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from arb import whale_mat, whale_score
from arb.config import load
from arb.whale_hygiene import hygiene_flags
from arb.whale_state import WhaleState, score_state
from arb.whale_tape import open_tape

from tests.test_whale_lane import ROOT

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def cfg():
    return copy.deepcopy(load(ROOT / "config.yaml"))


@pytest.fixture
def con(tmp_path):
    return open_tape(tmp_path / "tape.sqlite")


# ------------------------------------------------------- the frozen reference

def _reference_score_state(cfg, con, now):
    """score_state's body frozen at 94ef331 — every print read, positions and
    hygiene recomputed in memory. NOT imported from arb: this is the thing the
    materialized path has to keep agreeing with."""
    from arb.whale_state import MARKET_COLS, PRINT_COLS, PRINT_ORDER, RESOLUTION_COLS
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

    return WhaleState(markets=markets, resolutions=resolutions, positions=positions,
                      open_positions=open_pos,
                      hygiene_excluded=len(positions) - len(open_pos),
                      rates=rates, rate_n=rate_n, agg=agg, lookup=lookup)


def _assert_same(cfg, con, now=NOW, wallets=()):
    """score_state off the cache == the direct computation, field by field."""
    got, want = score_state(cfg, con, now), _reference_score_state(cfg, con, now)
    assert got.positions == want.positions
    assert got.open_positions == want.open_positions
    assert got.hygiene_excluded == want.hygiene_excluded
    assert got.agg == want.agg
    assert got.rates == want.rates and got.rate_n == want.rate_n
    for wallet in wallets:
        assert (got.lookup(wallet, "baseball_mlb", "WINNER")
                == want.lookup(wallet, "baseball_mlb", "WINNER"))
    return got


# ------------------------------------------------------------------- builders

def _market(con, cid, start=None, sport="baseball_mlb", mtype="WINNER"):
    con.execute("INSERT INTO markets (condition_id, sport, market_type, start_utc,"
                " tokens_json, first_seen, last_seen) VALUES (?,?,?,?,'[]',?,?)",
                (cid, sport, mtype, (start or NOW - timedelta(days=2)).isoformat(),
                 NOW.isoformat(), NOW.isoformat()))
    con.commit()


def _resolution(con, cid, winner=0, at=None):
    at = at or NOW - timedelta(hours=1)
    con.execute("INSERT INTO resolutions (condition_id, resolved_at,"
                " winner_outcome_index, status, source, raw_json, fetched_at)"
                " VALUES (?,?,?,'resolved','gamma','{}',?)",
                (cid, at.isoformat(), winner, at.isoformat()))
    con.commit()


def _print(con, wallet, cid, ts, side="BUY", price="0.50", size="1000", idx=0,
           tx=None, source="tape"):
    con.execute(
        "INSERT INTO prints (observed_at, ts, tx_hash, proxy_wallet, condition_id,"
        " asset, outcome_index, side, price, size, notional_usd, event_slug, source,"
        " raw_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'{}')",
        (NOW.isoformat(), int(ts), tx or f"0x{wallet}{cid}{ts}{side}{price}{size}{idx}",
         wallet, cid, f"tok{idx}", idx, side, price, size,
         str(Decimal(price) * Decimal(size)), f"evt-{cid}", source))
    con.commit()


def _seeded_tape(con, rng, n_prints=2000, n_wallets=12, n_markets=5):
    """Dust, SELLs, resolved and unresolved markets, and enough wallet/market
    overlap that hygiene's mm/wash/both_sides scans actually fire."""
    wallets = [f"0xw{i}" for i in range(n_wallets)]
    cids = [f"0xc{i}" for i in range(n_markets)]
    start = NOW - timedelta(days=5)
    for c in cids:
        _market(con, c, start=start)
    for c in cids[:max(1, n_markets // 2)]:
        _resolution(con, c, winner=rng.choice((0, 1)))
    prices, sizes = ("0.05", "0.35", "0.50", "0.90"), ("5", "300", "2000", "8000")
    for i in range(n_prints):
        _print(con, rng.choice(wallets), rng.choice(cids),
               int((start + timedelta(seconds=rng.randint(0, 4 * 24 * 3600))).timestamp()),
               side="SELL" if rng.random() < 0.15 else "BUY",
               price=rng.choice(prices), size=rng.choice(sizes),
               idx=rng.choice((0, 1)), tx=f"0xtx{i}")
    return wallets


# ---------------------------------------------------------------- equivalence

def test_score_state_off_the_cache_matches_the_direct_computation(cfg, con):
    """The base case: materialize a seeded tape once, then every number
    score_state serves is the one the whole-tape computation produced."""
    wallets = _seeded_tape(con, random.Random(20260905))

    whale_mat.materialize(con, cfg, NOW)

    state = _assert_same(cfg, con, wallets=wallets)
    assert state.positions, "the fixture must produce positions to compare"


def test_fresh_prints_for_some_wallets_are_picked_up(cfg, con):
    """(a) Live prints arrive for two of the wallets; only those two are
    recomputed, and the state still matches the whole-tape answer."""
    wallets = _seeded_tape(con, random.Random(1))
    whale_mat.materialize(con, cfg, NOW)

    ts = int((NOW - timedelta(hours=2)).timestamp())
    for wallet in wallets[:2]:
        _print(con, wallet, "0xc0", ts, size="5000")
    res = whale_mat.materialize(con, cfg, NOW)

    assert res["wallets"] == 2, res
    _assert_same(cfg, con, wallets=wallets)


def test_a_backfill_of_older_prints_rebuilds_that_wallets_whole_history(cfg, con):
    """(b) The backfill inserts prints OLDER than everything that wallet
    already has. FIFO must see the full history in ts order, so the wallet is
    recomputed whole — a delta on the new rows alone would mismatch the SELL
    matching."""
    wallets = _seeded_tape(con, random.Random(2))
    whale_mat.materialize(con, cfg, NOW)
    wallet = wallets[0]

    old = int((NOW - timedelta(days=30)).timestamp())
    _print(con, wallet, "0xc0", old, size="9000", source="activity")
    _print(con, wallet, "0xc0", old + 10, side="SELL", size="4000", source="activity")
    res = whale_mat.materialize(con, cfg, NOW)

    assert res["wallets"] == 1, res
    _assert_same(cfg, con, wallets=wallets)


def test_a_hygiene_flag_flipping_on_new_prints_is_picked_up(cfg, con):
    """(c) A wallet that was clean washes a token — BUY and SELL at the same
    price two seconds apart — and every one of its positions must leave
    `open_positions` in the cached path exactly as it does in the direct one."""
    _market(con, "0xcA")
    _resolution(con, "0xcA")
    ts = int((NOW - timedelta(days=1)).timestamp())
    _print(con, "0xwash", "0xcA", ts, size="1000")
    whale_mat.materialize(con, cfg, NOW)
    assert score_state(cfg, con, NOW).hygiene_excluded == 0

    _print(con, "0xwash", "0xcA", ts + 1, side="SELL", size="1")
    whale_mat.materialize(con, cfg, NOW)

    assert score_state(cfg, con, NOW).hygiene_excluded == 1
    _assert_same(cfg, con, wallets=["0xwash"])


def test_a_mat_version_bump_rebuilds_every_wallet(cfg, con, monkeypatch):
    """(d) `MAT_VERSION` is the escape hatch for a change to the FIFO or
    hygiene code: bumping it re-materializes every wallet from its prints,
    with no operator action and no stale row surviving."""
    wallets = _seeded_tape(con, random.Random(3), n_prints=400, n_wallets=5)
    whale_mat.materialize(con, cfg, NOW)
    assert whale_mat.materialize(con, cfg, NOW)["wallets"] == 0   # nothing dirty

    monkeypatch.setattr(whale_mat, "MAT_VERSION", whale_mat.MAT_VERSION + 1)
    res = whale_mat.materialize(con, cfg, NOW)

    assert res["wallets"] == len(wallets), res
    _assert_same(cfg, con, wallets=wallets)


def test_a_start_utc_arriving_later_redirties_that_markets_wallets(cfg, con):
    """§5 item 5: a market whose `start_utc` is filled or moved later changes
    `pregame` for positions already in the cache — and a market that gains a
    `sport` at all turns prints that scored NOTHING into positions. The tape
    dirties every wallet holding prints in that market, whoever wrote the row."""
    con.execute("INSERT INTO markets (condition_id, first_seen, last_seen)"
                " VALUES ('0xlate',?,?)", (NOW.isoformat(), NOW.isoformat()))
    con.commit()
    ts = int((NOW - timedelta(days=3)).timestamp())
    _print(con, "0xw", "0xlate", ts, size="1000")
    whale_mat.materialize(con, cfg, NOW)
    assert score_state(cfg, con, NOW).positions == []      # unmapped: no root

    con.execute("UPDATE markets SET sport='baseball_mlb', market_type='WINNER',"
                " start_utc=? WHERE condition_id='0xlate'",
                ((NOW - timedelta(days=1)).isoformat(),))
    con.commit()
    res = whale_mat.materialize(con, cfg, NOW)

    assert res["wallets"] == 1, res
    state = _assert_same(cfg, con, wallets=["0xw"])
    assert len(state.positions) == 1 and state.positions[0].pregame is True


def test_a_start_utc_that_does_not_move_leaves_the_cache_alone(cfg, con):
    """The other half: `persist_markets` rewrites every universe market every
    60 s cycle. If that counted as a change, every wallet would be dirty every
    cycle and the materialization would buy nothing."""
    _market(con, "0xc0")
    _print(con, "0xw", "0xc0", int((NOW - timedelta(days=3)).timestamp()))
    whale_mat.materialize(con, cfg, NOW)

    con.execute("UPDATE markets SET last_seen=?, sport='baseball_mlb',"
                " start_utc=start_utc WHERE condition_id='0xc0'", (NOW.isoformat(),))
    con.commit()

    assert whale_mat.materialize(con, cfg, NOW)["wallets"] == 0


# ------------------------------------------------------------------- bounding

def test_materialize_is_bounded_by_max_wallets_per_cycle(cfg, con):
    """The live lane must never stall on the first pass over an existing tape:
    a bounded slice a cycle, oldest-dirty first, leftovers next cycle."""
    wallets = _seeded_tape(con, random.Random(4), n_prints=300, n_wallets=10)
    cfg["whale"]["mat"]["max_wallets_per_cycle"] = 4

    first = whale_mat.materialize(con, cfg, NOW)

    assert first["wallets"] == 4
    assert first["dirty"] == len(wallets) - 4
    while whale_mat.materialize(con, cfg, NOW)["dirty"]:
        pass
    _assert_same(cfg, con, wallets=wallets)


def test_score_state_never_reads_the_prints_table(cfg, con):
    """The whole point: the 60 s scoring pass is O(cached positions), not
    O(every print ever collected). Traced at the sqlite level so a re-added
    read fails here rather than only as a slower cycle."""
    _seeded_tape(con, random.Random(5), n_prints=200, n_wallets=4)
    whale_mat.materialize(con, cfg, NOW)

    seen = []
    con.set_trace_callback(seen.append)
    try:
        score_state(cfg, con, NOW)
    finally:
        con.set_trace_callback(None)

    assert seen, "the trace callback saw no statement at all"
    assert not [s for s in seen if "prints" in s.lower()], seen


def _vm_steps(con, fn):
    """SQLite VM opcodes a call costs, in thousands. A COUNT of work done, not
    a clock: it does not move with this host's background load (which runs the
    real supervisor while the suite runs), so a ratio taken from it means
    something a wall-clock bound on the same box never could — that is why the
    400k-row absolute guard in tests/test_whale_report.py was deleted rather
    than re-tuned (review of 94ef331: at 15.0 s the old code passed it too)."""
    steps = [0]

    def tick():
        steps[0] += 1
    con.set_progress_handler(tick, 1000)
    try:
        fn()
    finally:
        con.set_progress_handler(None, 0)
    return steps[0]


def test_score_state_work_does_not_grow_with_the_tape(cfg, tmp_path):
    """The property the whole pass exists for: the 60 s scoring pass is
    O(cached positions), not O(prints on the tape).

    Two tapes with the SAME wallets, markets and positions, one carrying 4x
    the prints — the extra ones are DUST (under `min_print_usd`), which forms
    no position, so anything that still reads them is doing work it does not
    need. Before this pass score_state read, wrapped and grouped every one of
    them; the ratio below was ~4x by construction."""
    def build(dust_per_wallet):
        c = open_tape(tmp_path / f"tape{dust_per_wallet}.sqlite")
        wallets = _seeded_tape(c, random.Random(11), n_prints=600, n_wallets=8)
        for w in wallets:                       # dust: no position, but rows
            for i in range(dust_per_wallet):
                _print(c, w, "0xc0", int((NOW - timedelta(days=2)).timestamp()) + i,
                       price="0.50", size="200", tx=f"0xdust{w}{i}{dust_per_wallet}")
        whale_mat.materialize(c, cfg, NOW)
        return c

    lean, fat = build(0), build(225)             # 600 prints vs 2,400

    assert (fat.execute("SELECT COUNT(*) c FROM prints").fetchone()["c"]
            >= 4 * lean.execute("SELECT COUNT(*) c FROM prints").fetchone()["c"])
    assert (score_state(cfg, fat, NOW).positions
            == score_state(cfg, lean, NOW).positions)     # same positions, 4x prints
    lean_work = _vm_steps(lean, lambda: score_state(cfg, lean, NOW))
    fat_work = _vm_steps(fat, lambda: score_state(cfg, fat, NOW))

    assert fat_work <= 1.5 * lean_work, (lean_work, fat_work)


def test_rebuild_requeues_every_wallet(cfg, con):
    """`arb whale-mat --rebuild`'s engine: drop the derived rows and rebuild
    them from the record. The cache is a cache."""
    wallets = _seeded_tape(con, random.Random(6), n_prints=300, n_wallets=6)
    whale_mat.materialize(con, cfg, NOW)
    con.execute("DELETE FROM positions_mat")     # corrupt the cache by hand
    con.commit()

    assert whale_mat.mark_all_dirty(con) == len(wallets)
    while whale_mat.materialize(con, cfg, NOW)["dirty"]:
        pass

    _assert_same(cfg, con, wallets=wallets)


def test_materialize_reports_rows_backlog_and_seconds(cfg, con):
    """The ops line (§12): how big the cache is, how far behind it is, and
    what the pass cost — the three numbers that say whether the lane is
    keeping up."""
    _seeded_tape(con, random.Random(7), n_prints=300, n_wallets=6)
    cfg["whale"]["mat"]["max_wallets_per_cycle"] = 2

    res = whale_mat.materialize(con, cfg, NOW)

    assert res["wallets"] == 2 and res["dirty"] == 4
    assert res["rows"] == con.execute("SELECT COUNT(*) c FROM positions_mat"
                                      ).fetchone()["c"]
    assert isinstance(res["secs"], float) and res["secs"] >= 0
