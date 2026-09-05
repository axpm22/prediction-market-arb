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
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from arb import whale_mat, whale_migrate, whale_score
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
    con.set_progress_handler(tick, 100)
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


def test_a_tape_without_the_cache_reads_as_out_not_as_a_crash(cfg, tmp_path):
    """Every read-only reader — the daily report, the dashboard, `whale-score`,
    `whale-rank` — goes through `open_tape_ro` and then calls `score_state`,
    which reads the cache. Only a WRITER (`open_tape`) creates it, so between
    deploying this and the lane's next cycle a tape is readable and has no
    cache. That must report "tape out", the degraded path the report already
    serves, not raise `no such table` out of the daily report."""
    from arb import report_whale
    import sqlite3
    path = tmp_path / "pre_matpos.sqlite"
    old = sqlite3.connect(str(path))            # a tape as it was before matpos
    old.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    old.execute("CREATE TABLE prints (id INTEGER PRIMARY KEY, proxy_wallet TEXT)")
    old.commit()
    old.close()
    cfg["whale"]["tape_db"] = str(path)

    assert report_whale.open_tape_ro(cfg) is None


def test_cmd_whale_mat_rebuild_runs_to_completion(cfg, con, tmp_path, capsys):
    """The CLI: `--rebuild` is unbounded, so it never leaves a backlog behind
    however small `max_wallets_per_cycle` is. Writes to the tape and opens no
    ledger at all (`store` is None here, and nothing dereferences it)."""
    from arb.commands import cmd_whale_mat
    wallets = _seeded_tape(con, random.Random(8), n_prints=300, n_wallets=6)
    con.execute("DELETE FROM positions_mat")
    con.commit()
    cfg["whale"]["mat"]["max_wallets_per_cycle"] = 1
    cfg["whale"]["tape_db"] = str(tmp_path / "tape.sqlite")

    rc = cmd_whale_mat(cfg, None, type("A", (), {"rebuild": True})())

    assert rc == 0
    out = capsys.readouterr().out
    assert f"rebuild: {len(wallets)} wallet(s) queued" in out
    assert "0 still dirty" in out
    _assert_same(cfg, con, wallets=wallets)


def test_cmd_whale_mat_without_rebuild_is_one_bounded_pass(cfg, con, tmp_path, capsys):
    """Bare, it does exactly what a lane cycle does — one bounded slice — and
    says how much is still queued, because the lane scores off a partial cache
    until that reaches zero (KNOWN_ISSUES.md)."""
    from arb.commands import cmd_whale_mat
    _seeded_tape(con, random.Random(9), n_prints=300, n_wallets=6)
    cfg["whale"]["mat"]["max_wallets_per_cycle"] = 2
    cfg["whale"]["tape_db"] = str(tmp_path / "tape.sqlite")

    cmd_whale_mat(cfg, None, type("A", (), {"rebuild": False})())

    out = capsys.readouterr().out
    assert "2 wallet(s) recomputed, 4 still dirty" in out
    assert "partial cache" in out


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


# ------------------------------------------- the fix pass (review of 783301f)

def test_a_markets_change_committed_mid_pass_is_not_read_from_a_stale_snapshot(
        cfg, tmp_path, monkeypatch):
    """C1. `materialize` snapshotted `markets` ONCE per pass and recomputed
    every wallet from that snapshot, while the `markets` triggers only
    `INSERT OR IGNORE` into a queue the wallet was ALREADY on. So a
    `start_utc`/`sport`/`market_type` change another process commits between
    the snapshot and that wallet's recompute (the backfill child's mapper,
    `_local_remap`, `whale_resolve` — four writers) was applied from the STALE
    snapshot AND dequeued: for a wallet with no further prints the cache
    diverged from the tape permanently.

    Two real connections, and the change lands in the window that used to be
    unrecoverable. After the pass the cache must equal the from-scratch truth.
    """
    path = tmp_path / "c1.sqlite"
    con, other = open_tape(path), open_tape(path)
    _market(con, "0xcM", start=NOW - timedelta(days=2))
    ts = int((NOW - timedelta(days=3)).timestamp())       # BEFORE start: pregame
    _print(con, "0xw", "0xcM", ts, size="2000")
    whale_mat.materialize(con, cfg, NOW)
    assert score_state(cfg, con, NOW).positions[0].pregame is True

    _print(con, "0xw", "0xcM", ts + 60, size="3000")      # the wallet is dirty
    real, fired = whale_mat._wallet, []

    def hook(*a, **kw):
        if not fired:                                     # once, before 0xw's recompute
            fired.append(1)
            other.execute("UPDATE markets SET start_utc = ? WHERE condition_id = ?",
                          ((NOW - timedelta(days=4)).isoformat(), "0xcM"))
            other.commit()                                # trigger: INSERT OR IGNORE = no-op
        return real(*a, **kw)

    monkeypatch.setattr(whale_mat, "_wallet", hook)
    res = whale_mat.materialize(con, cfg, NOW)

    assert fired, "the hook never ran — the test asserts nothing"
    assert res["dirty"] == 0, "the wallet was dequeued, so the pass had to be correct"
    state = _assert_same(cfg, con, wallets=["0xw"])
    assert state.positions[0].pregame is False            # the market now starts EARLIER


def test_a_markets_change_committed_after_the_write_requeues_the_wallet(cfg, tmp_path,
                                                                        monkeypatch):
    """The other half of C1's window: a change that commits AFTER the wallet's
    own transaction is picked up by the trigger, because the dequeue has
    already happened. Together the two tests cover every ordering — there is
    no third one, since the recompute holds the write lock."""
    path = tmp_path / "c1b.sqlite"
    con, other = open_tape(path), open_tape(path)
    _market(con, "0xcM", start=NOW - timedelta(days=2))
    _print(con, "0xw", "0xcM", int((NOW - timedelta(days=3)).timestamp()), size="2000")
    whale_mat.materialize(con, cfg, NOW)

    other.execute("UPDATE markets SET start_utc = ? WHERE condition_id = ?",
                  ((NOW - timedelta(days=4)).isoformat(), "0xcM"))
    other.commit()

    assert whale_mat.materialize(con, cfg, NOW)["wallets"] == 1
    _assert_same(cfg, con, wallets=["0xw"])


def test_the_cache_is_partial_only_while_a_full_rebuild_drains(cfg, con):
    """I1. A handful of dirty wallets in steady state is NOT a partial cache —
    every other wallet is present and correct. A full rebuild is different:
    until the queue drains the population itself is incomplete, and V5's
    cutoff is a percentile OF that population."""
    wallets = _seeded_tape(con, random.Random(31), n_prints=300, n_wallets=6)
    cfg["whale"]["mat"]["max_wallets_per_cycle"] = 2
    while whale_mat.materialize(con, cfg, NOW)["dirty"]:
        pass
    assert whale_mat.cache_partial(con) is False

    _print(con, wallets[0], "0xc0", int((NOW - timedelta(hours=1)).timestamp()))
    assert whale_mat.cache_partial(con) is False          # steady state: not partial

    whale_mat.mark_all_dirty(con)

    assert whale_mat.cache_partial(con) is True
    while whale_mat.materialize(con, cfg, NOW)["dirty"]:
        assert whale_mat.cache_partial(con) is True
    assert whale_mat.cache_partial(con) is False


def test_freshness_reports_how_far_behind_the_cache_is(cfg, con):
    """I2. `open_tape_ro` probed that `positions_mat` EXISTS, never that it is
    CURRENT — and no read-only reader (report, dashboard, whale-score,
    whale-rank) can make it current. Two numbers say it: prints above the
    watermark, and how long the queue has been non-empty."""
    _seeded_tape(con, random.Random(32), n_prints=300, n_wallets=6)
    while whale_mat.materialize(con, cfg, NOW)["dirty"]:
        pass

    fresh = whale_mat.freshness(con, cfg, NOW)
    assert (fresh["behind"], fresh["dirty"], fresh["stale"]) == (0, 0, False)
    assert fresh["at"] == NOW.isoformat()

    cfg["whale"]["mat"]["stale_prints"] = 5
    for i in range(6):
        _print(con, "0xnew", "0xc0", int(NOW.timestamp()) - i, tx=f"0xbehind{i}")

    stale = whale_mat.freshness(con, cfg, NOW)
    assert stale["behind"] == 6 and stale["stale"] is True
    assert stale["note"] == "cache: stale (6 behind, 0 dirty)"


def test_a_queue_that_never_drains_reads_as_stale(cfg, con):
    """The other trip-wire: the watermark can be current while the queue never
    empties (a lane that materializes but cannot keep up). `stale_secs` is how
    long a non-empty queue is allowed to persist."""
    _seeded_tape(con, random.Random(33), n_prints=300, n_wallets=6)
    cfg["whale"]["mat"]["max_wallets_per_cycle"] = 1
    whale_mat.materialize(con, cfg, NOW)

    assert whale_mat.freshness(con, cfg, NOW)["stale"] is True      # never been clean
    while whale_mat.materialize(con, cfg, NOW)["dirty"]:
        pass
    whale_mat.mark_all_dirty(con)
    whale_mat.materialize(con, cfg, NOW)

    assert whale_mat.freshness(con, cfg, NOW)["stale"] is False     # clean 0s ago
    late = NOW + timedelta(seconds=int(cfg["whale"]["mat"]["stale_secs"]) + 1)
    assert whale_mat.freshness(con, cfg, late)["stale"] is True


def test_every_read_only_reader_warns_when_the_cache_is_stale(cfg, con, tmp_path, caplog):
    """The warning goes in the ONE place `whale-score`, `whale-rank`, the
    dashboard bundle and the daily report all go through, so no reader can
    forget it (rule 12's spirit: one predicate, one site)."""
    from arb import report_whale
    _seeded_tape(con, random.Random(34), n_prints=300, n_wallets=6)
    while whale_mat.materialize(con, cfg, NOW)["dirty"]:
        pass
    cfg["whale"]["tape_db"] = str(tmp_path / "tape.sqlite")
    cfg["whale"]["mat"]["stale_prints"] = 5
    for i in range(6):
        _print(con, "0xnew", "0xc0", int(NOW.timestamp()) - i, tx=f"0xbehind{i}")

    with caplog.at_level("WARNING"):
        assert report_whale.open_tape_ro(cfg) is not None

    assert [r for r in caplog.records if "STALE" in r.getMessage()], caplog.text


def test_the_ops_line_shows_a_stale_cache(cfg, con, tmp_path):
    """...and the daily report's console line says it in the same words the
    ops line already uses for the backlog."""
    from arb import report_whale
    from arb.report_whale_md import whale_line
    _seeded_tape(con, random.Random(35), n_prints=300, n_wallets=6)
    while whale_mat.materialize(con, cfg, NOW)["dirty"]:
        pass
    cfg["whale"]["tape_db"] = str(tmp_path / "tape.sqlite")
    cfg["whale"]["mat"]["stale_prints"] = 5
    for i in range(6):
        _print(con, "0xnew", "0xc0", int(NOW.timestamp()) - i, tx=f"0xbehind{i}")

    collector = report_whale._collector(cfg, con, NOW)

    assert collector["mat_stale"] == "cache: stale (6 behind, 0 dirty)"
    line = whale_line({"roi": {}, "primary_root": "sports", "primary_variant": "V1",
                       "signals": {"evaluated_24h": 0, "fired_24h": 0},
                       "collector": collector, "tape_ok": True})
    assert "cache: stale (6 behind, 0 dirty)" in line


def test_the_loader_rejects_an_unbounded_max_wallets_per_cycle(cfg):
    """M4. `materialize` reads `cap=0` as NO BOUND — that is the
    `whale whale-mat --rebuild` path. A 0 in config would hand the 60 s lane
    cycle an unbounded rebuild instead of stopping it, so it fails at boot."""
    from arb.config import ConfigError, _check_invariants
    cfg["whale"]["mat"]["max_wallets_per_cycle"] = 0

    with pytest.raises(ConfigError, match="max_wallets_per_cycle"):
        _check_invariants(cfg)


def test_cmd_whale_mat_refuses_a_tape_that_does_not_exist(cfg, tmp_path, capsys):
    """M5. It opened the tape with `whale_tape.open_tape`, which CREATES the
    file — so a typo'd `whale.tape_db` reported a healthy empty cache and left
    a new tape behind. `whale-score`/`whale-rank` already fail loudly here."""
    from arb.commands import cmd_whale_mat
    missing = tmp_path / "typo.sqlite"
    cfg["whale"]["tape_db"] = str(missing)

    rc = cmd_whale_mat(cfg, None, type("A", (), {"rebuild": False})())

    assert rc == 1
    assert not missing.exists(), "a read of a missing tape must not create one"
    assert "not found" in capsys.readouterr().out


def test_the_lane_suppresses_v5_while_a_rebuild_drains(cfg, tmp_path):
    """The wiring for I1: the lane computes the flag from its own queue and
    hands it to `evaluate`. A rebuild leaves the cache STALE-BUT-COMPLETE (
    `mark_all_dirty` does not clear `positions_mat`), so the whales are all
    still there and every other variant evaluates normally — only V5, whose
    bar is relative to the population, is pinned."""
    from tests.test_whale_lane import _ledger, _run, _tape
    cfg["whale"]["min_score_lb"] = Decimal("-0.10")     # tests/test_whale_lane.py's
    cfg["whale"]["exit"]["min_score_lb"] = Decimal("-0.20")   # own cfg fixture
    store, con = _ledger(tmp_path), _tape(tmp_path)
    first = _run(cfg, store, con)
    assert "cache-partial" not in first["suppressed"], first["suppressed"]

    whale_mat.mark_all_dirty(con)
    cfg["whale"]["mat"]["max_wallets_per_cycle"] = 1
    res = _run(cfg, store, con)

    assert res["mat"]["dirty"], "the rebuild must still be draining"
    assert res["suppressed"].get("cache-partial") == 1, res["suppressed"]


def test_markets_upsert_with_a_queued_wallet_does_not_hit_the_dirty_unique(tmp_path):
    # live 2026-09-05: `INSERT OR IGNORE` inside the markets trigger is overridden by
    # the outer upsert's conflict clause -> `UNIQUE constraint failed: mat_dirty.proxy_wallet`
    # crashed the backfill child at the non-sports universe step every cycle
    import sqlite3
    from datetime import datetime, timezone
    from arb import whale_tape
    from arb.whale_universe import Market, persist_markets
    con = whale_tape.open_tape(str(tmp_path / "tape.sqlite"))
    now = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
    con.execute("INSERT INTO markets (condition_id, sport, market_type, start_utc, tokens_json, "
                "slug, first_seen, last_seen) VALUES ('0xc', 'politics', 'BINARY', "
                "'2026-10-01T00:00:00+00:00', '[]', 'x', '2026-09-01', '2026-09-01')")
    con.execute("INSERT INTO prints (observed_at, ts, tx_hash, proxy_wallet, condition_id, asset, "
                "outcome, outcome_index, side, price, size, notional_usd, source) VALUES "
                "('2026-09-01', 1, 'h', '0xw', '0xc', 'a', 'Yes', 0, 'BUY', '0.5', '10', '5', 'tape')")
    con.execute("INSERT INTO mat_dirty (proxy_wallet) VALUES ('0xw')")   # already queued
    con.commit()
    m = Market(condition_id="0xc", sport="politics", market_type="BINARY", line=None,
               start_utc="2026-11-01T00:00:00+00:00", event_key="pm:x", tokens=(),
               volume_24h="1000")
    persist_markets(con, [m], now, first_sight_wins=False)   # upsert -> UPDATE trigger fires
    assert con.execute("SELECT COUNT(*) FROM mat_dirty WHERE proxy_wallet='0xw'").fetchone()[0] == 1


# --- the markets triggers' DDL lifecycle (C1, fix pass 3, 2026-09-05) --------
#
# df97ad3 fixed the trigger BODY by putting `DROP TRIGGER IF EXISTS` + a bare
# `CREATE TRIGGER` into MAT_SCHEMA — the script `open_tape` runs on EVERY open.
# That makes every process start-up a WRITER (the DROP takes sqlite's write
# lock), and `executescript` autocommits each statement, so the trigger pair is
# briefly absent on every open and a mid-script abort leaves the tape with no
# triggers at all. The bodies belong in `CREATE TRIGGER IF NOT EXISTS` and the
# replacement of an OLD tape's copies in the one-shot migration.

_OLD_TRIGGERS = ("""CREATE TRIGGER trg_markets_mat_update AFTER UPDATE ON markets
    WHEN NEW.sport IS NOT OLD.sport OR NEW.market_type IS NOT OLD.market_type
         OR NEW.start_utc IS NOT OLD.start_utc
    BEGIN
        INSERT OR IGNORE INTO mat_dirty (proxy_wallet)
            SELECT DISTINCT proxy_wallet FROM prints
             WHERE condition_id = NEW.condition_id;
    END;""", """CREATE TRIGGER trg_markets_mat_insert AFTER INSERT ON markets
    WHEN NEW.sport IS NOT NULL
    BEGIN
        INSERT OR IGNORE INTO mat_dirty (proxy_wallet)
            SELECT DISTINCT proxy_wallet FROM prints
             WHERE condition_id = NEW.condition_id;
    END;""")


def _old_ddl_tape(path):
    """A tape carrying the PRE-df97ad3 trigger bodies — the shape every live
    tape opened before this fix is in."""
    con = open_tape(path)
    for name, ddl in zip(whale_migrate.MAT_TRIGGERS, _OLD_TRIGGERS):
        con.execute(f"DROP TRIGGER IF EXISTS {name}")
        con.execute(ddl)
    con.commit()
    con.close()


def _trigger_rows(con):
    return {r[0]: (r[1], r[2]) for r in con.execute(
        "SELECT name, sql, rowid FROM sqlite_master WHERE type='trigger'"
        " AND name LIKE 'trg_markets_mat_%'")}


def test_an_old_ddl_tape_has_its_markets_triggers_replaced_once(tmp_path):
    """C1(b): the replacement is a ONE-SHOT migration, not something every
    open redoes. After the first open both triggers carry the new
    `NOT EXISTS` body; a second open must not touch sqlite_master at all —
    same rowid, same sql — because a DROP/CREATE there is a write."""
    path = tmp_path / "w.sqlite"
    _old_ddl_tape(path)

    con = open_tape(path)

    rows = _trigger_rows(con)
    assert set(rows) == set(whale_migrate.MAT_TRIGGERS)
    for name, (sql, _rowid) in rows.items():
        assert whale_migrate.MAT_TRIGGER_MARK in sql, name
        assert "INSERT OR IGNORE" not in sql, name
    con.close()

    again = open_tape(path)
    assert _trigger_rows(again) == rows, "a second open re-wrote the triggers"


def test_the_trigger_replacement_is_one_transaction(tmp_path):
    """C1(b), the other half: `executescript` autocommits each DDL, so an
    abort halfway through left the tape with one trigger dropped and never
    recreated. Under a single transaction a failure leaves the OLD pair
    intact — the tape is never trigger-less."""
    path = tmp_path / "w.sqlite"
    _old_ddl_tape(path)
    con = open_tape(path)                       # migrates it
    for name, ddl in zip(whale_migrate.MAT_TRIGGERS, _OLD_TRIGGERS):   # ... and back to old
        con.execute(f"DROP TRIGGER IF EXISTS {name}")
        con.execute(ddl)
    con.commit()
    before = _trigger_rows(con)
    calls = []

    def boom(sql, *a, **kw):
        calls.append(sql)
        if sql.startswith("CREATE TRIGGER") and len(
                [c for c in calls if c.startswith("CREATE TRIGGER")]) == 2:
            raise sqlite3.OperationalError("disk I/O error")
        return con.execute(sql, *a, **kw)

    spy = type("Spy", (), {"execute": staticmethod(boom),
                           "commit": con.commit, "rollback": con.rollback})()
    with pytest.raises(sqlite3.OperationalError):
        whale_migrate._migrate_triggers(spy)

    assert _trigger_rows(con) == before, "a half-applied migration left the tape"


def test_open_tape_takes_no_write_lock_on_an_already_migrated_tape(tmp_path):
    """C1(a), reproduced live: with the DROP in MAT_SCHEMA every child's
    start-up asked for sqlite's write lock, and the backfill child's long
    `persist_markets` transaction holds it — `database is locked` after the
    busy_timeout. An open that only runs `... IF NOT EXISTS` no-ops must
    succeed straight through a held `BEGIN IMMEDIATE`."""
    path = tmp_path / "w.sqlite"
    open_tape(path).close()
    holder = sqlite3.connect(str(path), timeout=30)
    done: list = []

    def _open_and_close():
        # sqlite3 connections are thread-bound (check_same_thread=True,
        # open_tape's default): close it HERE, not from the main thread
        # after join, or a passing run leaks it (minor, fix pass 4).
        open_tape(path).close()
        done.append(True)

    try:
        holder.execute("PRAGMA journal_mode=WAL")
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("INSERT INTO markets (condition_id) VALUES ('0xheld')")
        t = threading.Thread(target=_open_and_close, daemon=True)

        t.start()
        t.join(timeout=10)

        assert not t.is_alive() and done, "open_tape blocked on the write lock"
        holder.rollback()
    finally:
        holder.close()
