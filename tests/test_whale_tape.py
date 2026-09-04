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
from arb import whale_backfill, whale_map, whale_resolve, whale_sweep, whale_tape, whale_universe

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


def test_global_mode_makes_one_marketless_call_and_keeps_only_universe_rows(tmp_path):
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    mk = _global_markets(1)
    get = FakeGet(_fixture("whale_trades_global"))
    res = whale_tape.poll_prints(_cfg(page_size=10000), con, mk, NOW, get=get)
    assert len(get.urls) == 1 and "market=" not in get.urls[0]
    kept = con.execute("SELECT COUNT(*), COUNT(DISTINCT condition_id) FROM prints").fetchone()
    assert kept[1] == 1 and kept[0] == 70          # the fixture's busiest market
    assert res["rows"] == 70 and res["calls"] == 1 and res["status"] == "ok"
    row = con.execute("SELECT * FROM prints LIMIT 1").fetchone()
    assert row["observed_at"] == NOW.isoformat() and row["source"] == "tape"


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
    assert res["rows"] == len(ours) - 2
    assert con.execute("SELECT COUNT(*) FROM prints").fetchone()[0] == len(ours) - 2
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


# --- backfill ---------------------------------------------------------------

def _wallet(con, w, first_seen):
    con.execute("INSERT INTO wallets (proxy_wallet, pseudonym, first_seen, source) "
                "VALUES (?,?,?,'tape')", (w, "p", first_seen))


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
    _wallet(con, "0xw", "2026-09-01T00:00:00+00:00")
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


def test_backfill_is_fifo_by_first_seen_and_respects_wallets_per_cycle(tmp_path):
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    _wallet(con, "0xthird", "2026-09-03T00:00:00+00:00")
    _wallet(con, "0xfirst", "2026-09-01T00:00:00+00:00")
    _wallet(con, "0xsecond", "2026-09-02T00:00:00+00:00")
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
    _wallet(con, "0xw", "2026-09-01T00:00:00+00:00")
    con.commit()
    gamma = _fixture("whale_gamma_condition_ids_repeated")
    known, other = gamma[0]["conditionId"], gamma[1]["conditionId"]
    acts = [dict(r, conditionId=c) for r, c in
            zip(_fixture("whale_activity")[:2], (known, "0xnowhere"))]
    cfg = _cfg(backfill={"wallets_per_cycle": 1, "pages_per_wallet": 1, "page_size": 50,
                         "map_per_cycle": 40})
    whale_backfill.backfill(cfg, con, NOW, get=FakeGet(acts))
    assert con.execute("SELECT COUNT(*) FROM markets").fetchone()[0] == 0  # not mapped yet

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
    con.commit()
    cfg = _cfg(backfill={"wallets_per_cycle": 3, "pages_per_wallet": 40,
                         "page_size": 50, "map_per_cycle": 2})
    get = FakeGet([])                                 # one call covers both (2 ids/call)

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


def test_map_markets_is_bounded_and_fifo_by_first_print(tmp_path):
    """Fix 1's actual bound: three markets never seen before, `map_per_cycle`
    covers only two, oldest first print first; the third waits."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    for i, age in enumerate((100, 300, 200)):        # insertion order != age
        _print(con, "0xw", f"0xm{i}", "BUY", "900", int(NOW.timestamp()) - age)
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
    _wallet(con, "0xw", "2026-09-01T00:00:00+00:00")
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
    _wallet(con, "0xw", "2026-09-01T00:00:00+00:00")
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
        "calls": 0, "mapped": 0, "unmappable": 0, "status_counts": {}, "status": "ok"})

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
