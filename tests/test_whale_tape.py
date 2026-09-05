"""L14 collector. Its OWN SQLite file (rule 9 unreachable) and NO network:
`get_json` is injected, every response comes from a Task-1 fixture, and the
ledger is a three-row in-memory stand-in (the real one is 37 GB)."""
import copy
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from arb.config import load
from arb.venues.base import VenueError
from arb import (whale_backfill, whale_holdings, whale_hygiene, whale_map, whale_resolve,
                 whale_score, whale_sweep, whale_tape, whale_universe)

FIX = Path(__file__).resolve().parent / "fixtures"
NOW = datetime(2026, 9, 3, 17, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _fresh_bucket():
    """The rate guard is one bucket per PROCESS (so a single command cannot
    burst 3x the cap); here it is one per TEST, or the suite's hundreds of
    fake calls would pace themselves against the real 30/min."""
    whale_tape._BUCKET = None
    yield
    whale_tape._BUCKET = None


def _cfg(**over):
    cfg = copy.deepcopy(load(Path(__file__).resolve().parent.parent / "config.yaml"))
    cfg["whale"].update(over)
    return cfg


def _fixture(name):
    return json.loads((FIX / f"{name}.json").read_text())


class FakeGet:
    """Injected in place of arb.venues.base.get_json. Records every URL."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.urls = []

    def __call__(self, url, cfg, **kw):
        self.urls.append(url)
        r = self.responses[min(len(self.urls) - 1, len(self.responses) - 1)]
        if isinstance(r, Exception):
            raise r
        return r


# --- ledger stand-in --------------------------------------------------------

def _ledger(rows, poll1_at=None, poll2_at=None):
    """polls/quotes with the ledger's own columns; rows are quote dicts.
    Both polls default to well inside `data_stale_alarm_secs` (900s) of NOW —
    poll 1 further back only to keep the fallback-ordering tests meaningful,
    never far enough to trip the staleness guard on its own; a test that
    wants a genuinely stale poll passes an explicit poll1_at/poll2_at."""
    poll1_at = poll1_at or (NOW - timedelta(minutes=10))
    poll2_at = poll2_at or (NOW - timedelta(minutes=2))
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE polls (id INTEGER PRIMARY KEY, started_at TEXT, kind TEXT)")
    con.execute("CREATE TABLE quotes (id INTEGER PRIMARY KEY, poll_id INTEGER, "
                "fetched_at TEXT, event_key TEXT, category TEXT, sport TEXT, venue TEXT, "
                "market_type TEXT, line TEXT, outcome TEXT, side TEXT, start_utc TEXT, "
                "venue_market_id TEXT)")
    con.execute("INSERT INTO polls (id, started_at, kind) VALUES (1, ?, 'watch')",
                (poll1_at.isoformat(),))
    con.execute("INSERT INTO polls (id, started_at, kind) VALUES (2, ?, 'watch')",
                (poll2_at.isoformat(),))
    for r in rows:
        con.execute(
            "INSERT INTO quotes (poll_id, event_key, category, sport, venue, market_type, "
            "line, outcome, side, start_utc, venue_market_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (r.get("poll_id", 2), r.get("event_key", "ek"), r.get("category", "sports"),
             r.get("sport", "baseball_mlb"), r.get("venue", "polymarket"),
             r.get("market_type", "WINNER"), r.get("line"), r["outcome"],
             r.get("side", "YES"), r["start_utc"], r["venue_market_id"]))
    con.commit()
    return con


def _q(cond, token, outcome, start, **kw):
    d = {"venue_market_id": f"{cond}:{token}", "outcome": outcome, "start_utc": start}
    d.update(kw)
    return d


IN_WINDOW = (NOW + timedelta(hours=3)).isoformat()
TOO_OLD = (NOW - timedelta(hours=30)).isoformat()      # lookback_hours = 6
TOO_FAR = (NOW + timedelta(hours=200)).isoformat()     # lookahead_hours = 48


# --- schema -----------------------------------------------------------------

def test_schema_creates_every_table_and_cannot_reach_the_ledger(tmp_path):
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"prints", "markets", "wallets", "leaderboard_snapshots", "resolutions",
            "signals", "base_rates", "meta"} <= names
    # rule 9: the ledger's tables are structurally unreachable from this file
    assert "flat_bets" not in names and "bet_events" not in names


def test_leaderboard_snapshots_is_indexed_by_snapshot_at(tmp_path):
    """Fix 3 (V4 review): `ranked_wallets()` (arb/lanes/whale_book.py) runs
    every 60 s cycle now, not just at backfill time — a table scan per poll
    is wasted work once the tape has any leaderboard history."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    idx_sql = [r[0] for r in con.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='leaderboard_snapshots'")]
    assert any(sql and "snapshot_at" in sql for sql in idx_sql)


def test_gamma_ids_per_call_is_20_and_a_bigger_batch_is_refused():
    """Legacy re-map (2026-09-04, data/verified_facts.md last row): a live
    probe verified 20/20 ids matched in one Gamma condition_ids call (n=2,
    10, 20 all clean); n=50 silently CAPPED at 20 rows with no error — a
    batch above 20 would count real markets as absent with nothing to catch
    it. `_gamma_url` is the one builder every Gamma condition_ids caller
    (whale_map's mapper, whale_resolve's resolution reader) goes through, so
    the guard belongs there, not in each caller."""
    assert whale_tape.GAMMA_IDS_PER_CALL == 20
    ids20 = [f"0x{i}" for i in range(20)]
    whale_tape._gamma_url(ids20)                       # exactly the cap: must not raise
    with pytest.raises(ValueError, match="GAMMA_IDS_PER_CALL"):
        whale_tape._gamma_url(ids20 + ["0xtoomany"])


# --- migration (I2, review of a90a08d/fce71ea) ------------------------------

def test_migrate_adds_holders_ratio_to_a_pre_1b_tape(tmp_path):
    """A tape opened before the §7 item 1b amendment has a `signals` table
    with no `holders_ratio` column. `open_tape` must ALTER it in, leave the
    pre-existing row's value NULL (nothing to migrate INTO it), and let a new
    insert use the column normally."""
    path = tmp_path / "w.sqlite"
    old = sqlite3.connect(str(path))
    old.execute("""CREATE TABLE signals (
        id INTEGER PRIMARY KEY, evaluated_at TEXT, condition_id TEXT, side TEXT, variant TEXT,
        points_yes TEXT, points_no TEXT, ratio TEXT, whale_ids_json TEXT, vwap TEXT,
        touch_ask TEXT, touch_bid TEXT, depth_usd TEXT, fired INTEGER, suppressed_reason TEXT,
        bet_ids_json TEXT, flagged_edge TEXT)""")               # pre-1b DDL: no holders_ratio
    old.execute("INSERT INTO signals (evaluated_at, condition_id, variant) "
                "VALUES ('2026-09-01T00:00:00+00:00', '0xold', 'V1')")
    old.commit()
    old.close()

    con = whale_tape.open_tape(path)

    cols = {r[1] for r in con.execute("PRAGMA table_info(signals)")}
    assert "holders_ratio" in cols
    old_row = con.execute(
        "SELECT holders_ratio FROM signals WHERE condition_id='0xold'").fetchone()
    assert old_row["holders_ratio"] is None
    con.execute("INSERT INTO signals (evaluated_at, condition_id, variant, holders_ratio) "
                "VALUES ('2026-09-04T00:00:00+00:00', '0xnew', 'V1', '0.750000')")
    con.commit()
    new_row = con.execute(
        "SELECT holders_ratio FROM signals WHERE condition_id='0xnew'").fetchone()
    assert new_row["holders_ratio"] == "0.750000"


def test_migrate_adds_volume_24h_to_a_pre_nonsports_tape(tmp_path):
    """I4 (fix pass): the non-sports amendment added `markets.volume_24h`, and
    a tape written before it has the 10-column DDL. `open_tape` must ALTER the
    column in, leave the pre-existing row NULL, and let `persist_markets`'
    11-column INSERT work — without it the whole non-sports universe write
    fails on every cycle against a live tape."""
    path = tmp_path / "w.sqlite"
    old = sqlite3.connect(str(path))
    old.execute("""CREATE TABLE markets (
        condition_id TEXT PRIMARY KEY, sport TEXT, market_type TEXT, line TEXT,
        start_utc TEXT, event_key TEXT, tokens_json TEXT, slug TEXT,
        first_seen TEXT, last_seen TEXT)""")            # pre-nonsports DDL: no volume_24h
    old.execute("""CREATE TABLE signals (
        id INTEGER PRIMARY KEY, evaluated_at TEXT, condition_id TEXT, side TEXT, variant TEXT,
        points_yes TEXT, points_no TEXT, ratio TEXT, whale_ids_json TEXT, vwap TEXT,
        touch_ask TEXT, touch_bid TEXT, depth_usd TEXT, fired INTEGER, suppressed_reason TEXT,
        bet_ids_json TEXT, flagged_edge TEXT)""")       # pre-1b DDL: no holders_ratio
    old.execute("INSERT INTO markets (condition_id, sport) VALUES ('0xold', 'politics')")
    old.execute("INSERT INTO signals (evaluated_at, condition_id, variant) "
                "VALUES ('2026-09-01T00:00:00+00:00', '0xold', 'V1')")
    old.commit()
    old.close()

    con = whale_tape.open_tape(path)

    assert "volume_24h" in {r[1] for r in con.execute("PRAGMA table_info(markets)")}
    assert "holders_ratio" in {r[1] for r in con.execute("PRAGMA table_info(signals)")}
    assert con.execute("SELECT volume_24h FROM markets WHERE condition_id='0xold'"
                       ).fetchone()["volume_24h"] is None
    assert con.execute("SELECT holders_ratio FROM signals WHERE condition_id='0xold'"
                       ).fetchone()["holders_ratio"] is None
    from arb.whale_universe import Market, persist_markets
    persist_markets(con, [Market(condition_id="0xnew", sport="politics",
                                 market_type="BINARY", line=None,
                                 start_utc="2026-09-05T00:00:00+00:00",
                                 event_key="pm:e", tokens=(("t0", "Yes"), ("t1", "No")),
                                 volume_24h="25000")],
                    datetime(2026, 9, 4, tzinfo=timezone.utc))
    assert con.execute("SELECT volume_24h FROM markets WHERE condition_id='0xnew'"
                       ).fetchone()["volume_24h"] == "25000"


class _RacyCon:
    """Wraps a real, already-migrated connection so its PRAGMA table_info
    reports `holders_ratio` missing exactly once — simulating the other
    process (`whale` or `settle`, both call `open_tape` on start) winning the
    ADD COLUMN race between this process's own check and its ALTER."""

    def __init__(self, real):
        self._real = real

    def execute(self, sql, *a, **kw):
        if sql.startswith("PRAGMA table_info(signals)"):
            return [(0, "id", "INTEGER", 0, None, 1)]     # holders_ratio: missing (stale read)
        return self._real.execute(sql, *a, **kw)

    def commit(self):
        self._real.commit()

    def rollback(self):
        self._real.rollback()


def test_open_tape_adds_idx_prints_score_to_a_pre_posperf_tape(tmp_path):
    """posperf (2026-09-04): idx_prints_score(proxy_wallet, condition_id,
    outcome_index, ts) is new — a tape opened before this fix has a `prints`
    table (and its three older indexes) but not this one. Unlike the
    holders_ratio column above, an index needs no arb/whale_migrate.py ALTER
    entry: `CREATE INDEX IF NOT EXISTS` in SCHEMA is already self-migrating,
    since `open_tape` runs the whole SCHEMA script (not just `_migrate`) on
    every open, old tapes included."""
    path = tmp_path / "w.sqlite"
    old = sqlite3.connect(str(path))
    old.execute("""CREATE TABLE prints (
        id INTEGER PRIMARY KEY, observed_at TEXT, ts INTEGER, tx_hash TEXT,
        proxy_wallet TEXT, pseudonym TEXT, condition_id TEXT, asset TEXT, outcome TEXT,
        outcome_index INTEGER, side TEXT, price TEXT, size TEXT, notional_usd TEXT,
        event_slug TEXT, slug TEXT, title TEXT, source TEXT, raw_json TEXT)""")
    old.execute("CREATE UNIQUE INDEX idx_prints_key "
                "ON prints(tx_hash, proxy_wallet, asset, side, price, size, ts)")
    old.execute("CREATE INDEX idx_prints_market ON prints(condition_id, ts)")
    old.execute("CREATE INDEX idx_prints_wallet ON prints(proxy_wallet, ts)")
    old.execute("INSERT INTO prints (observed_at, ts, tx_hash, proxy_wallet, condition_id,"
                " outcome_index, side, price, size, notional_usd) VALUES"
                " ('2026-09-01T00:00:00+00:00', 1, '0xold', '0xw', '0xc', 0, 'BUY',"
                " '0.500000', '100.000000', '50.000000')")
    old.commit()
    old.close()

    con = whale_tape.open_tape(path)

    names = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE tbl_name='prints' AND type='index'")}
    assert "idx_prints_score" in names
    plan = [tuple(r) for r in con.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM prints ORDER BY proxy_wallet,"
        " condition_id, outcome_index, ts")]
    assert any("idx_prints_score" in str(row) for row in plan), plan
    assert not any("TEMP B-TREE" in str(row) for row in plan), plan
    # the pre-existing row survives the index add untouched
    assert con.execute("SELECT COUNT(*) FROM prints").fetchone()[0] == 1


def test_open_tape_adds_idx_holdings_observed_to_a_pre_fix_tape(tmp_path):
    """Dashboard review of c83964f: dashboard_whale_bundle._probe_key runs
    `SELECT MAX(observed_at) FROM holdings` on every request. Only
    idx_holdings_wallet(proxy_wallet, observed_at) existed, so the probe had
    to read that whole covering index to find the max (observed_at is not
    its leading column) — ~9ms at 300k rows, growing. A tape opened before
    this fix has `holdings` and idx_holdings_wallet but not
    idx_holdings_observed; like idx_prints_score, a bare index needs no
    arb/whale_migrate.py entry — `CREATE INDEX IF NOT EXISTS` in SCHEMA is
    self-migrating since `open_tape` runs the whole SCHEMA script on every
    open, old tapes included."""
    path = tmp_path / "w.sqlite"
    old = sqlite3.connect(str(path))
    old.execute("""CREATE TABLE holdings (
        id INTEGER PRIMARY KEY, observed_at TEXT, proxy_wallet TEXT, condition_id TEXT,
        asset TEXT, outcome_index INTEGER, size TEXT, avg_price TEXT,
        total_bought TEXT, redeemable INTEGER, raw_json TEXT)""")
    old.execute("CREATE INDEX idx_holdings_wallet ON holdings(proxy_wallet, observed_at)")
    old.execute("INSERT INTO holdings (observed_at, proxy_wallet, condition_id,"
                " outcome_index, size, avg_price, total_bought, redeemable) VALUES"
                " ('2026-09-01T00:00:00+00:00', '0xw', '0xc', 0, '10.000000',"
                " '0.500000', '5.000000', 0)")
    old.commit()
    old.close()

    con = whale_tape.open_tape(path)

    names = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE tbl_name='holdings' AND type='index'")}
    assert "idx_holdings_observed" in names
    plan = [tuple(r) for r in con.execute(
        "EXPLAIN QUERY PLAN SELECT MAX(observed_at) FROM holdings")]
    assert any("idx_holdings_observed" in str(row) for row in plan), plan
    # the pre-existing row survives the index add untouched
    assert con.execute("SELECT COUNT(*) FROM holdings").fetchone()[0] == 1


def test_migrate_treats_a_concurrent_add_column_as_already_done(tmp_path):
    """I2(b): the real table already has `holders_ratio` (this process's own
    earlier `open_tape` added it) by the time the stale PRAGMA read above
    drives another ALTER attempt — sqlite raises `duplicate column name`, and
    `_migrate` must read that as done, not propagate it."""
    real = whale_tape.open_tape(tmp_path / "w.sqlite")
    whale_tape._migrate(_RacyCon(real))                    # must not raise


# --- universe ---------------------------------------------------------------

def test_universe_keeps_only_polymarket_sports_markets_inside_the_window():
    led = _ledger([
        _q("0xin", "t1", "rays", IN_WINDOW),
        _q("0xold", "t2", "mets", TOO_OLD),
        _q("0xfar", "t3", "cubs", TOO_FAR),
        _q("0xkalshi", "t4", "jays", IN_WINDOW, venue="kalshi"),
        _q("0xpolitics", "t5", "yes", IN_WINDOW, category="nonsports"),
        _q("0xstale", "t6", "old poll", IN_WINDOW, poll_id=1),
    ])
    got = {m.condition_id for m in whale_universe.universe(led, _cfg(), NOW)}
    assert got == {"0xin"}


def test_universe_falls_back_to_the_previous_poll_when_the_latest_has_no_pm_rows():
    """Universe resilience (2026-09-04): Polymarket's own leg of a watch poll
    can fail (intermittent CLOB TLS errors) while the poll itself still lands
    with rows from other venues. Strictly the latest poll would then hand the
    lane an empty universe for the whole cycle; it must fall back one poll."""
    led = _ledger([
        _q("0xprev", "t1", "rays", IN_WINDOW, poll_id=1),
        _q("0xlatest_kalshi", "t2", "jays", IN_WINDOW, poll_id=2, venue="kalshi"),
    ])
    got = {m.condition_id for m in whale_universe.universe(led, _cfg(), NOW)}
    assert got == {"0xprev"}


def test_universe_treats_a_stale_fallback_poll_as_no_universe(caplog):
    """Lookback bounds the poll COUNT, not its age (2026-09-04): if the watch
    lane stalls, a poll count alone can hand back a universe hours old with
    no warning. The only PM-bearing candidate here is older than
    `data_stale_alarm_secs` (900s, config.yaml), so it must be rejected —
    same as no candidate having been found at all — with a WARNING naming
    the age."""
    led = _ledger([
        _q("0xstale", "t1", "rays", IN_WINDOW, poll_id=1),
        _q("0xlatest_kalshi", "t2", "jays", IN_WINDOW, poll_id=2, venue="kalshi"),
    ], poll1_at=NOW - timedelta(hours=2))
    with caplog.at_level(logging.WARNING, logger="arb.whale"):
        got = whale_universe.universe(led, _cfg(), NOW)
    assert got == []
    assert any("data_stale_alarm_secs" in r.getMessage() and "7200" in r.getMessage()
               for r in caplog.records)


def test_universe_uses_a_fresh_fallback_poll_within_the_alarm(caplog):
    """The mirror case: a fallback poll that IS within the alarm is used
    normally — the age check does not fire on every fallback, only a stale
    one."""
    led = _ledger([
        _q("0xprev", "t1", "rays", IN_WINDOW, poll_id=1),
        _q("0xlatest_kalshi", "t2", "jays", IN_WINDOW, poll_id=2, venue="kalshi"),
    ], poll1_at=NOW - timedelta(minutes=5))
    got = {m.condition_id for m in whale_universe.universe(led, _cfg(), NOW)}
    assert got == {"0xprev"}


def test_universe_fallback_is_bounded_by_the_configured_lookback():
    """The bound in `whale.universe_poll_lookback`, not an unbounded walk of
    the whole polls table."""
    led = _ledger([_q("0xprev", "t1", "rays", IN_WINDOW, poll_id=1)])
    # poll 2 (the latest) has no rows at all; lookback=1 checks poll 2 only,
    # so poll 1's market must never be reached.
    got = whale_universe.universe(led, _cfg(universe_poll_lookback=1), NOW)
    assert got == []


def test_universe_carries_the_market_facts_and_every_token():
    led = _ledger([
        _q("0xtot", "tA", "OVER", IN_WINDOW, market_type="TOTAL", line="7.5"),
        _q("0xtot", "tB", "UNDER", IN_WINDOW, market_type="TOTAL", line="7.5"),
    ])
    (m,) = whale_universe.universe(led, _cfg(), NOW)
    assert m.condition_id == "0xtot"
    assert m.sport == "baseball_mlb" and m.market_type == "TOTAL" and m.line == "7.5"
    assert m.start_utc == IN_WINDOW and m.event_key == "ek"
    assert sorted(m.tokens) == [("tA", "OVER"), ("tB", "UNDER")]


# --- poll_prints, global mode -----------------------------------------------

def _global_markets(n=2):
    """Markets built from the real global fixture's busiest condition ids."""
    rows = _fixture("whale_trades_global")
    counts = {}
    for r in rows:
        counts[r["conditionId"]] = counts.get(r["conditionId"], 0) + 1
    top = sorted(counts, key=lambda c: -counts[c])[:n]
    return [whale_universe.Market(condition_id=c, sport="baseball_mlb", market_type="WINNER",
                              line=None, start_utc=IN_WINDOW, event_key="ek",
                              tokens=(("t1", "Yes"),)) for c in top]


def test_global_mode_makes_one_marketless_call_and_keeps_the_two_doors(tmp_path):
    """Retention amendment 2026-09-04: one marketless call as before, but the
    keep rule now has TWO doors — every row in a universe market, plus any row
    anywhere at or above `retention.global_min_print_usd`. The fixture holds 70
    rows in its busiest market and 3 more, in 2 other markets, over $1,000."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    mk = _global_markets(1)
    get = FakeGet(_fixture("whale_trades_global"))
    res = whale_tape.poll_prints(_cfg(page_size=10000), con, mk, NOW, get=get)
    assert len(get.urls) == 1 and "market=" not in get.urls[0]
    kept = con.execute("SELECT COUNT(*), COUNT(DISTINCT condition_id) FROM prints").fetchone()
    assert kept[1] == 3 and kept[0] == 73          # 70 in-universe + 3 over the floor
    assert res["rows"] == 73 and res["calls"] == 1 and res["status"] == "ok"
    row = con.execute("SELECT * FROM prints WHERE condition_id=? LIMIT 1",
                      (mk[0].condition_id,)).fetchone()
    assert row["observed_at"] == NOW.isoformat() and row["source"] == "tape"


def test_retention_scope_universe_restores_the_pre_amendment_tape(tmp_path):
    """`scope: universe` is the documented way back: the universe door alone,
    exactly the collector's behaviour before 2026-09-04."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    mk = _global_markets(1)
    cfg = _cfg(page_size=10000)
    cfg["whale"]["retention"] = {**cfg["whale"]["retention"], "scope": "universe"}
    res = whale_tape.poll_prints(cfg, con, mk, NOW, get=FakeGet(_fixture("whale_trades_global")))
    kept = con.execute("SELECT COUNT(*), COUNT(DISTINCT condition_id) FROM prints").fetchone()
    assert kept[1] == 1 and kept[0] == 70 and res["rows"] == 70


def _trade(cond, wallet, notional, ts, tx, idx=0, side="BUY"):
    """One `/trades` row, priced so price x size is exactly `notional`."""
    return {"proxyWallet": wallet, "side": side, "asset": f"tok{cond}{idx}",
            "conditionId": cond, "size": notional * 2, "price": 0.5, "timestamp": ts,
            "title": "A vs B", "slug": "mlb-a-b-2026-09-03", "eventSlug": "mlb-a-b",
            "outcome": "Yes", "outcomeIndex": idx, "pseudonym": "p",
            "transactionHash": tx}


def _two_door_page(ts):
    """$150/$500/$5,000 inside and outside the universe (`0xin` is polled)."""
    return [_trade("0xin", "0xw", 150, ts, "0x1"),
            _trade("0xin", "0xw", 5000, ts, "0x2"),
            _trade("0xout", "0xsmall", 150, ts, "0x3"),
            _trade("0xout", "0xmid", 500, ts, "0x4"),
            _trade("0xout", "0xbig", 5000, ts, "0x5")]


def _universe(cond="0xin"):
    return [whale_universe.Market(condition_id=cond, sport="baseball_mlb",
                                  market_type="WINNER", line=None, start_utc=IN_WINDOW,
                                  event_key="ek", tokens=(("t1", "Yes"),))]


def test_a_non_universe_print_is_kept_only_above_the_global_floor_and_without_raw_json(
        tmp_path):
    """The two doors (proposal §2, registered 2026-09-04). Inside a universe
    market EVERY print is kept in full — hygiene's `mm` test reads prints far
    below `min_print_usd` and would go blind without them. Outside every
    universe a print is kept only at or above the global floor, and compact:
    `raw_json` and `title` NULL, every other column as usual."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    ts = int(NOW.timestamp()) - 30

    res = whale_tape.poll_prints(_cfg(), con, _universe(), NOW,
                                 get=FakeGet(_two_door_page(ts)))

    rows = {r["tx_hash"]: r for r in con.execute("SELECT * FROM prints")}
    assert sorted(rows) == ["0x1", "0x2", "0x5"] and res["rows"] == 3
    assert rows["0x1"]["notional_usd"] == "150.000000"       # sub-floor, in universe
    assert json.loads(rows["0x1"]["raw_json"])["size"] == 300
    assert rows["0x1"]["title"] == "A vs B"
    assert rows["0x5"]["notional_usd"] == "5000.000000"      # over the floor, outside
    assert rows["0x5"]["raw_json"] is None and rows["0x5"]["title"] is None
    assert rows["0x5"]["proxy_wallet"] == "0xbig" and rows["0x5"]["side"] == "BUY"


def test_notional_is_a_six_place_decimal_string_of_price_times_size(tmp_path):
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    get = FakeGet(_fixture("whale_trades_global"))
    whale_tape.poll_prints(_cfg(), con, _global_markets(1), NOW, get=get)
    price, size, notional = con.execute(
        "SELECT price, size, notional_usd FROM prints LIMIT 1").fetchone()
    assert notional == str((Decimal(price) * Decimal(size)).quantize(Decimal("0.000001")))
    assert len(notional.split(".")[1]) == 6


def test_a_second_identical_poll_stores_nothing_new(tmp_path):
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    mk = _global_markets(1)
    body = _fixture("whale_trades_global")
    first = whale_tape.poll_prints(_cfg(), con, mk, NOW, get=FakeGet(body))
    second = whale_tape.poll_prints(_cfg(), con, mk, NOW, get=FakeGet(body))
    assert second["rows"] == 0 and second["dupes"] == first["rows"]
    assert con.execute("SELECT COUNT(*) FROM prints").fetchone()[0] == first["rows"]


def test_taker_only_comes_from_config(tmp_path):
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    get = FakeGet([])
    whale_tape.poll_prints(_cfg(taker_only=False), con, _global_markets(1), NOW, get=get)
    assert "takerOnly=false" in get.urls[0]
    get = FakeGet([])
    whale_tape.poll_prints(_cfg(taker_only=True), con, _global_markets(1), NOW, get=get)
    assert "takerOnly=true" in get.urls[0]


MAKER_CID = "0xmakerlegcond"


def _maker_market():
    return [whale_universe.Market(
        condition_id=MAKER_CID, sport="baseball_mlb", market_type="WINNER",
        line=None, start_utc=IN_WINDOW, event_key="ek",
        tokens=(("tokyes", "Rays"), ("tokno", "Yankees")))]


def test_a_maker_leg_page_keeps_every_leg_and_a_maker_sell_reduces_the_position(tmp_path):
    """L14 amendment 2026-09-04, item B. `takerOnly=false` is the shipped
    value now: the live probe in data/verified_facts.md showed maker legs
    carry the MAKER's own `proxyWallet` and `side`, so a whale exiting
    through a resting order is invisible under `takerOnly=true` and only ever
    appears as its own SELL leg here. Three legs of one fill, three wallets,
    one transactionHash: the prints unique index is (tx_hash, proxy_wallet,
    asset, side, price, size, ts), so all three survive it, and the
    maker-SELL leg reduces that wallet's held size FIFO."""
    cfg = _cfg()
    assert cfg["whale"]["taker_only"] is False          # shipped 2026-09-04
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    get = FakeGet(_fixture("whale_trades_maker_legs"))
    res = whale_tape.poll_prints(cfg, con, _maker_market(), NOW, get=get)
    assert "takerOnly=false" in get.urls[0]
    assert res["rows"] == 4 and res["bad_rows"] == 0
    assert tuple(con.execute(
        "SELECT COUNT(*), COUNT(DISTINCT proxy_wallet) FROM prints WHERE tx_hash='0xfill1'"
    ).fetchone()) == (3, 3)

    prints = [dict(r) for r in con.execute("SELECT * FROM prints")]
    markets = {MAKER_CID: {"sport": "baseball_mlb", "market_type": "WINNER",
                           "start_utc": IN_WINDOW}}
    pos = [p for p in whale_score.positions_from_prints(prints, markets, cfg)
           if p.wallet == "0xmakerb"]
    assert len(pos) == 1                                 # the SELL forms no position
    assert (pos[0].size, pos[0].sold_size) == (Decimal("1000"), Decimal("400"))
    assert pos[0].size - pos[0].sold_size == Decimal("600")


def test_a_full_page_of_fresh_rows_pages_on_by_offset(tmp_path):
    """Page full AND its oldest row newer than last_seen - overlap => page on."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    mk = _global_markets(1)
    page = _fixture("whale_trades_global")            # 200 rows, newest-first
    cfg = _cfg(page_size=len(page), overlap_secs=120)
    # last_seen far in the past: every row is "fresh", so age never stops it
    con.execute("INSERT INTO meta (key, value) VALUES ('last_seen_ts', '1')")
    get = FakeGet(page, [])                            # second page short -> stop
    res = whale_tape.poll_prints(cfg, con, mk, NOW, get=get)
    assert len(get.urls) == 2 and "offset=200" in get.urls[1]
    assert res["calls"] == 2


def test_a_short_page_stops_paging(tmp_path):
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    page = _fixture("whale_trades_global")
    cfg = _cfg(page_size=len(page) + 1)               # 200 < page_size => short
    con.execute("INSERT INTO meta (key, value) VALUES ('last_seen_ts', '1')")
    get = FakeGet(page)
    whale_tape.poll_prints(cfg, con, _global_markets(1), NOW, get=get)
    assert len(get.urls) == 1


def test_a_full_page_older_than_the_overlap_window_stops_paging(tmp_path):
    """Full page, but its oldest row has already been seen => nothing behind it."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    page = _fixture("whale_trades_global")
    oldest = min(int(r["timestamp"]) for r in page)
    cfg = _cfg(page_size=len(page), overlap_secs=120)
    con.execute("INSERT INTO meta (key, value) VALUES ('last_seen_ts', ?)",
                (str(oldest + 1000),))               # oldest <= last_seen - 120
    get = FakeGet(page)
    whale_tape.poll_prints(cfg, con, _global_markets(1), NOW, get=get)
    assert len(get.urls) == 1


def test_a_warm_poll_puts_start_on_every_page_url(tmp_path):
    """2026-09-05 cache fix: the global feed sits behind a Cloudflare edge
    cache keyed on the full URL (max-age=300s), so a warm poll's UNCHANGING
    URL was replaying the same stale, all-dupes page 4 cycles out of 5. A
    `start=<watermark floor>` on every page (offset included) makes the URL
    advance with the watermark instead."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    page = _fixture("whale_trades_global")
    cfg = _cfg(page_size=len(page), overlap_secs=120)
    con.execute("INSERT INTO meta (key, value) VALUES ('last_seen_ts', '1')")
    get = FakeGet(page, [])                             # second page short -> stop
    whale_tape.poll_prints(cfg, con, _global_markets(1), NOW, get=get)
    assert len(get.urls) == 2
    for url in get.urls:
        assert "start=-119" in url                      # floor = 1 - overlap_secs(120)
    assert "offset=200" in get.urls[1] and "start=-119" in get.urls[1]


def test_a_cold_start_poll_carries_no_start_param(tmp_path):
    """No last_seen => nothing to varying the URL around, and no server-side
    window to apply — cold start stays exactly as before this fix: one page,
    no `start=`."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    page = _fixture("whale_trades_global")
    cfg = _cfg(page_size=len(page) + 1)                 # short page -> one call
    get = FakeGet(page)
    whale_tape.poll_prints(cfg, con, _global_markets(1), NOW, get=get)
    assert len(get.urls) == 1 and "start=" not in get.urls[0]


def test_two_consecutive_warm_polls_request_different_urls(tmp_path):
    """The watermark advances every completed poll, so back-to-back cycles
    against the SAME URL (the cache-poisoning failure mode) become back-to-back
    cycles against two different URLs, one per completed poll."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    page = _fixture("whale_trades_global")
    cfg = _cfg(page_size=len(page) + 1, overlap_secs=120)
    con.execute("INSERT INTO meta (key, value) VALUES ('last_seen_ts', '1')")
    get1 = FakeGet(page)
    whale_tape.poll_prints(cfg, con, _global_markets(1), NOW, get=get1)
    newest = whale_tape._meta(con, "last_seen_ts")
    assert newest != "1"                                 # watermark advanced
    get2 = FakeGet(page)
    whale_tape.poll_prints(cfg, con, _global_markets(1), NOW, get=get2)
    assert get1.urls[0] != get2.urls[0]
    assert f"start={int(newest) - 120}" in get2.urls[0]


def _pages(n, per_page, cond):
    """`n` full pages of distinct rows on one condition id, newest-first across
    pages (the global feed's own order), 10 s apart."""
    base = _fixture("whale_trades_global")[0]
    ts = int(NOW.timestamp())
    pages = []
    for p in range(n):
        page = []
        for i in range(per_page):
            page.append(dict(base, conditionId=cond, timestamp=ts,
                             transactionHash=f"0x{p}-{i}"))
            ts -= 10
        pages.append(page)
    return pages


def test_paging_stops_at_max_offset_and_records_a_gap(tmp_path):
    """The feed 400s past `offset=max_offset` (observed live 2026-09-04), so a
    watermark far enough behind used to page into a 400 every cycle forever.
    The cap stops one page short, records the unfetched span, and advances the
    watermark past it — a recorded gap, never a wedged collector."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    mk = _global_markets(1)
    pages = _pages(4, 5, mk[0].condition_id)
    cfg = _cfg(page_size=5, max_offset=5, overlap_secs=120)
    last_seen = int(NOW.timestamp()) - 100_000          # every row is "fresh"
    con.execute("INSERT INTO meta (key, value) VALUES ('last_seen_ts', ?)",
                (str(last_seen),))
    con.commit()
    # the fifth response is the live venue's own answer past the cap
    get = FakeGet(*pages, VenueError("400 from https://data-api.polymarket.com/trades"))
    res = whale_tape.poll_prints(cfg, con, mk, NOW, get=get)

    assert len(get.urls) == 2 and "offset=5" in get.urls[1]
    assert not any("offset=10" in u for u in get.urls)   # page 3 never requested
    assert res["status"] == "ok" and whale_tape._meta(con, "backoff_until") is None
    oldest = min(int(r["timestamp"]) for r in pages[1])
    assert res["gap_secs"] == oldest - last_seen > 0
    gap = json.loads(whale_tape._meta(con, "last_gap"))
    assert gap == {"from_ts": last_seen, "to_ts": oldest,
                   "secs": oldest - last_seen, "at": NOW.isoformat()}
    # the watermark is the NEWEST row fetched, exactly as on every other cycle:
    # everything above the gap is on the tape and the span below it is recorded
    # once, so the next cycle starts from the present instead of re-walking it
    newest = max(int(r["timestamp"]) for r in pages[0])
    assert whale_tape._meta(con, "last_seen_ts") == str(newest)
    assert con.execute("SELECT COUNT(*) FROM prints").fetchone()[0] == 10


class _Feed:
    """A continuous global feed: one print every `spacing` seconds on a FIXED
    grid, newest first, `size` rows per call from `offset`. The same wall-clock
    row comes back on every cycle, so consecutive polls overlap the way the
    venue's own feed does — which is what makes a repeated cap visible."""

    def __init__(self, cond, spacing, size):
        self.cond, self.spacing, self.size = cond, spacing, size
        self.now = NOW
        self.urls = []

    def top(self):
        return int(self.now.timestamp()) // self.spacing * self.spacing

    def __call__(self, url, cfg, **kw):
        self.urls.append(url)
        base = _fixture("whale_trades_global")[0]
        off = int(url.split("offset=")[1]) if "offset=" in url else 0
        return [dict(base, conditionId=self.cond, timestamp=t, transactionHash=f"0x{t}")
                for t in (self.top() - (off + i) * self.spacing
                          for i in range(self.size))]


def test_a_capped_cycle_records_one_gap_then_catches_up(tmp_path, caplog):
    """Spec §5 step 2 (re-amended 2026-09-04): after a cap the watermark
    advances to the NEWEST row fetched, like every other cycle, and the
    unreachable span is recorded ONCE. Advancing to the oldest row instead left
    the next floor two pages behind a `now` that kept moving, so the cap fired
    again every cycle and rewrote `meta.last_gap` with a spurious ~60 s gap."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    mk = _global_markets(1)
    feed = _Feed(mk[0].condition_id, spacing=30, size=5)
    cfg = _cfg(page_size=5, max_offset=5, overlap_secs=120)
    last_seen = int(NOW.timestamp()) - 100_000        # a long outage behind us
    con.execute("INSERT INTO meta (key, value) VALUES ('last_seen_ts', ?)",
                (str(last_seen),))
    con.commit()

    gaps, warned = [], []
    with caplog.at_level(logging.WARNING, logger="arb.whale"):
        for cycle in range(5):
            feed.now = NOW + timedelta(seconds=60 * cycle)
            caplog.clear()
            res = whale_tape.poll_prints(cfg, con, mk, feed.now, get=feed)
            gaps.append(res["gap_secs"])
            warned.append([r.getMessage() for r in caplog.records])
            assert res["status"] == "ok"
            # the watermark is the newest row fetched, every cycle
            assert whale_tape._meta(con, "last_seen_ts") == str(feed.top())

    # exactly one gap: the first cycle's, spanning last_seen -> its oldest row
    oldest = int(NOW.timestamp()) - 9 * 30        # 2 pages x 5 rows, 30 s apart
    assert gaps[0] == oldest - last_seen > 0
    assert gaps[1:] == [0, 0, 0, 0]
    assert len(warned[0]) == 1 and "capped at offset" in warned[0][0]
    assert warned[1:] == [[], [], [], []]
    gap = json.loads(whale_tape._meta(con, "last_gap"))
    assert gap["from_ts"] == last_seen and gap["secs"] == gaps[0]
    assert gap["at"] == NOW.isoformat()           # cycle 1's clock, never rewritten


def test_a_failed_call_records_a_backoff_and_the_next_poll_short_circuits(tmp_path):
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    mk = _global_markets(1)
    boom = FakeGet(VenueError("503 from https://data-api.polymarket.com/trades"))
    res = whale_tape.poll_prints(_cfg(), con, mk, NOW, get=boom)
    assert res["status"] == "error" and res["status_counts"]["error"] == 1
    until = datetime.fromisoformat(
        con.execute("SELECT value FROM meta WHERE key='backoff_until'").fetchone()[0])
    # the pause is stamped from the WALL clock, never earlier than the cycle's
    # own `now` — so a fixed test clock still gets exactly backoff_secs
    assert until >= NOW + timedelta(seconds=300)
    quiet = FakeGet([])
    res2 = whale_tape.poll_prints(_cfg(), con, mk, NOW + timedelta(seconds=60), get=quiet)
    assert res2["status"] == "backoff" and quiet.urls == []


def test_an_abort_mid_page_does_not_advance_the_watermark(tmp_path):
    """Page 1 lands, page 2 raises: last_seen_ts must NOT jump to page 1's
    newest, or the unfetched gap behind it is never revisited."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    page = _fixture("whale_trades_global")
    con.execute("INSERT INTO meta (key, value) VALUES ('last_seen_ts', '1')")
    con.commit()
    get = FakeGet(page, VenueError("503 from https://data-api.polymarket.com/trades"))
    cfg = _cfg(page_size=len(page), overlap_secs=120)
    res = whale_tape.poll_prints(cfg, con, _global_markets(1), NOW, get=get)
    assert res["status"] == "error" and len(get.urls) == 2
    assert whale_tape._meta(con, "last_seen_ts") == "1"
    assert whale_tape._meta(con, "backoff_until") is not None


def test_one_malformed_row_is_skipped_not_the_whole_page(tmp_path):
    """A bad row is the venue's data, not a venue outage: keep the rest of the
    page, count it, and do NOT write a backoff."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    page = copy.deepcopy(_fixture("whale_trades_global"))
    mk = _global_markets(1)
    ours = [r for r in page if r["conditionId"] == mk[0].condition_id]
    del ours[0]["timestamp"]
    ours[1]["price"] = "abc"
    res = whale_tape.poll_prints(_cfg(page_size=10000), con, mk, NOW, get=FakeGet(page))
    assert res["status"] == "ok" and res["bad_rows"] == 2
    # + the 3 rows over retention.global_min_print_usd in the fixture's OTHER markets
    assert res["rows"] == len(ours) - 2 + 3
    assert con.execute("SELECT COUNT(*) FROM prints").fetchone()[0] == len(ours) - 2 + 3
    assert whale_tape._meta(con, "backoff_until") is None


def test_a_full_page_of_unreadable_rows_is_an_error_not_a_quiet_stop(tmp_path):
    """Every row of a full page fails to parse: this must NOT read as "caught
    up" (status ok, watermark advanced, no backoff) — that's indistinguishable
    from the venue silently sending garbage forever. It must abort like any
    other failed call."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    mk = _global_markets(1)
    good = _fixture("whale_trades_global")
    bad = copy.deepcopy(good)
    for r in bad:
        del r["timestamp"]
    cfg = _cfg(page_size=len(good), overlap_secs=120)
    con.execute("INSERT INTO meta (key, value) VALUES ('last_seen_ts', '1')")
    con.commit()
    get = FakeGet(good, bad, good)          # third page must never be reached
    res = whale_tape.poll_prints(cfg, con, mk, NOW, get=get)
    assert res["status"] == "error"
    assert len(get.urls) == 2
    assert res["bad_rows"] == len(bad)
    assert whale_tape._meta(con, "last_seen_ts") == "1"
    assert whale_tape._meta(con, "backoff_until") is not None


def test_batched_mode_sends_comma_csv_markets_in_groups(tmp_path):
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    rows = _fixture("whale_trades_batch")
    ids = sorted({r["conditionId"] for r in rows})
    mk = [whale_universe.Market(condition_id=c, sport="baseball_kbo", market_type="WINNER",
                            line=None, start_utc=IN_WINDOW, event_key="ek",
                            tokens=(("t", "o"),)) for c in ids]
    get = FakeGet(rows, [])
    cfg = _cfg(collector_mode="batched", markets_per_call=2)
    res = whale_tape.poll_prints(cfg, con, mk, NOW, get=get)
    assert len(get.urls) == 2                          # 3 markets, 2 per call
    assert f"market={ids[0]},{ids[1]}" in get.urls[0]
    assert f"market={ids[2]}" in get.urls[1]
    assert res["rows"] == len(rows)


# --- leaderboard ------------------------------------------------------------

def test_leaderboard_snapshot_stores_rows_and_seeds_wallets(tmp_path):
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    board = _fixture("whale_leaderboard")
    get = FakeGet(board)
    cfg = _cfg(leaderboard={"enabled": True, "category": "SPORTS",
                            "time_periods": ["MONTH"], "order_by": "PNL", "pages": 1})
    n = whale_backfill.snapshot_leaderboard(cfg, con, NOW, get=get)
    assert "category=SPORTS" in get.urls[0] and "timePeriod=MONTH" in get.urls[0]
    assert "orderBy=PNL" in get.urls[0]
    assert con.execute("SELECT COUNT(*) FROM leaderboard_snapshots").fetchone()[0] == len(board)
    srcs = {r[0] for r in con.execute("SELECT DISTINCT source FROM wallets")}
    assert srcs == {"leaderboard"} and n == len(board)


def test_leaderboard_runs_at_most_once_per_utc_day(tmp_path):
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    cfg = _cfg(leaderboard={"enabled": True, "category": "SPORTS",
                            "time_periods": ["MONTH"], "order_by": "PNL", "pages": 1})
    whale_backfill.snapshot_leaderboard(cfg, con, NOW, get=FakeGet(_fixture("whale_leaderboard")))
    again = FakeGet(_fixture("whale_leaderboard"))
    assert whale_backfill.snapshot_leaderboard(cfg, con, NOW + timedelta(hours=6), get=again) == 0
    assert again.urls == []


def test_the_leaderboard_snapshot_covers_every_configured_window(tmp_path):
    """Amended 2026-09-04 (L14 amendment): discovery reads DAY/WEEK/MONTH/ALL,
    not MONTH alone, and the four windows of one run share one `snapshot_at` —
    which is what makes "the latest snapshot, any window" a single set."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    board = _fixture("whale_leaderboard")
    get = FakeGet(board)
    cfg = _cfg(leaderboard={"enabled": True, "category": "SPORTS",
                            "time_periods": ["DAY", "WEEK", "MONTH", "ALL"],
                            "order_by": "PNL", "pages": 1})

    whale_backfill.snapshot_leaderboard(cfg, con, NOW, get=get)

    assert [u.split("timePeriod=")[1].split("&")[0] for u in get.urls] \
        == ["DAY", "WEEK", "MONTH", "ALL"]
    assert dict(con.execute("SELECT time_period, COUNT(*) FROM leaderboard_snapshots"
                            " GROUP BY time_period")) \
        == {p: len(board) for p in ("DAY", "WEEK", "MONTH", "ALL")}
    assert con.execute("SELECT COUNT(DISTINCT snapshot_at) "
                       "FROM leaderboard_snapshots").fetchone()[0] == 1


def test_a_failure_mid_run_cannot_duplicate_the_windows_that_landed(tmp_path):
    """The day is claimed PER WINDOW, right after that window's page 0 lands:
    DAY is stored, WEEK's first page fails, and a same-day retry re-fetches
    only the windows that never landed — DAY's ranks are never written twice."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    board = _fixture("whale_leaderboard")
    cfg = _cfg(leaderboard={"enabled": True, "category": "SPORTS",
                            "time_periods": ["DAY", "WEEK", "MONTH", "ALL"],
                            "order_by": "PNL", "pages": 1})
    get = FakeGet(board, VenueError("503 from https://data-api.polymarket.com/v1/leaderboard"))

    whale_backfill.snapshot_leaderboard(cfg, con, NOW, get=get)

    assert len(get.urls) == 2                        # DAY kept, WEEK lost, then stop
    assert whale_tape._meta(con, "leaderboard_day:DAY") == NOW.strftime("%Y-%m-%d")
    assert whale_tape._meta(con, "leaderboard_day:WEEK") is None
    con.execute("DELETE FROM meta WHERE key='backoff_until'")   # isolate the day guard
    con.commit()
    retry = FakeGet(board)

    whale_backfill.snapshot_leaderboard(cfg, con, NOW + timedelta(minutes=5), get=retry)

    assert [u.split("timePeriod=")[1].split("&")[0] for u in retry.urls] \
        == ["WEEK", "MONTH", "ALL"]
    assert dict(con.execute("SELECT time_period, COUNT(*) FROM leaderboard_snapshots"
                            " GROUP BY time_period")) \
        == {p: len(board) for p in ("DAY", "WEEK", "MONTH", "ALL")}


def test_a_leaderboard_failure_mid_snapshot_cannot_duplicate_ranks(tmp_path):
    """Page 1 stored, page 2 fails: the day is already claimed, so a retry
    cannot write a second copy of ranks 1..50 under the same snapshot."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    board = _fixture("whale_leaderboard")
    cfg = _cfg(leaderboard={"enabled": True, "category": "SPORTS",
                            "time_periods": ["MONTH"], "order_by": "PNL", "pages": 4})
    get = FakeGet(board, VenueError("503 from https://data-api.polymarket.com/v1/leaderboard"))
    whale_backfill.snapshot_leaderboard(cfg, con, NOW, get=get)
    stored = con.execute("SELECT COUNT(*) FROM leaderboard_snapshots").fetchone()[0]
    assert stored == len(board) and len(get.urls) == 2      # page 1 kept, page 2 lost
    con.execute("DELETE FROM meta WHERE key='backoff_until'")   # isolate the day guard
    con.commit()
    retry = FakeGet(board)
    assert whale_backfill.snapshot_leaderboard(cfg, con, NOW, get=retry) == 0
    assert retry.urls == []
    assert con.execute("SELECT COUNT(*) FROM leaderboard_snapshots").fetchone()[0] == stored


def test_a_failure_on_the_first_page_does_not_burn_the_day(tmp_path):
    """Page 0 itself fails: nothing was stored, so the day must NOT be
    claimed — a same-day retry needs to actually run, not short-circuit."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    cfg = _cfg(leaderboard={"enabled": True, "category": "SPORTS",
                            "time_periods": ["MONTH"], "order_by": "PNL", "pages": 4})
    boom = FakeGet(VenueError("503 from https://data-api.polymarket.com/v1/leaderboard"))
    assert whale_backfill.snapshot_leaderboard(cfg, con, NOW, get=boom) == 0
    assert whale_tape._meta(con, "leaderboard_day:MONTH") is None
    con.execute("DELETE FROM meta WHERE key='backoff_until'")
    con.commit()
    retry = FakeGet(_fixture("whale_leaderboard"))
    n = whale_backfill.snapshot_leaderboard(cfg, con, NOW + timedelta(minutes=5), get=retry)
    assert retry.urls and n == len(_fixture("whale_leaderboard"))


def test_the_rate_bucket_is_one_per_process_not_one_per_call_site(tmp_path):
    """Three call sites, one budget: otherwise a single command bursts 3x the
    configured cap."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    cfg = _cfg(leaderboard={"enabled": True, "category": "SPORTS",
                            "time_periods": ["MONTH"], "order_by": "PNL", "pages": 1})
    whale_backfill.snapshot_leaderboard(cfg, con, NOW, get=FakeGet(_fixture("whale_leaderboard")))
    after_leaderboard = whale_tape._BUCKET.tokens
    whale_tape.poll_prints(cfg, con, _global_markets(1), NOW, get=FakeGet([]))
    assert whale_tape._BUCKET.tokens < after_leaderboard
    assert after_leaderboard <= float(cfg["whale"]["max_calls_per_min"]) - 1


# --- discovery --------------------------------------------------------------

def _print(con, wallet, cond, side, notional, ts, source="tape"):
    con.execute(
        "INSERT INTO prints (observed_at, ts, tx_hash, proxy_wallet, pseudonym, condition_id,"
        " asset, outcome, outcome_index, side, price, size, notional_usd, event_slug, slug,"
        " title, source, raw_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (NOW.isoformat(), ts, f"0x{wallet}{ts}{notional}", wallet, "p", cond, "a", "o", 0,
         side, "0.500000", "1.000000", f"{Decimal(notional):.6f}", "", "", "", source, "{}"))


def _market(con, cond, start):
    con.execute("INSERT INTO markets (condition_id, sport, market_type, line, start_utc,"
                " event_key, tokens_json, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?)",
                (cond, "baseball_mlb", "WINNER", None, start, "ek", "[]",
                 NOW.isoformat(), NOW.isoformat()))


def test_discovery_counts_pregame_buys_only_and_honours_the_threshold(tmp_path):
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    start = int((NOW + timedelta(hours=2)).timestamp())
    _market(con, "0xm", (NOW + timedelta(hours=2)).isoformat())
    _print(con, "0xbig", "0xm", "BUY", "900", start - 60)
    _print(con, "0xbig", "0xm", "BUY", "200", start - 30)      # 1100 >= 1000
    _print(con, "0xsmall", "0xm", "BUY", "999", start - 60)    # under
    _print(con, "0xseller", "0xm", "SELL", "5000", start - 60)  # not a BUY
    _print(con, "0xlate", "0xm", "BUY", "5000", start + 60)    # in-play, not pregame
    con.commit()
    assert whale_backfill.discover_wallets(_cfg(), con, NOW) == 1
    assert [r[0] for r in con.execute("SELECT proxy_wallet FROM wallets")] == ["0xbig"]
    assert con.execute("SELECT source FROM wallets").fetchone()[0] == "tape"


def test_discovery_admits_a_wallet_that_printed_outside_the_polled_universe(tmp_path):
    """Retention amendment 2026-09-04: the discovery door keeps its $1,000
    value and its mapped-root requirement, but is now evaluated over the whole
    platform rather than over the markets this cycle happened to poll — the
    $5,000 print in `0xelsewhere` is on the tape now, so its wallet is a
    candidate whale. A market the mapper could give no root (`sport IS NULL`)
    still counts for nothing, which is what stops churn in the unfollowable
    5-minute markets from manufacturing whales."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    start = (NOW + timedelta(hours=2)).isoformat()
    _market(con, "0xelsewhere", start)                       # mapped, never polled
    con.execute("INSERT INTO markets (condition_id, sport, market_type, start_utc,"
                " tokens_json, first_seen, last_seen) VALUES ('0xnoroot', NULL,"
                " 'WINNER', ?, '[]', ?, ?)", (start, NOW.isoformat(), NOW.isoformat()))
    con.commit()
    ts = int(NOW.timestamp()) - 30
    page = [_trade("0xelsewhere", "0xbig", 5000, ts, "0xa"),
            _trade("0xnoroot", "0xchurn", 5000, ts, "0xb")]

    whale_tape.poll_prints(_cfg(), con, _universe(), NOW, get=FakeGet(page))

    assert con.execute("SELECT COUNT(*) FROM prints").fetchone()[0] == 2
    assert whale_backfill.discover_wallets(_cfg(), con, NOW) == 1
    assert [r[0] for r in con.execute("SELECT proxy_wallet FROM wallets")] == ["0xbig"]


def test_mm_detection_still_sees_sub_floor_prints_in_universe_markets(tmp_path):
    """Why the universe door keeps FULL fidelity (proposal §2): `mm` is "both
    outcomes bought inside mm_window_secs on >=30% of the wallet's markets with
    mean print notional < $50". A single $1,000 floor would delete exactly the
    rows that define a market maker and make every one of them invisible."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    ts = int(NOW.timestamp()) - 30
    page = [_trade("0xin", "0xmaker", 10, ts, "0xm1", idx=0),
            _trade("0xin", "0xmaker", 10, ts + 1, "0xm2", idx=1)]

    whale_tape.poll_prints(_cfg(), con, _universe(), NOW, get=FakeGet(page))

    prints = [dict(r) for r in con.execute("SELECT * FROM prints")]
    assert len(prints) == 2                              # both far below the floor
    assert whale_hygiene.hygiene_flags(prints, [], _cfg()).wallets["0xmaker"] == {"mm"}


# --- backfill ---------------------------------------------------------------

def _wallet(con, w, first_seen, source="tape"):
    """A wallet already in `wallets`. `source` matters to the backfill queue
    since the 2026-09-04 retention amendment: a `tape` wallet was discovered by
    printing in a market with a mapped root, so it has one, while a
    `leaderboard` seed has no prints at all until backfill fetches them."""
    con.execute("INSERT INTO wallets (proxy_wallet, pseudonym, first_seen, source) "
                "VALUES (?,?,?,?)", (w, "p", first_seen, source))


def _map_row(con, cond, sport=None, market_type=None, start_utc=None, slug=None,
            first_seen=None):
    """A pre-existing `markets` row, as the mapper-fix REVISIT_SQL finds one:
    written by an earlier cycle (a prefix remap, a partial Gamma answer),
    possibly still missing a field. Distinct from `_market` above (which
    always writes a fully-mapped row) — this one's whole point is to leave
    fields NULL."""
    at = first_seen or NOW.isoformat()
    con.execute(
        "INSERT INTO markets (condition_id, sport, market_type, line, start_utc,"
        " event_key, tokens_json, slug, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (cond, sport, market_type, None, start_utc, None, "[]", slug, at, at))


def test_backfill_pulls_activity_pages_until_a_short_page(tmp_path):
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    _wallet(con, "0xw", "2026-09-01T00:00:00+00:00", source="leaderboard")
    con.commit()
    acts = _fixture("whale_activity")
    # the Gamma ladder is ~36 calls: pace them off the clock, not at 30/min
    cfg = _cfg(max_calls_per_min=100000,
               backfill={"wallets_per_cycle": 5, "pages_per_wallet": 40, "page_size": len(acts)})
    get = FakeGet(acts, [], [])                    # full page, then a short one
    res = whale_backfill.backfill(cfg, con, NOW, get=get)
    trade_urls = [u for u in get.urls if "/activity" in u]
    assert len(trade_urls) == 2 and "offset=72" in trade_urls[1]
    assert "type=TRADE" in trade_urls[0] and "user=0xw" in trade_urls[0]
    stored = con.execute("SELECT COUNT(*), COUNT(DISTINCT source) FROM prints").fetchone()
    assert stored[0] == len(acts) and res["rows"] == len(acts)
    assert con.execute("SELECT DISTINCT source FROM prints").fetchone()[0] == "activity"
    assert con.execute("SELECT backfilled_at FROM wallets").fetchone()[0] == NOW.isoformat()


def test_the_backfill_queue_is_fifo_within_relevant_wallets_only(tmp_path):
    """Retention amendment 2026-09-04: with discovery reading the whole
    platform, the wallet queue must not fill with wallets whose whole history
    is in markets no root will ever score — at `wallets_per_cycle` a cycle it
    would never drain, and the roster would silently become "whoever was
    discovered first". A wallet SEEDED by another source (the leaderboard) has
    no prints yet BECAUSE backfill is what fetches them, so it is never judged
    by this filter — that would be a filter on edge, which this is not."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    _market(con, "0xmapped", (NOW + timedelta(hours=2)).isoformat())     # has a root
    _map_row(con, "0xnoroot", sport=None, market_type="WINNER",
             start_utc=(NOW + timedelta(hours=2)).isoformat())
    _wallet(con, "0xchurn", "2026-09-01T00:00:00+00:00")      # source='tape'
    _wallet(con, "0xrelevant", "2026-09-02T00:00:00+00:00")   # source='tape'
    con.execute("INSERT INTO wallets (proxy_wallet, pseudonym, first_seen, source) "
                "VALUES ('0xseed', 'p', '2026-09-03T00:00:00+00:00', 'leaderboard')")
    ts = int(NOW.timestamp()) - 60
    _print(con, "0xchurn", "0xnoroot", "BUY", "5000", ts)
    _print(con, "0xrelevant", "0xmapped", "BUY", "5000", ts)
    con.commit()
    cfg = _cfg(backfill={"wallets_per_cycle": 3, "pages_per_wallet": 1, "page_size": 50})

    res = whale_backfill.backfill(cfg, con, NOW, get=FakeGet([]))

    assert res["wallets"] == 2
    assert [r[0] for r in con.execute(
        "SELECT proxy_wallet FROM wallets WHERE backfilled_at IS NOT NULL "
        "ORDER BY first_seen")] == ["0xrelevant", "0xseed"]


def test_backfill_is_fifo_by_first_seen_and_respects_wallets_per_cycle(tmp_path):
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    _wallet(con, "0xthird", "2026-09-03T00:00:00+00:00", source="leaderboard")
    _wallet(con, "0xfirst", "2026-09-01T00:00:00+00:00", source="leaderboard")
    _wallet(con, "0xsecond", "2026-09-02T00:00:00+00:00", source="leaderboard")
    con.commit()
    cfg = _cfg(backfill={"wallets_per_cycle": 2, "pages_per_wallet": 1, "page_size": 50})
    get = FakeGet([])
    res = whale_backfill.backfill(cfg, con, NOW, get=get)
    assert res["wallets"] == 2
    done = [r[0] for r in con.execute(
        "SELECT proxy_wallet FROM wallets WHERE backfilled_at IS NOT NULL ORDER BY first_seen")]
    assert done == ["0xfirst", "0xsecond"]


def test_backfill_writes_prints_only_and_map_markets_maps_them_through_gamma(tmp_path):
    """Whale-backfill fix pass (2026-09-04): mapping is its own step now —
    `backfill()` only pages wallet activity into `prints`; `map_markets()`
    reads the pending queue straight off `prints` (MAP_SQL) and maps through
    Gamma, bounded by `map_per_cycle`. Folding mapping into `backfill()` used
    to mean walking every condition_id THIS cycle's wallets had ever touched
    in one unbounded pass, which is what blew the child's 900s cap."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    # `/activity?user=W` returns W's OWN trades, so every print this writes
    # carries W in proxy_wallet — which is what MAP_SQL's relevance guard reads.
    wallet = _fixture("whale_activity")[0]["proxyWallet"]
    _wallet(con, wallet, "2026-09-01T00:00:00+00:00", source="leaderboard")
    con.commit()
    gamma = _fixture("whale_gamma_condition_ids_repeated")
    known, other = gamma[0]["conditionId"], gamma[1]["conditionId"]
    acts = [dict(r, conditionId=c) for r, c in
            zip(_fixture("whale_activity")[:2], (known, "0xnowhere"))]
    cfg = _cfg(backfill={"wallets_per_cycle": 1, "pages_per_wallet": 1, "page_size": 50,
                         "map_per_cycle": 40})
    whale_backfill.backfill(cfg, con, NOW, get=FakeGet(acts))
    assert con.execute("SELECT COUNT(*) FROM markets").fetchone()[0] == 0  # not mapped yet
    assert con.execute("SELECT COUNT(DISTINCT proxy_wallet) FROM prints").fetchone()[0] == 1

    get = FakeGet([gamma[0]])                       # gamma answers for `known` only
    whale_backfill.map_markets(cfg, con, NOW, get=get)

    g = [u for u in get.urls if "gamma" in u]
    # F0 (mapper fix pass 2): "0xnowhere" isn't in the default pass's answer,
    # so it gets a second, closed=true call before it counts as unmappable.
    assert len(g) == 2
    assert g[0].count("condition_ids=") == 2 and "closed=true" not in g[0]
    assert g[1].count("condition_ids=") == 1 and "closed=true" in g[1]
    assert "condition_ids=0xnowhere" in g[1]
    rows = {r["condition_id"]: r for r in con.execute("SELECT * FROM markets")}
    assert rows[known]["sport"] == "baseball_kbo"              # kbo- slug prefix
    assert rows[known]["market_type"] == "WINNER"              # sportsMarketType=moneyline
    assert rows[known]["start_utc"] == "2026-08-09T09:00:00+00:00"
    assert json.loads(rows[known]["tokens_json"])[0][1] == "Lotte Giants"
    assert rows[known]["slug"] == "kbo-lot-kt-2026-08-09"      # kept for a LOCAL re-map
    assert rows["0xnowhere"]["sport"] is None                  # unmappable => never scored
    assert other not in rows


def test_backfill_stores_rows_compact_by_default_raw_json_null_title_kept(tmp_path):
    """2026-09-05 retention: the cold-start /activity backfill was storing
    every historical print at full fidelity — ~1.3 GB/day over ~2,700
    remaining wallets, most of it `raw_json`, which nothing reads (scoring:
    ts/price/size/side/outcome_index/condition_id/asset; hygiene: price/size/
    side/ts). `whale.retention.backfill_compact` defaults true: raw_json NULL,
    every other column — title included, an observed field rather than a
    convenience copy (rule 7) — kept exactly as fetched."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    _wallet(con, "0xw", "2026-09-01T00:00:00+00:00", source="leaderboard")
    con.commit()
    acts = _fixture("whale_activity")
    cfg = _cfg(backfill={"wallets_per_cycle": 1, "pages_per_wallet": 1, "page_size": len(acts)})

    res = whale_backfill.backfill(cfg, con, NOW, get=FakeGet(acts))

    rows = con.execute("SELECT title, raw_json, price, size, side, ts, outcome_index, "
                       "condition_id, asset FROM prints").fetchall()
    assert res["rows"] == len(acts) == len(rows) > 0
    assert all(r["raw_json"] is None for r in rows)
    assert all(r["title"] is not None for r in rows)
    assert all(r["price"] is not None and r["side"] is not None
              and r["outcome_index"] is not None for r in rows)


def test_backfill_compact_false_restores_full_fidelity_rows(tmp_path):
    """The escape hatch: an operator who wants the pre-2026-09-05 behaviour
    back sets `backfill_compact: false` — no code change needed."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    _wallet(con, "0xw", "2026-09-01T00:00:00+00:00", source="leaderboard")
    con.commit()
    acts = _fixture("whale_activity")
    cfg = _cfg(backfill={"wallets_per_cycle": 1, "pages_per_wallet": 1, "page_size": len(acts)})
    cfg["whale"]["retention"] = {**cfg["whale"]["retention"], "backfill_compact": False}

    whale_backfill.backfill(cfg, con, NOW, get=FakeGet(acts))

    rows = con.execute("SELECT raw_json FROM prints").fetchall()
    assert len(rows) > 0 and all(r["raw_json"] is not None for r in rows)


def test_backfill_compact_rows_score_and_hygiene_identically_to_full_rows(tmp_path):
    """Equivalence: compacting must not change what the lane computes off a
    backfilled print. Run the same fixture through backfill twice, once
    compact and once full, and assert positions_from_prints/hygiene_flags
    agree exactly on the two tapes."""
    acts = _fixture("whale_activity")
    wallet = acts[0]["proxyWallet"]

    def _tape(path, compact):
        con = whale_tape.open_tape(path)
        _wallet(con, wallet, "2026-09-01T00:00:00+00:00", source="leaderboard")
        con.commit()
        cfg = _cfg(backfill={"wallets_per_cycle": 1, "pages_per_wallet": 1,
                             "page_size": len(acts)})
        cfg["whale"]["retention"] = {**cfg["whale"]["retention"], "backfill_compact": compact}
        whale_backfill.backfill(cfg, con, NOW, get=FakeGet(acts))
        cfg["whale"]["min_print_usd"] = 0   # the fixture's own notionals run $0.01-$50
        return con, cfg

    con_c, cfg_c = _tape(tmp_path / "compact.sqlite", True)
    con_f, cfg_f = _tape(tmp_path / "full.sqlite", False)
    rc = [dict(r) for r in con_c.execute("SELECT * FROM prints ORDER BY id")]
    rf = [dict(r) for r in con_f.execute("SELECT * FROM prints ORDER BY id")]
    assert len(rc) == len(rf) == len(acts) > 0
    assert all(r["raw_json"] is None for r in rc)
    assert all(r["raw_json"] is not None for r in rf)

    markets = {cid: {"sport": "baseball_mlb", "market_type": "WINNER", "start_utc": None}
              for cid in {r["condition_id"] for r in rc}}
    pos_c = whale_score.positions_from_prints(rc, markets, cfg_c)
    pos_f = whale_score.positions_from_prints(rf, markets, cfg_f)
    assert len(pos_c) > 0
    assert pos_c == pos_f

    hyg_c = whale_hygiene.hygiene_flags(rc, pos_c, cfg_c)
    hyg_f = whale_hygiene.hygiene_flags(rf, pos_f, cfg_f)
    assert hyg_c == hyg_f


def test_whale_compact_cli_nulls_backfill_raw_json_on_a_fixture_tape(tmp_path, capsys):
    """The one-shot `arb whale-compact --backfill`: NULLs raw_json on existing
    `source='activity'` rows in bounded batches, touches nothing else — a
    `source='tape'` (live collector) row keeps its raw_json even when it is a
    universe row that already carries one — and reports rows touched and
    bytes freed. Never VACUUMs (that's the operator's own, offline, separate
    call)."""
    import re
    from types import SimpleNamespace
    from arb.commands import cmd_whale_compact
    from arb.store import Store
    path = tmp_path / "w.sqlite"
    con = whale_tape.open_tape(path)
    _wallet(con, "0xw", "2026-09-01T00:00:00+00:00", source="leaderboard")
    con.commit()
    acts = _fixture("whale_activity")
    cfg = _cfg(backfill={"wallets_per_cycle": 1, "pages_per_wallet": 1, "page_size": len(acts)})
    cfg["whale"]["retention"] = {**cfg["whale"]["retention"], "backfill_compact": False}
    whale_backfill.backfill(cfg, con, NOW, get=FakeGet(acts))       # full-fidelity backfill rows
    whale_tape.poll_prints(cfg, con, _universe(), NOW,
                           get=FakeGet(_two_door_page(int(NOW.timestamp()) - 30)))
    before = {r["tx_hash"]: dict(r) for r in con.execute("SELECT * FROM prints")}
    activity = {h for h, r in before.items() if r["source"] == "activity"}
    tape = {h for h, r in before.items() if r["source"] == "tape"}
    assert len(activity) == len(acts) and "0x1" in tape
    assert all(before[h]["raw_json"] is not None for h in activity)   # pre-compact: full
    assert before["0x1"]["raw_json"] is not None                      # universe row: full
    con.close()

    cfg["whale"]["tape_db"] = str(path)
    rc = cmd_whale_compact(cfg, Store(tmp_path / "arb.sqlite"), SimpleNamespace(backfill=True))
    out = capsys.readouterr().out

    assert rc == 0
    m = re.search(r"(\d+) row\(s\) compacted, (\d+) byte\(s\)", out)
    assert m and int(m.group(1)) == len(activity) and int(m.group(2)) > 0   # count reported
    con = whale_tape.open_tape(path)
    after = {r["tx_hash"]: dict(r) for r in con.execute("SELECT * FROM prints")}
    assert all(after[h]["raw_json"] is None for h in activity)
    assert all(after[h]["raw_json"] == before[h]["raw_json"] for h in tape)  # untouched
    for h in activity:                          # every other column is byte-identical
        b, a = before[h], after[h]
        assert {k: v for k, v in a.items() if k != "raw_json"} == \
               {k: v for k, v in b.items() if k != "raw_json"}


def test_map_markets_revisits_an_existing_row_missing_a_field(tmp_path):
    """Mapper fix pass (2026-09-04): the ORIGINAL bug — `_map_markets` did
    INSERT OR IGNORE, so a `markets` row already on hand (here: a prefix
    remap that filled `sport` but not `market_type`/`start_utc`) could never
    be revisited once written; MAP_SQL's LEFT JOIN excludes any condition_id
    with ANY markets row, mapped or not. REVISIT_SQL must pick this row up
    (no print required — it isn't in `prints` at all) and the upsert must
    WRITE the Gamma answer, not silently no-op."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    gamma = _fixture("whale_gamma_condition_ids_repeated")[0]
    cond = gamma["conditionId"]
    _map_row(con, cond, sport="baseball_kbo", market_type=None, start_utc=None,
           slug=gamma["slug"])
    con.commit()
    cfg = _cfg(backfill={"wallets_per_cycle": 3, "pages_per_wallet": 40,
                         "page_size": 50, "map_per_cycle": 40})

    res = whale_backfill.map_markets(cfg, con, NOW, get=FakeGet([gamma]))

    row = con.execute("SELECT * FROM markets WHERE condition_id=?", (cond,)).fetchone()
    assert row["sport"] == "baseball_kbo"                      # untouched, already right
    assert row["market_type"] == "WINNER"                      # filled from Gamma
    assert row["start_utc"] == "2026-08-09T09:00:00+00:00"     # filled from Gamma
    assert res["mapped"] == 1 and res["calls"] == 1


def test_map_markets_reaches_an_old_market_via_a_closed_true_retry(tmp_path):
    """F0 (mapper fix pass 2, 2026-09-04, live probe in data/verified_facts.md
    row 71): Gamma's default index omits an old/resolved market — 0/2 without
    `closed=true`, 2/2 with it. An id absent from the default pass must get a
    SECOND request with `closed=true` before it counts as not-on-Gamma."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    gamma = _fixture("whale_gamma_condition_ids_repeated")[0]
    cond = gamma["conditionId"]
    _print(con, "0xw", cond, "BUY", "900", int(NOW.timestamp()) - 100)
    _wallet(con, "0xw", "2026-09-01T00:00:00+00:00")   # MAP_SQL's relevance guard
    con.commit()
    cfg = _cfg(backfill={"wallets_per_cycle": 3, "pages_per_wallet": 40,
                         "page_size": 50, "map_per_cycle": 40})

    class ClosedOnly:
        """Answers only a query carrying closed=true, empty otherwise —
        exactly the live index gap F0 exists for."""

        def __init__(self, row):
            self.row, self.urls = row, []

        def __call__(self, url, cfg, **kw):
            self.urls.append(url)
            return [self.row] if "closed=true" in url else []

    get = ClosedOnly(gamma)
    res = whale_backfill.map_markets(cfg, con, NOW, get=get)

    row = con.execute("SELECT * FROM markets WHERE condition_id=?", (cond,)).fetchone()
    assert row["sport"] == "baseball_kbo" and row["market_type"] == "WINNER"
    assert res["mapped"] == 1 and res["unmappable"] == 0
    assert len(get.urls) == 2 and "closed=true" not in get.urls[0]
    assert "closed=true" in get.urls[1]
    assert whale_tape._meta(con, f"unmappable:{cond}") is None


def test_revisit_sql_drops_the_permanent_slug_floor_and_absent_never_reasked(tmp_path):
    """Legacy re-map (2026-09-04): 12,091 `markets` rows the OLD mapper
    (pre-4b3bcc9) inserted from Gamma's default index alone carry
    slug/sport/start_utc all NULL; REVISIT_SQL's permanent `slug IS NOT
    NULL` floor meant they could never be re-asked. Dropping the floor
    readmits them; an `absent:` marker (PERMANENT, unlike the one-day
    `unmappable:` throttle) stops Gamma being asked forever once BOTH
    passes come back empty for an id that just isn't there, while a legacy
    row Gamma DOES find (closed pass — live probe: these ids exist there
    with closed=true) gets slug/sport/start filled exactly like any other
    revisit row."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    gamma = _fixture("whale_gamma_condition_ids_repeated")[0]
    found_cond = gamma["conditionId"]
    _map_row(con, found_cond, sport=None, market_type=None, start_utc=None, slug=None,
           first_seen="2026-08-01T00:00:00+00:00")
    _map_row(con, "0xgone", sport=None, market_type=None, start_utc=None, slug=None,
           first_seen="2026-08-02T00:00:00+00:00")
    con.commit()
    cfg = _cfg(backfill={"wallets_per_cycle": 3, "pages_per_wallet": 40,
                         "page_size": 50, "map_per_cycle": 40})

    # Pre-fix RED: the permanent slug floor selects NEITHER row (both are
    # legacy slug-NULL rows) — this is the bug the brief describes.
    revisit = [r[0] for r in con.execute(whale_map.REVISIT_SQL, ("2026-09-03", 40))]
    assert set(revisit) == {found_cond, "0xgone"}

    # default pass matches `found_cond` only; closed pass finds nothing for
    # either (so "0xgone" is absent from BOTH passes).
    get = FakeGet([gamma], [])
    res = whale_backfill.map_markets(cfg, con, NOW, get=get)

    row = con.execute("SELECT * FROM markets WHERE condition_id=?", (found_cond,)).fetchone()
    assert row["sport"] == "baseball_kbo" and row["market_type"] == "WINNER"
    assert row["start_utc"] == "2026-08-09T09:00:00+00:00"
    assert res["mapped"] == 1 and res["unmappable"] == 1
    assert whale_tape._meta(con, f"absent:0xgone") == NOW.strftime("%Y-%m-%d")
    assert whale_tape._meta(con, f"absent:{found_cond}") is None

    # a new day clears the one-day `unmappable:` throttle, but `absent:` is
    # permanent: REVISIT_SQL must never select "0xgone" again.
    tomorrow = (NOW + timedelta(days=1)).strftime("%Y-%m-%d")
    revisit_after = [r[0] for r in con.execute(whale_map.REVISIT_SQL, (tomorrow, 40))]
    assert revisit_after == []


def test_a_non_list_gamma_body_raises_instead_of_being_coerced_to_absent(tmp_path):
    """CRITICAL fix (reviewer, 4b746cf): a 200 whose JSON body is not a list
    (a degraded Gamma error object, e.g. `{"error": "degraded"}`) used to be
    silently coerced to `[]` by `_call`, with status_counts still "ok". Both
    Gamma passes (default, then closed=true) then read as "found nothing",
    and `_map_markets` wrote a PERMANENT `absent:` marker — quarantining a
    real market forever, with nothing logged above INFO. `_call` must raise
    VenueError instead, so the SAME handling a bad HTTP status already gets
    (`map_markets`: backoff set, status="error", nothing written) covers
    this too — one guard at the shared choke point, not a second check in
    the mapper."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    cond = "0xdegraded"
    _print(con, "0xw", cond, "BUY", "900", int(NOW.timestamp()) - 100)
    _wallet(con, "0xw", "2026-09-01T00:00:00+00:00")   # MAP_SQL's relevance guard
    con.commit()
    cfg = _cfg(backfill={"wallets_per_cycle": 3, "pages_per_wallet": 40,
                         "page_size": 50, "map_per_cycle": 40})
    get = FakeGet({"error": "degraded", "markets": []})  # same dict both passes

    res = whale_backfill.map_markets(cfg, con, NOW, get=get)

    assert res["status"] == "error"
    assert res["status_counts"].get("error", 0) >= 1
    assert whale_tape._meta(con, f"absent:{cond}") is None
    assert whale_tape._meta(con, f"unmappable:{cond}") is None
    assert con.execute("SELECT * FROM markets WHERE condition_id=?",
                       (cond,)).fetchone() is None
    assert whale_tape._meta(con, "backoff_until") is not None


def test_mapper_closed_pass_records_a_resolution_and_the_sweep_ignores_it(tmp_path):
    """Legacy re-map (2026-09-04): the closed=true retry (F0) already fetches
    the FULL resolved-market row for anything missing from Gamma's default
    index — a Gamma row this mapper is about to use for sport/start mapping
    may carry a winner (`parse_resolution`) that the resolution sweep would
    otherwise spend its own call re-asking for once this market ages out of
    the 60s cycle's universe. `record_resolution` (shared with
    `read_resolutions`, arb/whale_resolve.py) persists it right here,
    source='mapper'; SWEEP_SQL's `NOT EXISTS resolutions` then excludes the
    market with no wasted call."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    resolved = dict(_fixture("whale_gamma_resolved"))
    resolved["slug"] = "kbo-lot-kt-2026-08-09"            # known prefix -> baseball_kbo
    resolved["sportsMarketType"] = "moneyline"
    resolved["gameStartTime"] = "2026-09-01 09:00:00+00"  # within resolve_after_hours/giveup window
    cond = resolved["conditionId"]
    _print(con, "0xw", cond, "BUY", "900", int(NOW.timestamp()) - 100)
    _wallet(con, "0xw", "2026-09-01T00:00:00+00:00")   # MAP_SQL's relevance guard
    con.commit()
    cfg = _cfg(backfill={"wallets_per_cycle": 3, "pages_per_wallet": 40, "page_size": 50,
                         "map_per_cycle": 40, "resolve_per_cycle": 40})

    res = whale_backfill.map_markets(cfg, con, NOW, get=FakeGet([], [resolved]))

    row = con.execute("SELECT sport, market_type, start_utc FROM markets "
                      "WHERE condition_id=?", (cond,)).fetchone()
    assert row["sport"] == "baseball_kbo" and row["market_type"] == "WINNER"
    assert res["mapped"] == 1
    resolution = whale_resolve.resolution_for(con, cond)
    assert resolution is not None and resolution.status == "resolved"
    assert con.execute("SELECT source FROM resolutions WHERE condition_id=?",
                       (cond,)).fetchone()[0] == "mapper"

    get2 = FakeGet([])
    n = whale_sweep.resolve_sweep(cfg, con, NOW, get=get2)
    assert n == 0 and get2.urls == []          # already resolved, never re-asked


def test_map_markets_never_overwrites_a_seeded_sport_with_a_slug_guess(tmp_path):
    """C1 (mapper fix pass 2, 2026-09-04): the upsert used to let THIS call's
    slug-heuristic sport (`sport=COALESCE(excluded.sport, sport)`, new wins)
    overwrite an existing value even when that value came from the watch
    lane's own sport TAG — ground truth, not a heuristic. A revisit row
    seeded sport=baseball_mlb (as `persist_markets` would from a tag) must
    keep it even though Gamma's slug for this exact market maps to a
    different sport (baseball_kbo); market_type/start_utc, genuinely
    missing, still fill from Gamma."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    gamma = _fixture("whale_gamma_condition_ids_repeated")[0]   # slug -> baseball_kbo
    cond = gamma["conditionId"]
    _map_row(con, cond, sport="baseball_mlb", market_type=None, start_utc=None,
           slug=gamma["slug"])
    con.commit()
    cfg = _cfg(backfill={"wallets_per_cycle": 3, "pages_per_wallet": 40,
                         "page_size": 50, "map_per_cycle": 40})

    res = whale_backfill.map_markets(cfg, con, NOW, get=FakeGet([gamma]))

    row = con.execute("SELECT * FROM markets WHERE condition_id=?", (cond,)).fetchone()
    assert row["sport"] == "baseball_mlb"                       # seeded value survives
    assert row["market_type"] == "WINNER"                       # genuinely missing, filled
    assert row["start_utc"] == "2026-08-09T09:00:00+00:00"      # genuinely missing, filled
    assert res["mapped"] == 1


def test_map_markets_marks_unmappable_when_only_start_utc_is_missing(tmp_path):
    """C2 (mapper fix pass 2, 2026-09-04): the marker used to key off
    sport+market_type only, so a market Gamma answers for but never gives a
    start (`gameStartTime` absent) got sport+market_type filled, NO marker,
    and — because REVISIT_SQL's own predicate DOES include start_utc — sat
    at the head of its FIFO burning one Gamma call every cycle forever. The
    marker must key off the row AS STORED against REVISIT_SQL's own
    predicate (sport, market_type OR start_utc still NULL)."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    gamma = dict(_fixture("whale_gamma_condition_ids_repeated")[0])
    gamma["gameStartTime"] = None
    cond = gamma["conditionId"]
    _print(con, "0xw", cond, "BUY", "900", int(NOW.timestamp()) - 100)
    _wallet(con, "0xw", "2026-09-01T00:00:00+00:00")   # MAP_SQL's relevance guard
    con.commit()
    cfg = _cfg(backfill={"wallets_per_cycle": 3, "pages_per_wallet": 40,
                         "page_size": 50, "map_per_cycle": 40})

    res1 = whale_backfill.map_markets(cfg, con, NOW, get=FakeGet([gamma]))

    row = con.execute("SELECT * FROM markets WHERE condition_id=?", (cond,)).fetchone()
    assert row["sport"] == "baseball_kbo" and row["market_type"] == "WINNER"
    assert row["start_utc"] is None
    assert res1["calls"] == 1 and res1["unmappable"] == 1 and res1["mapped"] == 0
    assert whale_tape._meta(con, f"unmappable:{cond}") == NOW.strftime("%Y-%m-%d")

    # second cycle, same day: the marker skips it entirely — 0 Gamma calls
    get2 = FakeGet([])
    res2 = whale_backfill.map_markets(cfg, con, NOW, get=get2)
    assert res2["calls"] == 0 and get2.urls == []


def test_map_markets_budget_reserves_a_floor_for_each_queue(tmp_path):
    """I1 (mapper fix pass 2, 2026-09-04): MAP_SQL used to take the WHOLE
    `map_per_cycle` budget and REVISIT_SQL only whatever was left, so a
    sustained inflow of new ids could starve the revisit queue indefinitely
    — the same unbounded-backlog class 2c74fff was fixing for revisit
    itself. Each queue gets a floor of `limit // 2`, isolated here from any
    Gamma call (`_queue` alone)."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    for i in range(100):
        _print(con, "0xw", f"0xnew{i}", "BUY", "900", int(NOW.timestamp()) - i)
    for i in range(100):
        _map_row(con, f"0xold{i}", sport="baseball_mlb", market_type=None,
               start_utc="2026-01-01T00:00:00+00:00", slug=f"mlb-x-y-{i}",
               first_seen=(NOW - timedelta(days=100 - i)).isoformat())
    _wallet(con, "0xw", "2026-09-01T00:00:00+00:00")   # MAP_SQL's relevance guard
    con.commit()

    new_ids, revisit = whale_backfill._queue(con, 40, NOW.strftime("%Y-%m-%d"))

    assert (len(new_ids), len(revisit)) == (20, 20)


def test_map_markets_budget_flows_a_short_revisit_queue_back_to_new(tmp_path):
    """The other half of I1's split: when revisit cannot fill its own floor
    (only 100 old rows exist but new supply is thin — 5 ids — the leftover
    still has to go SOMEWHERE), the unused share flows back to new ids,
    which have already proven there is more supply there."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    for i in range(5):
        _print(con, "0xw", f"0xnew{i}", "BUY", "900", int(NOW.timestamp()) - i)
    for i in range(100):
        _map_row(con, f"0xold{i}", sport="baseball_mlb", market_type=None,
               start_utc="2026-01-01T00:00:00+00:00", slug=f"mlb-x-y-{i}",
               first_seen=(NOW - timedelta(days=100 - i)).isoformat())
    _wallet(con, "0xw", "2026-09-01T00:00:00+00:00")   # MAP_SQL's relevance guard
    con.commit()

    new_ids, revisit = whale_backfill._queue(con, 40, NOW.strftime("%Y-%m-%d"))

    assert (len(new_ids), len(revisit)) == (5, 35)


def test_map_markets_prefix_only_remap_needs_no_gamma_call(tmp_path):
    """The other half of the fix: a revisit row that already carries its OWN
    slug can fill `sport` LOCALLY when that is the only field missing —
    `_local_remap` must never spend a Gamma call for it."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    _map_row(con, "0xprefixonly", sport=None, market_type="WINNER",
           start_utc="2026-08-09T09:00:00+00:00", slug="kbo-lot-kt-2026-08-09")
    con.commit()
    cfg = _cfg(backfill={"wallets_per_cycle": 3, "pages_per_wallet": 40,
                         "page_size": 50, "map_per_cycle": 40})
    get = FakeGet()                                  # raises if ever called

    res = whale_backfill.map_markets(cfg, con, NOW, get=get)

    row = con.execute(
        "SELECT sport FROM markets WHERE condition_id='0xprefixonly'").fetchone()
    assert row["sport"] == "baseball_kbo"
    assert res["mapped"] == 1 and res["calls"] == 0 and get.urls == []


def test_map_markets_marks_an_unmappable_revisit_row_for_the_day(tmp_path):
    """`unmappable:<cond>` mirrors `swept:`'s idiom: a row Gamma still can't
    map is skipped for the REST of today's cycles (no wasted calls chasing a
    market that will never resolve) and picked back up tomorrow — UNLESS
    Gamma found nothing for it on EITHER pass (legacy re-map, 2026-09-04:
    see test_revisit_sql_drops_the_permanent_slug_floor_and_absent_never_
    reasked): that additionally earns the row a PERMANENT `absent:` marker,
    so a condition_id Gamma has never heard of is asked once, not every day
    forever."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    _map_row(con, "0xhopeless", sport=None, market_type=None, start_utc=None,
           slug="will-something-happen-2026")             # a real slug, no prefix maps it
    con.commit()
    cfg = _cfg(backfill={"wallets_per_cycle": 3, "pages_per_wallet": 40,
                         "page_size": 50, "map_per_cycle": 40})
    get = FakeGet([])                                 # Gamma finds nothing either call

    res1 = whale_backfill.map_markets(cfg, con, NOW, get=get)
    res2 = whale_backfill.map_markets(cfg, con, NOW, get=get)   # same day: must not re-ask
    tomorrow = NOW + timedelta(days=1)
    res3 = whale_backfill.map_markets(cfg, con, tomorrow, get=get)  # new day: still absent

    # F0: Gamma finding nothing on the default pass earns the id a SECOND,
    # closed=true call before it counts as unmappable — 2 calls, not 1.
    assert res1["unmappable"] == 1 and res1["calls"] == 2
    assert res2["unmappable"] == 0 and res2["calls"] == 0       # skipped, marker still today
    assert res3["unmappable"] == 0 and res3["calls"] == 0       # absent: is permanent


def test_map_markets_bounds_new_ids_and_revisit_rows_together(tmp_path):
    """`map_per_cycle` bounds the UNION, not each source separately: with only
    one new id on hand (below its I1 floor of `limit // 2 == 1`), the unused
    new-queue budget flows to revisit — whatever is left over waits for the
    next cycle. See test_map_markets_budget_reserves_a_floor_for_each_queue
    (I1) for the split itself, isolated from any Gamma call."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    _print(con, "0xw", "0xnew", "BUY", "900", int(NOW.timestamp()) - 100)
    _map_row(con, "0xold1", sport="baseball_mlb", market_type=None,
           start_utc="2026-01-01T00:00:00+00:00", slug="mlb-x-y-2026-01-01",
           first_seen="2026-08-01T00:00:00+00:00")
    _map_row(con, "0xold2", sport="baseball_mlb", market_type=None,
           start_utc="2026-01-01T00:00:00+00:00", slug="mlb-a-b-2026-01-01",
           first_seen="2026-08-02T00:00:00+00:00")
    _wallet(con, "0xw", "2026-09-01T00:00:00+00:00")   # MAP_SQL's relevance guard
    con.commit()
    cfg = _cfg(backfill={"wallets_per_cycle": 3, "pages_per_wallet": 40,
                         "page_size": 50, "map_per_cycle": 2})
    get = FakeGet([])                                 # one call covers both (GAMMA_IDS_PER_CALL=20 >= map_per_cycle=2)

    res = whale_backfill.map_markets(cfg, con, NOW, get=get)

    assert set(_asked(get)) == {"0xnew", "0xold1"}     # 0xold2 waits for the next cycle
    # F0: Gamma finding nothing means a closed=true retry for the same 2 ids
    assert len(get.urls) == 2


def test_the_2026_09_04_census_prefixes_resolve_to_the_right_sport():
    """Regression guard for the shipped `slug_sport_prefixes` census additions
    (whale-backfill fix pass 2026-09-04) — one sample per family, not the
    whole 57: a traditional soccer league, a short prefix that collides on
    `startswith` with a longer sibling (kor/kor2), college football, FIBA
    basketball, and the esports prefixes the L14 amendment took back out."""
    prefixes = _cfg()["whale"]["slug_sport_prefixes"]
    assert whale_backfill._sport_of("epl-ars-che-2026-09-06-che", prefixes) == "soccer"
    assert whale_backfill._sport_of("kor-jej-uls-2026-09-05-jej", prefixes) == "soccer"
    assert whale_backfill._sport_of("kor2-bus-ang-2026-09-05-bus", prefixes) == "soccer"
    assert whale_backfill._sport_of(
        "cfb-ballst-ohiost-2026-09-05", prefixes) == "americanfootball_ncaaf"
    assert whale_backfill._sport_of(
        "bkfibaw-usa-chi-2026-09-04", prefixes) == "basketball_fiba_womens"
    # esports prefixes were OBSERVED in the census but are deliberately left
    # unmapped (controller decision 2026-09-04): `whale_score.root_of` pools
    # every mapped sport into the one `sports` root, so an `esports_*` key
    # would pool CS2 skill with MLB skill. Unmapped => never scored.
    for slug in ("cs2-b8-ntr-2026-09-04", "lol-t1-geng-2026-09-04-game1",
                 "dota2-tsp-ntr-2026-09-04", "val-tec-tyl-2026-09-04"):
        assert whale_backfill._sport_of(slug, prefixes) is None
    assert whale_backfill._sport_of("will-trump-win-2026", prefixes) is None  # question, unmapped


def test_the_mapping_queue_ignores_markets_no_known_wallet_has_traded(tmp_path):
    """Retention amendment 2026-09-04, the guard that has to land in the same
    commit: once prints are kept platform-wide, MAP_SQL's queue would otherwise
    inherit EVERY market on Polymarket — about 300 five-minute crypto markets a
    day, forever, each costing Gamma calls that map nothing. A market is only
    worth a call once a wallet we already follow has traded in it."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    ts = int(NOW.timestamp())
    _print(con, "0xknown", "0xworth_mapping", "BUY", "5000", ts - 100)
    _print(con, "0xstranger", "0xbtc_updown_4h", "BUY", "5000", ts - 200)  # older, still skipped
    _wallet(con, "0xknown", "2026-09-01T00:00:00+00:00")
    con.commit()

    new_ids, revisit = whale_backfill._queue(con, 40, NOW.strftime("%Y-%m-%d"))

    assert (new_ids, revisit) == (["0xworth_mapping"], [])


def test_map_markets_is_bounded_and_fifo_by_first_print(tmp_path):
    """Fix 1's actual bound: three markets never seen before, `map_per_cycle`
    covers only two, oldest first print first; the third waits."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    for i, age in enumerate((100, 300, 200)):        # insertion order != age
        _print(con, "0xw", f"0xm{i}", "BUY", "900", int(NOW.timestamp()) - age)
    _wallet(con, "0xw", "2026-09-01T00:00:00+00:00")   # MAP_SQL's relevance guard
    con.commit()
    cfg = _cfg(backfill={"wallets_per_cycle": 3, "pages_per_wallet": 40, "page_size": 50,
                         "map_per_cycle": 2})
    get = FakeGet([])          # one call covers both (GAMMA_IDS_PER_CALL=20, verified live 2026-09-04)

    res = whale_backfill.map_markets(cfg, con, NOW, get=get)

    # F0: Gamma finding nothing means a closed=true retry for the same chunk
    assert _asked(get) == ["0xm1", "0xm2", "0xm1", "0xm2"]
    assert len(get.urls) == 2
    assert res["unmappable"] == 2 and res["mapped"] == 0


def test_map_markets_short_circuits_while_a_backoff_is_live(tmp_path):
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    con.execute("INSERT INTO meta (key, value) VALUES ('backoff_until', ?)",
                ((NOW + timedelta(seconds=60)).isoformat(),))
    con.commit()
    get = FakeGet([])
    assert whale_backfill.map_markets(_cfg(), con, NOW, get=get)["status"] == "backoff"
    assert get.urls == []


def test_backfill_short_circuits_while_a_backoff_is_live(tmp_path):
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    _wallet(con, "0xw", "2026-09-01T00:00:00+00:00", source="leaderboard")
    con.execute("INSERT INTO meta (key, value) VALUES ('backoff_until', ?)",
                ((NOW + timedelta(seconds=60)).isoformat(),))
    con.commit()
    get = FakeGet([])
    assert whale_backfill.backfill(_cfg(), con, NOW, get=get)["status"] == "backoff"
    assert get.urls == []


def test_a_backoff_written_late_in_a_long_cycle_is_still_in_the_future(tmp_path):
    """A backfill cycle runs ~11 min against backoff_secs=300, so a pause
    stamped from the CYCLE-START clock has already expired when it is written."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    _wallet(con, "0xw", "2026-09-01T00:00:00+00:00", source="leaderboard")
    con.commit()
    started = datetime.now(timezone.utc) - timedelta(minutes=20)
    boom = FakeGet(VenueError("503 from https://data-api.polymarket.com/activity"))
    assert whale_backfill.backfill(_cfg(), con, started, get=boom)["status"] == "error"
    until = datetime.fromisoformat(whale_tape._meta(con, "backoff_until"))
    assert until > datetime.now(timezone.utc)


def test_a_cold_start_takes_one_page_and_does_not_walk_the_venues_history(tmp_path):
    """No last_seen => no gap to catch up on. Paging on the age rule alone
    would page forever, since every row of every page looks fresh."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    page = _fixture("whale_trades_global")
    get = FakeGet(page)                                # a full page, every time
    whale_tape.poll_prints(_cfg(page_size=len(page)), con, _global_markets(1), NOW, get=get)
    assert len(get.urls) == 1


# --- the resolution sweep (final review C1) ---------------------------------

def _resolvable(con, cond, start, wallet="0xw", side="BUY"):
    """A tape market a whale traded, aged past resolve_after_hours."""
    _market(con, cond, start.isoformat())
    _print(con, wallet, cond, side, "900", int(start.timestamp()) - 60)
    con.commit()


def _asked(get):
    ids = []
    for url in get.urls:
        ids += [p.split("=", 1)[1] for p in url.split("?", 1)[-1].split("&")
                if p.startswith("condition_ids=")]
    return ids


def test_the_sweep_resolves_a_backfilled_market_the_60s_cycle_never_reads(tmp_path):
    """C1 (final review 2026-09-04): the lane's own read_resolutions call only
    ever covers the CURRENT universe (start in [now-6h, now+48h]) plus open-bet
    markets, so a backfilled market is never read at all and no wallet can ever
    score. `whale-backfill --once` sweeps them."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    old = NOW - timedelta(days=3)                      # long out of the universe
    resolved = dict(_fixture("whale_gamma_resolved"))
    _resolvable(con, resolved["conditionId"], old)
    get = FakeGet([resolved])

    n = whale_sweep.resolve_sweep(_cfg(), con, NOW, get=get)

    assert n == 1
    row = con.execute("SELECT * FROM resolutions").fetchone()
    assert row["condition_id"] == resolved["conditionId"] and row["status"] == "resolved"


def test_the_sweep_is_fifo_by_start_and_bounded_by_resolve_per_cycle(tmp_path):
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    for i, days in enumerate((1, 3, 2)):               # insertion order != age
        _resolvable(con, f"0xm{i}", NOW - timedelta(days=days))
    cfg = _cfg(backfill={"wallets_per_cycle": 5, "pages_per_wallet": 40,
                         "page_size": 50, "resolve_per_cycle": 2})
    get = FakeGet([])

    whale_sweep.resolve_sweep(cfg, con, NOW, get=get)

    assert _asked(get) == ["0xm1", "0xm2"]             # oldest two starts, in order


def test_the_sweep_skips_markets_past_resolve_giveup_days_and_the_unresolved(tmp_path):
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    cfg = _cfg()
    give = int(cfg["whale"]["resolve_giveup_days"])
    _resolvable(con, "0xgaveup", NOW - timedelta(days=give + 1))
    _resolvable(con, "0xfresh", NOW - timedelta(hours=1))     # inside resolve_after_hours
    _resolvable(con, "0xnowhale", NOW - timedelta(days=1), side="SELL")
    _resolvable(con, "0xdone", NOW - timedelta(days=1))
    con.execute("INSERT INTO resolutions (condition_id, status) VALUES ('0xdone','resolved')")
    # unmappable: sport/market_type both NULL, exactly what a Gamma miss
    # writes (_gamma_row) — never scored, so never worth a sweep slot either.
    unmapped_start = NOW - timedelta(days=6)
    con.execute(
        "INSERT INTO markets (condition_id, sport, market_type, line, start_utc,"
        " event_key, tokens_json, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?)",
        ("0xnowhere", None, None, None, unmapped_start.isoformat(), None, "[]",
         NOW.isoformat(), NOW.isoformat()))
    _print(con, "0xw", "0xnowhere", "BUY", "900", int(unmapped_start.timestamp()) - 60)
    con.commit()
    get = FakeGet([])

    whale_sweep.resolve_sweep(cfg, con, NOW, get=get)

    assert _asked(get) == []


def test_the_sweep_writes_a_per_day_skip_marker_so_the_fifo_advances(tmp_path):
    """Head-of-line blocking (final review fix pass): `read_resolutions`
    persists nothing for a market Gamma reports open or omits, so without a
    marker the same oldest `resolve_per_cycle` ids get re-asked every cycle
    forever and nothing past them is ever swept. A `swept:<condition_id>`
    meta row dated today lets the FIFO advance within one day's cycle budget,
    and resets the next day; a resolved market is never swept again."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    resolved = dict(_fixture("whale_gamma_resolved"))
    ids = [resolved["conditionId"]] + [f"0xm{i}" for i in range(1, 6)]
    for i, days in enumerate(range(6, 0, -1)):          # ids[0] oldest .. ids[5] newest
        _resolvable(con, ids[i], NOW - timedelta(days=days))
    cfg = _cfg(backfill={"wallets_per_cycle": 5, "pages_per_wallet": 40,
                         "page_size": 50, "resolve_per_cycle": 2})
    get = FakeGet([])

    for _ in range(3):                                  # three 40 s cycles, same day
        whale_sweep.resolve_sweep(cfg, con, NOW, get=get)

    assert _asked(get) == ids                           # every id asked once, FIFO order

    # nothing left to sweep the same day: a 4th cycle makes no Gamma call
    get2 = FakeGet([])
    whale_sweep.resolve_sweep(cfg, con, NOW, get=get2)
    assert get2.urls == []

    # the next day: stale markers, the FIFO starts over from the oldest two —
    # and this time Gamma resolves the oldest
    tomorrow = NOW + timedelta(days=1)
    get3 = FakeGet([resolved])
    whale_sweep.resolve_sweep(cfg, con, tomorrow, get=get3)
    assert _asked(get3) == [ids[0], ids[1]]
    row = con.execute("SELECT * FROM resolutions WHERE condition_id=?",
                      (ids[0],)).fetchone()
    assert row["status"] == "resolved"

    # ... and a resolved market is never asked again
    get4 = FakeGet([])
    whale_sweep.resolve_sweep(cfg, con, tomorrow, get=get4)
    assert _asked(get4) == [ids[2], ids[3]]


def test_whale_backfill_command_runs_the_sweep(tmp_path, monkeypatch):
    """The sweep is part of `whale-backfill --once` (spec §5 step 5), never of
    the 60 s cycle: at 2 ids a call it is minutes of paced Gamma reads."""
    from arb.commands import cmd_whale_backfill
    from arb.store import Store
    cfg = _cfg()
    cfg["whale"]["tape_db"] = str(tmp_path / "w.sqlite")
    calls = []
    monkeypatch.setattr(whale_backfill, "snapshot_leaderboard", lambda *a, **k: 0)
    monkeypatch.setattr(whale_backfill, "backfill", lambda *a, **k: {
        "wallets": 0, "rows": 0, "dupes": 0, "bad_rows": 0, "calls": 0,
        "mapped": 0, "unmappable": 0, "status_counts": {}, "status": "ok"})
    monkeypatch.setattr(whale_sweep, "resolve_sweep",
                        lambda *a, **k: calls.append(a) or 0)

    cmd_whale_backfill(cfg, Store(tmp_path / "arb.sqlite"), None)

    assert len(calls) == 1


def test_whale_backfill_runs_leaderboard_then_sweep_then_backfill_then_mapping(
        tmp_path, monkeypatch, capsys):
    """Whale-backfill fix pass (2026-09-04, live diagnosis 12:20Z): `backfill`
    and mapping can each run for minutes, and either one used to be able to
    blow the child's 900s cap. Mapping used to run last (inside `backfill`)
    and the sweep dead last of all — so a slow cycle starved the sweep, the
    ONLY step that ever puts a resolution on a wallet's scoreboard (C1). The
    sweep now runs right after the leaderboard snapshot, before either slow
    step, so it always gets its full budget regardless of what backfill or
    mapping do this cycle."""
    from arb.commands import cmd_whale_backfill
    from arb.store import Store
    cfg = _cfg()
    cfg["whale"]["tape_db"] = str(tmp_path / "w.sqlite")
    order = []
    monkeypatch.setattr(whale_backfill, "snapshot_leaderboard",
                        lambda *a, **k: order.append("leaderboard") or 0)
    monkeypatch.setattr(whale_sweep, "resolve_sweep",
                        lambda *a, **k: order.append("sweep") or 0)
    monkeypatch.setattr(whale_backfill, "backfill", lambda *a, **k: order.append("backfill") or {
        "wallets": 0, "rows": 0, "dupes": 0, "bad_rows": 0, "calls": 0, "status": "ok"})
    monkeypatch.setattr(whale_backfill, "map_markets", lambda *a, **k: order.append("map") or {
        "calls": 0, "mapped": 0, "unmappable": 0, "no_root": 0, "unrooted": 0,
        "status_counts": {}, "status": "ok"})

    cmd_whale_backfill(cfg, Store(tmp_path / "arb.sqlite"), None)

    assert order == ["leaderboard", "sweep", "backfill", "map"]
    out = capsys.readouterr().out
    assert out.index("resolutions swept:") < out.index("backfill:") < out.index("mapping:")


# --- the leg convention on a 3-way proposition (final review I2) -------------

def _prop_ledger(prop="Arsenal", sides=("YES", "NO")):
    """arb/venues/polymarket.py's own shape for `Will X win?`: BOTH tokens carry
    the PROPOSITION as their label and are told apart by `side` only."""
    return _ledger([_q("0x3way", "tokY", prop, IN_WINDOW, side=sides[0],
                       sport="soccer_epl"),
                    _q("0x3way", "tokN", prop, IN_WINDOW, side=sides[1],
                       sport="soccer_epl")])


def test_a_three_way_proposition_gets_one_label_per_token():
    """I2: `universe` mapped token -> outcome ignoring `side`, so a soccer
    3-way collapsed to ONE label — and with it one fingerprint, one
    position_key and one quotes key tuple for two different tokens."""
    from arb.lanes.whale_book import quote_rows
    from arb.models import position_key

    (m,) = whale_universe.universe(_prop_ledger(), _cfg(), NOW)

    assert dict(m.tokens) == {"tokY": "Yes", "tokN": "No"}
    keys = {position_key("WHALE", m.sport, "pm:e", m.market_type, m.line,
                         outcome=label, side="YES") for _, label in m.tokens}
    assert len(keys) == 2
    book = {"asks": [{"price": "0.51", "size": "1000"}],
            "bids": [{"price": "0.49", "size": "1000"}]}
    rows = quote_rows(_cfg(), m, {"tokY": book, "tokN": book}, "pm:e", NOW)
    assert {(r["outcome"], r["side"], r["venue_market_id"]) for r in rows} == {
        ("Yes", "YES", "0x3way:tokY"), ("No", "YES", "0x3way:tokN")}


def test_a_market_whose_tokens_cannot_be_told_apart_is_skipped_and_counted():
    """Rule 3's habit: an ambiguity we cannot resolve is dropped and reported,
    never guessed into a colliding key."""
    res = {}

    markets = whale_universe.universe(_prop_ledger(sides=("YES", "YES")),
                                      _cfg(), NOW, res=res)

    assert markets == []
    assert res["label_collision"] == 1


# --- holdings: the authoritative "what do they still hold" check (§9, item C)

def _position_row(wallet, size, asset="tokyes", cid=MAKER_CID, idx=0, value=17):
    """One `positions?user=` row. Every field is from the live-probed row in
    data/verified_facts.md (2026-09-04); the collector reads only conditionId,
    asset, outcomeIndex, size, avgPrice, totalBought, redeemable and (since the
    2026-09-04 retention amendment) currentValue, for the summary row."""
    return {"proxyWallet": wallet, "conditionId": cid, "asset": asset,
            "outcomeIndex": idx, "size": size, "avgPrice": 0.182,
            "totalBought": 1000, "redeemable": False, "curPrice": 0.115,
            "currentValue": value, "outcome": "Rays",
            "slug": "mlb-tb-nyy-2026-09-03-tb",
            "eventSlug": "mlb-tb-nyy-2026-09-03", "title": "Rays vs Yankees",
            "negativeRisk": False, "mergeable": False}


def _holdings_tape(tmp_path):
    """A tape that TRACKS the market every `_position_row` belongs to. Since
    the 2026-09-04 retention amendment a holdings snapshot keeps only the rows
    whose condition_id is in `markets` (plus one summary row), so a holdings
    test has to say which markets it tracks."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    _market(con, MAKER_CID, IN_WINDOW)
    con.commit()
    return con


def _ctx(wallets, side=0, token="tokyes"):
    return {"variant": "V1", "side": side, "condition_id": MAKER_CID,
            "token": token, "start_utc": IN_WINDOW,
            "whales": [{"wallet": w, "side": side, "lb": "0.05",
                        "open_size": "1000"} for w in wallets]}


def test_holdings_poll_persists_every_snapshot_row(tmp_path):
    """Rule 7: what we observed is stored whether or not it moves an exit."""
    con = _holdings_tape(tmp_path)
    get = FakeGet([_position_row("0xw0", 400), _position_row("0xw0", 900, asset="tokno", idx=1)])
    res = whale_holdings.poll_holdings(_cfg(), con, ["0xw0"], NOW, get=get)

    assert res["wallets"] == 1 and res["rows"] == 2 and res["calls"] == 1
    assert "positions?user=0xw0&limit=500" in get.urls[0]
    rows = [dict(r) for r in con.execute("SELECT * FROM holdings ORDER BY asset")]
    assert [r["asset"] for r in rows] == ["tokno", "tokyes"]
    assert rows[1]["observed_at"] == NOW.isoformat()
    assert (rows[1]["proxy_wallet"], rows[1]["condition_id"]) == ("0xw0", MAKER_CID)
    assert (rows[1]["size"], rows[1]["avg_price"]) == ("400.000000", "0.182000")
    assert (rows[1]["outcome_index"], rows[1]["redeemable"]) == (0, 0)
    assert json.loads(rows[1]["raw_json"])["curPrice"] == 0.115


def test_holdings_poll_caps_the_calls_per_cycle_and_round_robins_by_staleness(tmp_path):
    """<=holdings_calls_per_cycle wallets a 60 s cycle, stalest first, so a
    roster bigger than the cap is still refreshed rather than starved."""
    con = _holdings_tape(tmp_path)
    wallets = [f"0xw{i:02d}" for i in range(25)]
    cap = int(_cfg()["whale"]["exit"]["holdings_calls_per_cycle"])
    assert cap == 10

    first = whale_holdings.poll_holdings(_cfg(), con, wallets, NOW, get=FakeGet([]))
    second = whale_holdings.poll_holdings(_cfg(), con, wallets,
                                          NOW + timedelta(seconds=60), get=FakeGet([]))

    assert (first["calls"], second["calls"]) == (cap, cap)
    polled = {r[0] for r in con.execute(
        "SELECT substr(key, 13) FROM meta WHERE key LIKE 'holdings_at:%'")}
    assert polled == set(wallets[:20])            # 10 + the next 10, none repeated


def test_holdings_a_wallet_polled_within_the_cadence_is_not_polled_again(tmp_path):
    con = _holdings_tape(tmp_path)
    whale_holdings.poll_holdings(_cfg(), con, ["0xw0"], NOW, get=FakeGet([]))
    cadence = int(_cfg()["whale"]["exit"]["holdings_poll_secs"])
    soon = whale_holdings.poll_holdings(_cfg(), con, ["0xw0"],
                                        NOW + timedelta(seconds=cadence - 1), get=FakeGet([]))
    due = whale_holdings.poll_holdings(_cfg(), con, ["0xw0"],
                                       NOW + timedelta(seconds=cadence), get=FakeGet([]))
    assert (soon["calls"], due["calls"]) == (0, 1)


def test_holdings_for_bet_reads_the_freshest_snapshot_and_zeroes_a_missing_token(tmp_path):
    con = _holdings_tape(tmp_path)
    get = FakeGet([_position_row("0xw0", 400)], [_position_row("0xw1", 900, asset="tokno", idx=1)])
    whale_holdings.poll_holdings(_cfg(), con, ["0xw0", "0xw1"], NOW, get=get)

    held = whale_holdings.for_bet(_cfg(), con, _ctx(["0xw0", "0xw1"]), NOW)

    # w0 holds 400 of the bet's token; w1 was polled and no longer holds it at
    # all — a fresh reading of zero, not an absent one.
    assert held == {"0xw0": Decimal("400.000000"), "0xw1": Decimal(0)}


def test_holdings_older_than_the_max_age_are_ignored_not_believed(tmp_path):
    """A snapshot older than holdings_poll_secs x holdings_max_age_polls is no
    reading at all: that wallet falls back to the prints (absent from the
    mapping), because exiting on a stale holding is exactly the failure item A
    just fixed on the other rule."""
    con = _holdings_tape(tmp_path)
    cfg = _cfg()
    ex = cfg["whale"]["exit"]
    max_age = int(ex["holdings_poll_secs"]) * int(ex["holdings_max_age_polls"])
    whale_holdings.poll_holdings(cfg, con, ["0xw0"], NOW,
                                 get=FakeGet([_position_row("0xw0", 400)]))
    ctx = _ctx(["0xw0"])

    assert whale_holdings.for_bet(cfg, con, ctx, NOW + timedelta(seconds=max_age)) \
        == {"0xw0": Decimal("400.000000")}
    assert whale_holdings.for_bet(cfg, con, ctx,
                                  NOW + timedelta(seconds=max_age + 1)) == {}


def test_holdings_a_failed_wallet_costs_only_that_wallet(tmp_path):
    """One venue error is not an outage: the other wallets are still read and
    the print collector's own backoff is untouched (it is a different feed)."""
    con = _holdings_tape(tmp_path)
    get = FakeGet(VenueError("polymarket 503"), [_position_row("0xw1", 400)])
    res = whale_holdings.poll_holdings(_cfg(), con, ["0xw0", "0xw1"], NOW, get=get)

    assert (res["errors"], res["wallets"], res["rows"]) == (1, 1, 1)
    assert whale_tape._meta(con, "backoff_until") is None
    assert whale_tape._meta(con, "holdings_at:0xw0") is None   # retried next cycle


def test_holdings_an_empty_page_is_no_reading_not_a_verified_zero(tmp_path):
    """Rule 3: the live probe verified what `positions?user=` returns for a
    wallet WITH open positions, never what an empty body means. Reading it as
    "sold everything" would exit every bet behind that whale on one bad
    response; a whale that really did sell everything prints its SELL leg
    under `taker_only: false` anyway. So the wallet is absent from the
    mapping — the prints stand — while the poll still counts as done, so the
    round-robin moves on rather than retrying it forever."""
    con = _holdings_tape(tmp_path)
    res = whale_holdings.poll_holdings(_cfg(), con, ["0xw0"], NOW, get=FakeGet([]))

    assert (res["wallets"], res["rows"], res["errors"]) == (1, 0, 0)
    assert whale_tape._meta(con, "holdings_at:0xw0") == NOW.isoformat()
    assert whale_holdings.for_bet(_cfg(), con, _ctx(["0xw0"]), NOW) == {}
    # ... and the summary says the page really was empty, which is what makes
    # "no reading" distinguishable from "held nothing we track" below.
    summary = con.execute("SELECT * FROM holdings_snapshots").fetchone()
    assert (summary["n_positions"], summary["partial"]) == (0, 0)


# --- holdings retention (addendum 2026-09-04): keep the tracked rows ---------

def test_holdings_keeps_only_tracked_markets_plus_one_wallet_summary(tmp_path):
    """`holdings` grew ~39k rows in ~2.5 h over 20 wallets — every poll stored
    the wallet's WHOLE positions page. The observation the lane USES is the
    size of a signal whale's position in a market we hold or track, so a poll
    keeps the rows whose condition_id is in `markets` and one compact summary
    row for the page as a whole (rule 7: the full-page fact is not lost)."""
    con = _holdings_tape(tmp_path)
    page = ([_position_row("0xw0", 10, asset=f"f{i}", cid=f"0xuntracked{i}", value=2)
             for i in range(488)]
            + [_position_row("0xw0", 10, asset=f"t{i}", value=3) for i in range(12)])
    get = FakeGet(page, [])                       # a full page, then a short one

    res = whale_holdings.poll_holdings(_cfg(), con, ["0xw0"], NOW, get=get)

    assert (res["wallets"], res["rows"], res["calls"]) == (1, 12, 2)
    kept = [dict(r) for r in con.execute("SELECT * FROM holdings")]
    assert len(kept) == 12 and {r["condition_id"] for r in kept} == {MAKER_CID}
    summary = con.execute("SELECT * FROM holdings_snapshots").fetchone()
    assert (summary["observed_at"], summary["proxy_wallet"]) == (NOW.isoformat(), "0xw0")
    assert summary["n_positions"] == 500                    # the whole page, both calls
    assert summary["total_value_usd"] == "1012.000000"      # 488 x 2 + 12 x 3
    assert summary["partial"] == 0


def test_holdings_keep_all_stores_the_whole_page(tmp_path):
    """`exit.holdings_keep: all` is the documented way back to storing every
    row of every page."""
    con = _holdings_tape(tmp_path)
    cfg = _cfg()
    cfg["whale"]["exit"] = {**cfg["whale"]["exit"], "holdings_keep": "all"}
    page = [_position_row("0xw0", 10, asset=f"f{i}", cid=f"0xuntracked{i}") for i in range(3)]

    res = whale_holdings.poll_holdings(cfg, con, ["0xw0"], NOW, get=FakeGet(page))

    assert res["rows"] == 3
    assert con.execute("SELECT COUNT(*) FROM holdings").fetchone()[0] == 3


def test_holdings_a_page_holding_none_of_our_markets_is_still_a_real_zero(tmp_path):
    """The summary row is what keeps `for_bet`'s three-way reading intact once
    the rows are filtered: a wallet whose page held 300 positions, none of them
    in a market we track, has been READ and genuinely holds none of the bet's
    token — a zero, not an absence. Only an EMPTY page is no reading."""
    con = _holdings_tape(tmp_path)
    page = [_position_row("0xw0", 10, asset=f"f{i}", cid=f"0xuntracked{i}")
            for i in range(300)]

    whale_holdings.poll_holdings(_cfg(), con, ["0xw0"], NOW, get=FakeGet(page))

    assert con.execute("SELECT COUNT(*) FROM holdings").fetchone()[0] == 0
    assert whale_holdings.for_bet(_cfg(), con, _ctx(["0xw0"]), NOW) == {"0xw0": Decimal(0)}


# --- holdings retention fix (2026-09-05): tracked set is the LANE's, not ----
# --- the `markets` table (which grew to every market backfill ever mapped) -

def test_holdings_universe_mode_keeps_only_the_callers_explicit_tracked_set(tmp_path):
    """`holdings_keep: universe` (2026-09-05 fix, new shipped default): the
    tracked set is whatever the CALLER passes — the lane's current-cycle
    universe union open bets' markets — never the `markets` table, which had
    grown to every market backfill EVER mapped (14k+ rows) and made a signal
    whale's whole historical portfolio 'tracked' (measured: +57,844
    holdings rows in one hour). Same shape as the 2026-09-04 `markets`-mode
    test above, but the 12 kept rows come from the explicit set, not a table
    read — proven by a BARE tape whose `markets` table is empty."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")     # no _market(): table is empty
    cfg = _cfg()
    cfg["whale"]["exit"] = {**cfg["whale"]["exit"], "holdings_keep": "universe"}
    page = ([_position_row("0xw0", 10, asset=f"f{i}", cid=f"0xuntracked{i}", value=2)
             for i in range(488)]
            + [_position_row("0xw0", 10, asset=f"t{i}", value=3) for i in range(12)])
    get = FakeGet(page, [])                       # a full page, then a short one

    res = whale_holdings.poll_holdings(cfg, con, ["0xw0"], NOW, {MAKER_CID}, get=get)

    assert (res["wallets"], res["rows"], res["calls"]) == (1, 12, 2)
    kept = [dict(r) for r in con.execute("SELECT * FROM holdings")]
    assert len(kept) == 12 and {r["condition_id"] for r in kept} == {MAKER_CID}
    summary = con.execute("SELECT * FROM holdings_snapshots").fetchone()
    assert (summary["observed_at"], summary["proxy_wallet"]) == (NOW.isoformat(), "0xw0")
    assert summary["n_positions"] == 500                    # the whole page, both calls
    assert summary["total_value_usd"] == "1012.000000"      # 488 x 2 + 12 x 3
    assert summary["partial"] == 0


def test_holdings_universe_mode_keeps_an_open_bets_market_outside_the_universe(tmp_path):
    """for_bet reads a bet's own market regardless of whether that market is
    still in THIS cycle's live universe — a market can drop out of the watch
    feed (game started, delisted) while a bet on it stays open. The lane
    folds every open bet's condition_id into the set it passes (§9 amendment
    2026-09-05); here the explicit set is JUST the bet's market, with no live
    universe at all, and its holding still survives because it is an open
    bet, not because it is in `markets`."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")     # bare: no live universe either
    cfg = _cfg()
    cfg["whale"]["exit"] = {**cfg["whale"]["exit"], "holdings_keep": "universe"}
    off_universe_cid = "0xoffuniverse"
    get = FakeGet([_position_row("0xw0", 400, cid=off_universe_cid)])

    res = whale_holdings.poll_holdings(cfg, con, ["0xw0"], NOW, {off_universe_cid}, get=get)

    assert res["rows"] == 1
    kept = con.execute("SELECT condition_id FROM holdings").fetchone()
    assert kept[0] == off_universe_cid
    ctx = _ctx(["0xw0"], token="tokyes")
    ctx["condition_id"] = off_universe_cid
    held = whale_holdings.for_bet(cfg, con, ctx, NOW)
    assert held == {"0xw0": Decimal("400.000000")}


def test_holdings_universe_mode_a_page_with_none_of_the_tracked_set_still_summarises(tmp_path):
    """Same three-way reading as the `markets`-mode test above, under
    `universe` mode: a wallet whose page holds nothing in the caller's
    tracked set has still been READ — a real zero, not an absent reading —
    and that is what the summary row (not a `holdings` row) has to carry."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    cfg = _cfg()
    cfg["whale"]["exit"] = {**cfg["whale"]["exit"], "holdings_keep": "universe"}
    page = [_position_row("0xw0", 10, asset=f"f{i}", cid=f"0xuntracked{i}")
            for i in range(300)]

    whale_holdings.poll_holdings(cfg, con, ["0xw0"], NOW, {MAKER_CID}, get=FakeGet(page))

    assert con.execute("SELECT COUNT(*) FROM holdings").fetchone()[0] == 0
    summary = con.execute("SELECT * FROM holdings_snapshots").fetchone()
    assert (summary["n_positions"], summary["partial"]) == (300, 0)
    assert whale_holdings.for_bet(cfg, con, _ctx(["0xw0"]), NOW) == {"0xw0": Decimal(0)}


# --- holdings paging fix (2026-09-04): a full page keeps going ---------------

def test_holdings_poll_pages_past_one_page_until_a_short_page(tmp_path):
    """Item 1 of the holdings-paging fix: a wallet whose page 1 comes back
    full must not be read as "sold out" of a token that only shows up on
    page 3 — the poll continues by `offset` (verified live 2026-09-04) until
    a short page or holdings_max_pages, and every page lands under the SAME
    observed_at so `for_bet` sees the real size wherever it lands."""
    con = _holdings_tape(tmp_path)
    page1 = [_position_row("0xw0", 10, asset=f"filler{i}") for i in range(500)]
    page2 = [_position_row("0xw0", 10, asset=f"filler{500 + i}") for i in range(500)]
    page3 = ([_position_row("0xw0", 10, asset=f"filler{1000 + i}") for i in range(36)]
             + [_position_row("0xw0", "777.5", asset="tokyes")])
    get = FakeGet(page1, page2, page3)

    res = whale_holdings.poll_holdings(_cfg(), con, ["0xw0"], NOW, get=get)

    assert (res["wallets"], res["rows"], res["calls"], res["errors"]) == (1, 1037, 3, 0)
    assert [u.split("offset=")[1] for u in get.urls] == ["0", "500", "1000"]
    held = whale_holdings.for_bet(_cfg(), con, _ctx(["0xw0"]), NOW)
    assert held == {"0xw0": Decimal("777.500000")}    # real size, not a hard zero


def test_holdings_marks_a_still_full_snapshot_partial(tmp_path):
    """Item 2: a wallet still full at holdings_max_pages is truncated, not
    fully read. The snapshot is marked partial; a token PRESENT in it is
    still a valid size, but a token ABSENT from it may simply be on the page
    that was never fetched — no reading, not a verified zero."""
    con = _holdings_tape(tmp_path)
    cfg = _cfg()
    cfg["whale"]["exit"] = {**cfg["whale"]["exit"],
                            "holdings_page_size": 2, "holdings_max_pages": 2}
    page1 = [_position_row("0xw0", "5", asset="tokyes"), _position_row("0xw0", 1, asset="b")]
    page2 = [_position_row("0xw0", 1, asset="c"), _position_row("0xw0", 1, asset="d")]
    get = FakeGet(page1, page2)

    res = whale_holdings.poll_holdings(cfg, con, ["0xw0"], NOW, get=get)

    assert (res["wallets"], res["rows"], res["calls"], res["errors"]) == (1, 4, 2, 0)
    assert whale_holdings.for_bet(cfg, con, _ctx(["0xw0"], token="tokyes"), NOW) \
        == {"0xw0": Decimal("5.000000")}                      # present: trusted
    assert whale_holdings.for_bet(cfg, con, _ctx(["0xw0"], token="zzz_missing"), NOW) == {}


def test_holdings_truncation_warns_once_per_wallet_per_day(tmp_path, caplog):
    """Item 3: the truncation WARNING is keyed by (wallet, UTC day), not a
    module-level set — a second poll of the same wallet the same day is
    silent, and a poll on a new day warns again."""
    con = _holdings_tape(tmp_path)
    cfg = _cfg()
    cfg["whale"]["exit"] = {**cfg["whale"]["exit"],
                            "holdings_page_size": 1, "holdings_max_pages": 1}
    cadence = int(cfg["whale"]["exit"]["holdings_poll_secs"])
    full_page = lambda: [_position_row("0xw0", 1, asset="tokyes")]

    with caplog.at_level(logging.WARNING, logger="arb.whale"):
        whale_holdings.poll_holdings(cfg, con, ["0xw0"], NOW, get=FakeGet(full_page()))
        caplog.clear()
        whale_holdings.poll_holdings(cfg, con, ["0xw0"],
                                     NOW + timedelta(seconds=cadence), get=FakeGet(full_page()))
        assert caplog.records == []                    # same UTC day: already warned

        caplog.clear()
        whale_holdings.poll_holdings(cfg, con, ["0xw0"],
                                     NOW + timedelta(days=1), get=FakeGet(full_page()))
        assert len(caplog.records) == 1                 # a new day: warns again


# --- holdings-paging fix, part 2 (2026-09-04): cap checked per page, --------
# --- each page persisted as it lands (review of 489bb3e/c4bbd92) -----------

def test_holdings_cap_is_checked_before_every_page_not_just_between_wallets(tmp_path):
    """Item 1: the OLD code checked holdings_calls_per_cycle only between
    wallets, so an admitted wallet ran every page of holdings_max_pages
    regardless — worst case cap - 1 + max_pages calls, not the cap. Cap 3,
    two wallets each needing 2 pages to finish: 0xa finishes (2 calls), 0xb
    is cut after its first page (the 3rd call) — exactly 3 calls total. 0xb
    is left unmarked (no POLLED stamp) so it stays the stalest and the very
    next cycle finishes it, starting over from offset 0."""
    con = _holdings_tape(tmp_path)
    cfg = _cfg()
    cfg["whale"]["exit"] = {**cfg["whale"]["exit"], "holdings_page_size": 2,
                            "holdings_calls_per_cycle": 3, "holdings_max_pages": 4}
    a1 = [_position_row("0xa", 1, asset="a1"), _position_row("0xa", 1, asset="a2")]
    a2 = [_position_row("0xa", 1, asset="a3")]                       # short: 0xa done
    b1 = [_position_row("0xb", 1, asset="b1"), _position_row("0xb", 1, asset="b2")]
    get = FakeGet(a1, a2, b1)

    res = whale_holdings.poll_holdings(cfg, con, ["0xa", "0xb"], NOW, get=get)

    assert res["calls"] == 3                                # the cap, not 1 + max_pages
    assert whale_tape._meta(con, "holdings_at:0xa") == NOW.isoformat()     # 0xa: done
    assert whale_tape._meta(con, "holdings_at:0xb") is None                # 0xb: cut, still due
    b_rows = {r[0] for r in con.execute(
        "SELECT asset FROM holdings WHERE proxy_wallet='0xb'")}
    assert b_rows == {"b1", "b2"}                            # what it fetched is kept

    # stays the stalest: round-robin picks it first, and the very next cycle
    # (well inside the same cadence) finishes it.
    assert whale_holdings.due(cfg, con, ["0xa", "0xb"], NOW + timedelta(seconds=60)) == ["0xb"]
    get2 = FakeGet([_position_row("0xb", 1, asset="b1")])       # short: completes in one call
    res2 = whale_holdings.poll_holdings(cfg, con, ["0xa", "0xb"],
                                        NOW + timedelta(seconds=60), get=get2)
    assert res2["calls"] == 1 and "offset=0" in get2.urls[0]    # starts over, not resumed
    assert whale_tape._meta(con, "holdings_at:0xb") == (NOW + timedelta(seconds=60)).isoformat()


def test_holdings_a_page_2_failure_keeps_page_1s_rows(tmp_path):
    """Item 2: `body` used to accumulate across pages and persist only after
    the whole per-wallet loop — a page-2 failure discarded page 1's real,
    already-fetched rows, contradicting the module's own rule-7 docstring
    ("every snapshot is persisted"). Each page is now written as it lands, so
    page 1 survives a page-2 VenueError; the wallet is left unmarked (no
    POLLED stamp) so it is retried whole, not half-believed, next cycle."""
    con = _holdings_tape(tmp_path)
    cfg = _cfg()
    cfg["whale"]["exit"] = {**cfg["whale"]["exit"], "holdings_page_size": 2}
    page1 = [_position_row("0xw0", "5", asset="tokyes"), _position_row("0xw0", 1, asset="b")]
    get = FakeGet(page1, VenueError("polymarket 503"))

    res = whale_holdings.poll_holdings(cfg, con, ["0xw0"], NOW, get=get)

    assert (res["calls"], res["errors"]) == (2, 1)
    rows = {r[0] for r in con.execute(
        "SELECT asset FROM holdings WHERE proxy_wallet='0xw0'")}
    assert rows == {"tokyes", "b"}                            # page 1 kept, not discarded
    assert whale_tape._meta(con, "holdings_at:0xw0") is None  # not half-believed
    assert whale_holdings.due(cfg, con, ["0xw0"], NOW) == ["0xw0"]   # still due, immediately


def test_holdings_persist_commits_so_a_second_connection_sees_the_rows(tmp_path):
    """C1 (review of a90a08d): `_persist` executed its INSERT with no
    `con.commit()` — the previous test above reads page 1's rows back through
    the SAME connection, which sees its own uncommitted writes and cannot
    tell a real commit from a pending transaction that would vanish if this
    process died before some LATER write happened to commit it (contradicting
    the module's own docstring, "can never discard what already landed"). A
    second, fresh connection to the same file proves the rows actually
    reached disk."""
    path = tmp_path / "w.sqlite"
    con = _holdings_tape(tmp_path)                # the tape TRACKS _position_row's market
    cfg = _cfg()
    cfg["whale"]["exit"] = {**cfg["whale"]["exit"], "holdings_page_size": 2}
    page1 = [_position_row("0xw0", "5", asset="tokyes"), _position_row("0xw0", 1, asset="b")]
    get = FakeGet(page1, VenueError("polymarket 503"))    # page 2 fails; no _set_meta commit follows

    whale_holdings.poll_holdings(cfg, con, ["0xw0"], NOW, get=get)

    fresh = sqlite3.connect(str(path))
    rows = {r[0] for r in fresh.execute(
        "SELECT asset FROM holdings WHERE proxy_wallet='0xw0'")}
    fresh.close()
    assert rows == {"tokyes", "b"}                 # page 1's rows, visible cross-connection


# --- historical non-sports roots (amendment 2026-09-05) ---------------------
#
# `_gamma_row` rooted a market from its sport slug prefix alone, so every
# historical politics/crypto market a whale had traded stayed `sport=NULL`,
# `positions_from_prints` dropped it and no politics whale could ever be
# scored (Opus review C2). Gamma's market row carries no `tags` and no
# `category`, and its nested `events[0]` carries no `tags` either — only the
# event's own row does (`events?id=`, batched 20 a call; live probes
# 2026-09-05 in data/verified_facts.md).

def _hist_market(cond, slug, event_id, end="2026-11-03T12:00:00Z"):
    return {"conditionId": cond, "slug": slug, "endDate": end,
            "startDate": "2026-01-01T00:00:00Z", "createdAt": "2025-12-01T00:00:00Z",
            "clobTokenIds": '["t0", "t1"]', "outcomes": '["Yes", "No"]',
            "volume24hr": "1234",
            "events": [{"id": event_id, "slug": "which-party-wins"}]}


def _hist_event(event_id, tags=("politics",)):
    return {"id": event_id, "slug": "which-party-wins",
            "startDate": "2025-12-01T00:00:00Z",
            "tags": [{"slug": t, "label": t} for t in tags]}


class _RootGet:
    """Answers `markets?` from one list and `events?` from another, so a test
    can see exactly which endpoint the mapper reached for."""

    def __init__(self, markets, events):
        self.markets, self.events, self.urls = markets, events, []

    def __call__(self, url, cfg, **kw):
        self.urls.append(url)
        return list(self.events) if "/events?" in url else list(self.markets)


def _map_cfg(**ns):
    cfg = _cfg(backfill={"wallets_per_cycle": 3, "pages_per_wallet": 40,
                         "page_size": 50, "map_per_cycle": 40})
    cfg["whale"]["nonsports"].update(ns)
    return cfg


def test_map_markets_roots_a_historical_nonsports_market_from_its_event(tmp_path):
    """C2: a revisit row Gamma answers for but no sport slug can root is
    rooted from its EVENT's tags — the same `whale.nonsports.tags` map, the
    same `market_type` convention and the same `start_utc := the market's own
    endDate` the by-tag universe builder uses."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    cond = "0xhistpol"
    _map_row(con, cond, slug="will-the-democrats-win-the-2028-us-presidential-election")
    con.commit()
    get = _RootGet([_hist_market(cond, "will-the-democrats-win", 33228)],
                   [_hist_event(33228)])

    res = whale_backfill.map_markets(_map_cfg(), con, NOW, get=get)

    row = con.execute("SELECT * FROM markets WHERE condition_id=?", (cond,)).fetchone()
    assert (row["sport"], row["market_type"]) == ("politics", "BINARY")
    assert row["start_utc"] == "2026-11-03T12:00:00+00:00"     # the market's endDate
    assert res["mapped"] == 1 and res["unmappable"] == 0
    # one markets call, one events call — repeated-param `id=`, the only form
    # verified (2026-09-05)
    assert len(get.urls) == 2 and res["calls"] == 2
    assert "/events?id=33228" in get.urls[1]
    assert res["root_calls"] == 1


def test_map_markets_spends_no_event_call_on_a_sports_row(tmp_path):
    """A row the slug prefix already roots never reaches the event lookup, so
    the sports path costs exactly what it did before this amendment."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    gamma = _fixture("whale_gamma_condition_ids_repeated")[0]
    cond = gamma["conditionId"]
    _map_row(con, cond, slug=gamma["slug"])
    con.commit()
    get = _RootGet([gamma], [_hist_event(1)])

    res = whale_backfill.map_markets(_map_cfg(), con, NOW, get=get)

    row = con.execute("SELECT * FROM markets WHERE condition_id=?", (cond,)).fetchone()
    assert row["sport"] == "baseball_kbo" and row["market_type"] == "WINNER"
    assert row["start_utc"] == "2026-08-09T09:00:00+00:00"     # the KICKOFF, as before
    assert not any("/events?" in u for u in get.urls)
    assert res["root_calls"] == 0


def test_the_historical_root_lookup_is_bounded_by_its_own_config_key(tmp_path):
    """`whale.nonsports.root_calls_per_cycle` bounds the EXTRA Gamma calls a
    mapping cycle may spend on event lookups — at most one per chunk of
    `GAMMA_IDS_PER_CALL` condition ids, and never more than the key. It counts
    against the same `max_child_runtime_secs.whale-backfill` budget as every
    other call the child makes."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    conds = [f"0xh{i}" for i in range(40)]
    for cond in conds:
        _map_row(con, cond, slug=f"will-x-{cond}")
    con.commit()
    markets = [_hist_market(c, f"will-x-{c}", 33228 + i) for i, c in enumerate(conds)]
    get = _RootGet(markets, [_hist_event(33228 + i) for i in range(40)])

    res = whale_backfill.map_markets(_map_cfg(root_calls_per_cycle=1), con, NOW, get=get)

    assert res["root_calls"] == 1
    assert sum("/events?" in u for u in get.urls) == 1
    # zero disables the lookup entirely, and the rows stay exactly as they were
    con2 = whale_tape.open_tape(tmp_path / "w2.sqlite")
    _map_row(con2, "0xz", slug="will-x-z")
    con2.commit()
    get2 = _RootGet([_hist_market("0xz", "will-x-z", 33228)], [_hist_event(33228)])
    res2 = whale_backfill.map_markets(_map_cfg(root_calls_per_cycle=0), con2, NOW,
                                      get=get2)
    assert res2["root_calls"] == 0 and res2["unmappable"] == 1
    assert con2.execute("SELECT sport FROM markets WHERE condition_id='0xz'"
                        ).fetchone()["sport"] is None


# ---------- mechanical price series + upsert precedence (fix pass 2, 2026-09-05)

def test_the_mapper_permanently_marks_a_mechanical_price_series_no_root(tmp_path):
    """C1/M5: the up/down cadence series must never be rooted `crypto`, must
    cost NO `events?id=` call, and must take the PERMANENT `no-root:` marker
    (the `absent:` idiom) instead of the daily `unmappable:` churn — 3,669 of
    these rows sit in the live revisit queue and would otherwise spend event
    calls every day forever."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    cond, slug = "0xupdown", "btc-updown-5m-1785140700"
    _map_row(con, cond, slug=slug)
    con.commit()
    market = _hist_market(cond, slug, 750976)
    market["events"] = [{"id": 750976, "slug": slug}]
    get = _RootGet([market], [_hist_event(750976, tags=("crypto",))])

    res = whale_backfill.map_markets(_map_cfg(), con, NOW, get=get)

    assert con.execute("SELECT sport FROM markets WHERE condition_id=?",
                       (cond,)).fetchone()["sport"] is None
    assert not any("/events?" in u for u in get.urls) and res["root_calls"] == 0
    assert whale_tape._meta(con, f"no-root:{cond}") == NOW.strftime("%Y-%m-%d")
    assert whale_tape._meta(con, f"unmappable:{cond}") is None
    # and REVISIT_SQL never offers it again, on any later day
    assert [r[0] for r in con.execute(whale_map.REVISIT_SQL, ("2027-01-01", 10))] == []


def test_a_nonsports_root_wins_over_a_stale_kickoff_and_market_type(tmp_path):
    """M4: the upsert is `COALESCE(existing, excluded)` — existing non-NULL
    wins — which is right for a sports row but wrong for one the rooting
    helper roots non-sports: the pre-4b3bcc9 mapper wrote `gameStartTime` as
    `start_utc` for EVERY row, so a politics row can carry a kickoff. For a
    non-sports root the helper's `endDate` and its LADDER/BINARY are the
    authority."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    cond = "0xstale"
    _map_row(con, cond, slug="will-x-happen", market_type="WINNER",
             start_utc="2026-01-02T03:04:05+00:00")
    con.commit()
    get = _RootGet([_hist_market(cond, "will-x-happen", 33228)], [_hist_event(33228)])

    whale_backfill.map_markets(_map_cfg(), con, NOW, get=get)

    row = con.execute("SELECT * FROM markets WHERE condition_id=?", (cond,)).fetchone()
    assert row["sport"] == "politics" and row["market_type"] == "BINARY"
    assert row["start_utc"] == "2026-11-03T12:00:00+00:00"     # the market's endDate


def test_the_mapper_never_stores_colliding_outcome_tokens(tmp_path):
    """I3: `_gamma_row` zipped clobTokenIds/outcomes with no two-sided guard,
    while the builder has always applied one. Identical labels collide in the
    lane's fingerprints, so the tokens are skipped and the row is reported
    unmappable rather than stored."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    cond = "0xdupe"
    _map_row(con, cond, slug="will-x-happen")
    con.commit()
    market = _hist_market(cond, "will-x-happen", 33228)
    market["outcomes"] = '["Yes", "Yes"]'
    get = _RootGet([market], [_hist_event(33228)])

    res = whale_backfill.map_markets(_map_cfg(), con, NOW, get=get)

    assert con.execute("SELECT tokens_json FROM markets WHERE condition_id=?",
                       (cond,)).fetchone()["tokens_json"] == "[]"
    assert res["unmappable"] == 1 and res["mapped"] == 0


def test_map_markets_reports_its_root_call_count(tmp_path):
    """M5: `root_calls` is part of the result shape `map_markets` returns, not
    a key only `_map_markets` happens to add — the supervisor's call budget is
    read off this dict."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    assert whale_backfill.map_markets(_map_cfg(), con, NOW,
                                      get=_RootGet([], []))["root_calls"] == 0


def test_the_mapper_unroots_a_mechanical_series_row_already_in_the_tape(tmp_path):
    """C1, second half: the exclusion has to reach BACKWARDS. Measured
    read-only on the live tape 2026-09-05, 11 already-rooted rows match the
    registered patterns (10 `crypto`, 1 `politics` — `will-solana-reach-150-in-
    november`, rooted politics off a multi-tagged event), and they would keep
    being scored and priced forever. Un-rooting them clears sport/market_type/
    start_utc and writes the permanent `no-root:` marker; the `markets` UPDATE
    trips the mat triggers, so the affected wallets re-materialize on their
    own. A tag-builder row carries no slug at all — its event slug is the
    `pm:<slug>` in `event_key`, and that is what must be read for it."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    for cond, sport, slug, ekey in (
            ("0xladder", "crypto", "bitcoin-above-65000-on-october-4", None),
            ("0xnested", "politics", "will-solana-reach-150-in-november", None),
            ("0xbytag", "crypto", None, "pm:btc-updown-5m-1785140700"),
            ("0xreal", "politics", "which-party-will-win-the-senate-in-2026", None)):
        con.execute("INSERT INTO markets (condition_id, sport, market_type, start_utc,"
                    " event_key, tokens_json, slug, first_seen, last_seen)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (cond, sport, "BINARY", "2026-10-04T12:00:00+00:00", ekey,
                     '[["t0", "Yes"], ["t1", "No"]]', slug, NOW.isoformat(),
                     NOW.isoformat()))
    con.commit()

    res = whale_backfill.map_markets(_map_cfg(), con, NOW, get=_RootGet([], []))

    rooted = dict(con.execute("SELECT condition_id, sport FROM markets"))
    assert rooted == {"0xladder": None, "0xnested": None, "0xbytag": None,
                      "0xreal": "politics"}
    gone = con.execute("SELECT market_type, start_utc FROM markets "
                       "WHERE condition_id='0xladder'").fetchone()
    assert gone["market_type"] is None and gone["start_utc"] is None
    assert res["unrooted"] == 3
    for cond in ("0xladder", "0xnested", "0xbytag"):
        assert whale_tape._meta(con, f"no-root:{cond}") == NOW.strftime("%Y-%m-%d")
    assert whale_tape._meta(con, "no-root:0xreal") is None
    # and it converges: a second cycle finds nothing left to do
    assert whale_backfill.map_markets(_map_cfg(), con, NOW,
                                      get=_RootGet([], []))["unrooted"] == 0


# ---------- fix pass 3 (review of 235cf42) -----------------------------------

def test_a_nonsports_root_never_clobbers_a_tag_verified_sports_row(tmp_path):
    """I1: M4's `UPDATE markets SET sport, market_type, start_utc` ran
    unconditionally for every id the rooting helper rooted non-sports. A row
    whose SPORT is already known — from its own tape slug, a prefix remap, the
    universe builder — but whose `market_type` is still NULL enters
    REVISIT_SQL, and Gamma's answer for it can carry a slug no prefix roots
    plus a multi-tagged event (`politics` alongside the league tag). M4 then
    rewrote `baseball_mlb` to `politics` and replaced the real KICKOFF with
    the market's endDate, which `first_sight_wins` exists to protect. Only a
    row with no sport at all, or one already carrying THIS non-sports root, is
    the rooting helper's to write."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    cond, kickoff = "0xmlb", "2026-08-09T09:00:00+00:00"
    _map_row(con, cond, sport="baseball_mlb", start_utc=kickoff,   # market_type NULL
             slug="mlb-nyy-bos-2026-08-09")
    con.commit()
    get = _RootGet([_hist_market(cond, "yankees-red-sox-series-winner", 33228)],
                   [_hist_event(33228)])

    whale_backfill.map_markets(_map_cfg(), con, NOW, get=get)

    row = con.execute("SELECT * FROM markets WHERE condition_id=?", (cond,)).fetchone()
    assert row["sport"] == "baseball_mlb", "a tag-verified sports root was clobbered"
    assert row["start_utc"] == kickoff, "first_sight_wins' start_utc was rewritten"


def test_the_no_root_marker_is_counted_on_its_own_and_never_written_when_disabled(tmp_path):
    """Minors, fix pass 3. (a) `no-root:` is PERMANENT and is the non-sports
    half's verdict, so `whale.nonsports.enabled: false` must not write it —
    `unroot_mechanical` already reads the flag and `no_root` did not, which
    left the switch half-off. (b) it gets its own counter: a mechanical row
    is not `unmappable` (Gamma answered fine), and the ops line conflating
    the two hides both numbers. (c) the backoff early return reports the same
    keys as a real pass, so a reader never has to guess whether a missing
    `unrooted` means zero or 'never ran'."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    cond, slug = "0xupdown", "btc-updown-5m-1785140700"
    _map_row(con, cond, slug=slug)
    con.commit()
    market = _hist_market(cond, slug, 750976)
    market["events"] = [{"id": 750976, "slug": slug}]

    off = whale_backfill.map_markets(_map_cfg(enabled=False), con, NOW,
                                     get=_RootGet([market], []))

    assert whale_tape._meta(con, f"no-root:{cond}") is None, "wrote a PERMANENT marker while off"
    assert off["no_root"] == 0
    con2 = whale_tape.open_tape(tmp_path / "w2.sqlite")     # the `unmappable:` day-throttle
    _map_row(con2, cond, slug=slug)                         # above would hide the second pass
    con2.commit()
    res = whale_backfill.map_markets(_map_cfg(), con2, NOW, get=_RootGet([market], []))
    assert res["no_root"] == 1 and res["unmappable"] == 0
    assert whale_tape._meta(con2, f"no-root:{cond}") == NOW.strftime("%Y-%m-%d")
    whale_tape._set_meta(con2, "backoff_until", "2099-01-01T00:00:00+00:00")
    backoff = whale_backfill.map_markets(_map_cfg(), con2, NOW, get=_RootGet([], []))
    assert backoff["status"] == "backoff"
    assert backoff["unrooted"] == 0 and backoff["no_root"] == 0
