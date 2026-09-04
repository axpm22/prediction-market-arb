"""L14 collector. Its OWN SQLite file (rule 9 unreachable) and NO network:
`get_json` is injected, every response comes from a Task-1 fixture, and the
ledger is a three-row in-memory stand-in (the real one is 37 GB)."""
import copy
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from arb.config import load
from arb.venues.base import VenueError
from arb import whale_backfill, whale_tape

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

def _ledger(rows):
    """polls/quotes with the ledger's own columns; rows are quote dicts."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE polls (id INTEGER PRIMARY KEY, started_at TEXT, kind TEXT)")
    con.execute("CREATE TABLE quotes (id INTEGER PRIMARY KEY, poll_id INTEGER, "
                "fetched_at TEXT, event_key TEXT, category TEXT, sport TEXT, venue TEXT, "
                "market_type TEXT, line TEXT, outcome TEXT, side TEXT, start_utc TEXT, "
                "venue_market_id TEXT)")
    con.execute("INSERT INTO polls (id, started_at, kind) VALUES (1, '2026-09-01T00:00:00+00:00', 'watch')")
    con.execute("INSERT INTO polls (id, started_at, kind) VALUES (2, '2026-09-03T16:00:00+00:00', 'watch')")
    for r in rows:
        con.execute(
            "INSERT INTO quotes (poll_id, event_key, category, sport, venue, market_type, "
            "line, outcome, side, start_utc, venue_market_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (r.get("poll_id", 2), r.get("event_key", "ek"), r.get("category", "sports"),
             r.get("sport", "baseball_mlb"), r.get("venue", "polymarket"),
             r.get("market_type", "WINNER"), r.get("line"), r["outcome"], "YES",
             r["start_utc"], r["venue_market_id"]))
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
    got = {m.condition_id for m in whale_tape.universe(led, _cfg(), NOW)}
    assert got == {"0xin"}


def test_universe_carries_the_market_facts_and_every_token():
    led = _ledger([
        _q("0xtot", "tA", "OVER", IN_WINDOW, market_type="TOTAL", line="7.5"),
        _q("0xtot", "tB", "UNDER", IN_WINDOW, market_type="TOTAL", line="7.5"),
    ])
    (m,) = whale_tape.universe(led, _cfg(), NOW)
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
    return [whale_tape.Market(condition_id=c, sport="baseball_mlb", market_type="WINNER",
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
    # the watermark is the OLDEST row fetched, not the newest: everything above
    # it is on the tape, everything below it is the recorded gap
    assert whale_tape._meta(con, "last_seen_ts") == str(oldest)
    assert con.execute("SELECT COUNT(*) FROM prints").fetchone()[0] == 10


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
    mk = [whale_tape.Market(condition_id=c, sport="baseball_kbo", market_type="WINNER",
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
    cfg = _cfg(leaderboard={"enabled": True, "category": "SPORTS", "time_period": "MONTH",
                            "order_by": "PNL", "pages": 1})
    n = whale_backfill.snapshot_leaderboard(cfg, con, NOW, get=get)
    assert "category=SPORTS" in get.urls[0] and "timePeriod=MONTH" in get.urls[0]
    assert "orderBy=PNL" in get.urls[0]
    assert con.execute("SELECT COUNT(*) FROM leaderboard_snapshots").fetchone()[0] == len(board)
    srcs = {r[0] for r in con.execute("SELECT DISTINCT source FROM wallets")}
    assert srcs == {"leaderboard"} and n == len(board)


def test_leaderboard_runs_at_most_once_per_utc_day(tmp_path):
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    cfg = _cfg(leaderboard={"enabled": True, "category": "SPORTS", "time_period": "MONTH",
                            "order_by": "PNL", "pages": 1})
    whale_backfill.snapshot_leaderboard(cfg, con, NOW, get=FakeGet(_fixture("whale_leaderboard")))
    again = FakeGet(_fixture("whale_leaderboard"))
    assert whale_backfill.snapshot_leaderboard(cfg, con, NOW + timedelta(hours=6), get=again) == 0
    assert again.urls == []


def test_a_leaderboard_failure_mid_snapshot_cannot_duplicate_ranks(tmp_path):
    """Page 1 stored, page 2 fails: the day is already claimed, so a retry
    cannot write a second copy of ranks 1..50 under the same snapshot."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    board = _fixture("whale_leaderboard")
    cfg = _cfg(leaderboard={"enabled": True, "category": "SPORTS", "time_period": "MONTH",
                            "order_by": "PNL", "pages": 4})
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
    cfg = _cfg(leaderboard={"enabled": True, "category": "SPORTS", "time_period": "MONTH",
                            "order_by": "PNL", "pages": 4})
    boom = FakeGet(VenueError("503 from https://data-api.polymarket.com/v1/leaderboard"))
    assert whale_backfill.snapshot_leaderboard(cfg, con, NOW, get=boom) == 0
    assert whale_tape._meta(con, "leaderboard_day") is None
    con.execute("DELETE FROM meta WHERE key='backoff_until'")
    con.commit()
    retry = FakeGet(_fixture("whale_leaderboard"))
    n = whale_backfill.snapshot_leaderboard(cfg, con, NOW + timedelta(minutes=5), get=retry)
    assert retry.urls and n == len(_fixture("whale_leaderboard"))


def test_the_rate_bucket_is_one_per_process_not_one_per_call_site(tmp_path):
    """Three call sites, one budget: otherwise a single command bursts 3x the
    configured cap."""
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    cfg = _cfg(leaderboard={"enabled": True, "category": "SPORTS", "time_period": "MONTH",
                            "order_by": "PNL", "pages": 1})
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


def test_backfill_maps_unknown_markets_through_gamma_and_leaves_the_rest_unscored(tmp_path):
    con = whale_tape.open_tape(tmp_path / "w.sqlite")
    _wallet(con, "0xw", "2026-09-01T00:00:00+00:00")
    con.commit()
    gamma = _fixture("whale_gamma_condition_ids_repeated")
    known, other = gamma[0]["conditionId"], gamma[1]["conditionId"]
    acts = [dict(r, conditionId=c) for r, c in
            zip(_fixture("whale_activity")[:2], (known, "0xnowhere"))]
    cfg = _cfg(backfill={"wallets_per_cycle": 1, "pages_per_wallet": 1, "page_size": 50})
    get = FakeGet(acts, [gamma[0]])                # gamma answers for `known` only
    whale_backfill.backfill(cfg, con, NOW, get=get)
    g = [u for u in get.urls if "gamma" in u]
    assert len(g) == 1 and g[0].count("condition_ids=") == 2   # repeated param, not CSV
    rows = {r["condition_id"]: r for r in con.execute("SELECT * FROM markets")}
    assert rows[known]["sport"] == "baseball_kbo"              # kbo- slug prefix
    assert rows[known]["market_type"] == "WINNER"              # sportsMarketType=moneyline
    assert rows[known]["start_utc"] == "2026-08-09T09:00:00+00:00"
    assert json.loads(rows[known]["tokens_json"])[0][1] == "Lotte Giants"
    assert rows[known]["slug"] == "kbo-lot-kt-2026-08-09"      # kept for a LOCAL re-map
    assert rows["0xnowhere"]["sport"] is None                  # unmappable => never scored
    assert other not in rows


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
