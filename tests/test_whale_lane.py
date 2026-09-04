"""L14 Task 8 — the lane cycle (`arb/lanes/whale.py`) and supervisor wiring.

No network and no clock: `get` (prints/resolutions) and `fetch` (books) are
injected, `now` is an argument, the ledger is a tmp `Store` and the tape a tmp
`open_tape` file.

`whale.min_score_lb` is lowered to -0.10 in the `cfg` fixture: with no resolved
positions in a tmp tape every wallet scores the prior (mu = base_rate_default
= -0.02, sd = 0.0616 -> lb80 = -0.0719), so every test below can exercise the
WIRING around the bar without seeding a scoring corpus first.

That crutch masked C1 (final review 2026-09-04) — nothing ever resolved a
backfilled market, so `agg` stayed empty in production too and no wallet could
have qualified at the shipped 0.00 — so
`test_the_lane_books_at_the_shipped_min_score_lb` (bottom of this file) runs
the SHIPPED config against seeded resolved history. The -0.10 tests are kept:
each pins a different piece of wiring, not the bar.
"""
import copy
import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from arb import whale_backfill, whale_tape
from arb.config import load
from arb.lanes import whale_book
from arb.store import Store

ROOT = Path(__file__).resolve().parent.parent

START = datetime(2026, 9, 3, 20, 0, tzinfo=timezone.utc)
NOW = START - timedelta(hours=3)          # 10,800 s to start: clears min_secs_to_start
CID = "0xcond"
YES_TOK, NO_TOK = "0xyes", "0xno"
LABELS = {YES_TOK: "Rays", NO_TOK: "Yankees"}
SLUG = "mlb-tb-nyy-2026-09-03"
EVENT_KEY = f"pm:{SLUG}"
CID2, YES2, NO2 = "0xcond2", "0xyes2", "0xno2"
LABELS2 = {YES2: "Cubs", NO2: "Reds"}
SLUG2 = "mlb-chc-cin-2026-09-03"


@pytest.fixture(autouse=True)
def _fresh_bucket():
    """One token bucket per TEST, or the fake calls pace against the real
    30/min (same guard as tests/test_whale_tape.py)."""
    whale_tape._BUCKET = None
    yield
    whale_tape._BUCKET = None


@pytest.fixture
def cfg():
    c = copy.deepcopy(load(ROOT / "config.yaml"))
    c["whale"]["min_score_lb"] = Decimal("-0.10")   # see the module docstring
    # the exit bar moves with the qualification bar it is measured against:
    # left at the shipped -0.01 every one of these whales sits below it, and a
    # `confidence` exit would fire on the cycle after booking — which would make
    # the flip and no-bid tests below pass without ever exercising a flip.
    c["whale"]["exit"]["min_score_lb"] = Decimal("-0.20")
    return c


# ----------------------------------------------------------------- builders

def _ledger(tmp_path, volume="1000") -> Store:
    """A three-row ledger: one watch poll with both Polymarket tokens, which is
    all `whale_universe.universe` reads (the real one is 37 GB)."""
    store = Store(tmp_path / "arb.sqlite")
    poll_id = store.insert("polls", {
        "started_at": (NOW - timedelta(minutes=5)).isoformat(), "kind": "watch",
        "n_quotes": 2, "n_opps": 0})
    _watch_quotes(store, poll_id, CID, LABELS, volume)
    return store


def _second_market(store, volume="1000"):
    """A second market on the same watch poll: two markets, one cycle."""
    poll_id = store.query("SELECT MAX(id) id FROM polls WHERE kind='watch'")[0]["id"]
    _watch_quotes(store, poll_id, CID2, LABELS2, volume)


def _watch_quotes(store, poll_id, cid, labels, volume):
    for token, label in labels.items():
        store.insert("quotes", {
            "poll_id": poll_id, "fetched_at": (NOW - timedelta(minutes=5)).isoformat(),
            "event_key": "baseball_mlb|2026-09-03|nyy@tb", "category": "sports",
            "sport": "baseball_mlb", "venue": "polymarket", "book": "polymarket",
            "market_type": "WINNER", "line": None, "outcome": label, "side": "YES",
            "raw_price": Decimal("0.5"), "cost": Decimal("0.5"),
            "fee_cost": Decimal("0"), "depth_usd": Decimal("500"),
            "ladder_json": None, "live": False, "bid": Decimal("0.49"),
            "volume_24h": Decimal(volume), "start_utc": START.isoformat(),
            "start_grade": "nominal", "venue_market_id": f"{cid}:{token}"})


def _tape(tmp_path):
    return whale_tape.open_tape(tmp_path / "tape.sqlite")


def _trade(wallet, idx=0, price="0.50", size="1000", age=60, tx=None):
    return {"proxyWallet": wallet, "pseudonym": wallet, "conditionId": CID,
            "asset": (YES_TOK, NO_TOK)[idx], "outcome": ("Rays", "Yankees")[idx],
            "outcomeIndex": idx, "side": "BUY", "price": price, "size": size,
            "timestamp": int((NOW - timedelta(seconds=age)).timestamp()),
            "transactionHash": tx or f"0x{wallet}", "eventSlug": SLUG,
            "slug": f"{SLUG}-tb", "title": "Rays vs Yankees"}


def _second_trades():
    """Three whales on the second market's YES token."""
    return [dict(_trade(f"c{i}", age=60 + 30 * i), conditionId=CID2, asset=YES2,
                 eventSlug=SLUG2, slug=f"{SLUG2}-chc", transactionHash=f"0x2c{i}")
            for i in range(3)]


def _three(price="0.50"):
    """Three whales on YES, prints 30 s apart so the sybil rule is inert."""
    return [_trade(f"w{i}", price=price, age=60 + 30 * i) for i in range(3)]


class Get:
    """Stands in for arb.venues.base.get_json. `/trades` gets the print page;
    anything else (Gamma resolutions) gets an empty body."""

    def __init__(self, rows=()):
        self.rows = list(rows)
        self.urls = []

    def __call__(self, url, cfg, **kw):
        self.urls.append(url)
        return list(self.rows) if "/trades" in url else []


def _book(ask="0.51", bid="0.49", size="1000"):
    return {"asks": [{"price": ask, "size": size}],
            "bids": ([{"price": bid, "size": size}] if bid else [])}


def _fetch(**per_token):
    """fetch_books stand-in. Default book on both tokens; override by token."""
    books = {YES_TOK: _book(), NO_TOK: _book(), YES2: _book(), NO2: _book()}
    books.update(per_token)

    def fetch(cfg, token_ids, deadline=None):
        return {t: books[t] for t in token_ids if t in books}
    return fetch


def _ranked(con, wallets, at=NOW, period="DAY"):
    """A leaderboard snapshot row per wallet — what V4 reads as `ranked`."""
    con.executemany(
        "INSERT INTO leaderboard_snapshots (snapshot_at, category, time_period,"
        " order_by, rank, proxy_wallet, vol, pnl) VALUES (?,?,?,?,?,?,?,?)",
        [(at.isoformat(), "SPORTS", period, "PNL", i + 1, w, "0", "0")
         for i, w in enumerate(wallets)])
    con.commit()


def _run(cfg, store, con, now=NOW, rows=None, fetch=None):
    from arb.lanes.whale import run_whale_cycle
    return run_whale_cycle(cfg, store, con, now, get=Get(rows if rows is not None
                                                         else _three()),
                           fetch=fetch or _fetch())


def _bets(store):
    return [dict(r) for r in store.query("SELECT * FROM flat_bets ORDER BY id")]


def _events(store, kind="settled"):
    return [dict(r) for r in store.query(
        "SELECT * FROM bet_events WHERE kind=? ORDER BY id", (kind,))]


# ------------------------------------------------------------------ booking

def test_one_cycle_books_two_rows_per_fired_variant(cfg, tmp_path):
    """Spec §8: per fired (market, side, variant), two flat_bets rows — flat $1
    and the confidence stake — with distinct fingerprints and one signal_id."""
    store, con = _ledger(tmp_path), _tape(tmp_path)
    res = _run(cfg, store, con)

    assert res["status"] == "ok"
    assert res["prints"] == 3
    bets = _bets(store)
    assert [b["variant"] for b in bets] == ["V1/flat", "V1/conf"]
    assert len({b["fingerprint"] for b in bets}) == 2
    assert len({b["signal_id"] for b in bets}) == 1
    assert bets[0]["signal_id"] is not None
    assert [b["stake_usd"] for b in bets] == ["1.000000", "0.353080"]
    assert all(b["lane"] == "review" and b["poll_kind"] == "whale"
               and b["kind"] == "WHALE" and b["category"] == "sports"
               and b["venue_pair"] == "polymarket:self"
               and b["event_key"] == EVENT_KEY for b in bets)
    assert res["booked"] == 2 and res["fired"] == 1
    (venue,) = store.query("SELECT * FROM poll_venues")
    assert venue["latency_ms"] is not None and venue["latency_ms"] >= 0


def test_the_booked_leg_is_the_touch_the_fire_rule_saw(cfg, tmp_path):
    """§8's leg convention: one PM token, side YES, cost = the best ask."""
    store, con = _ledger(tmp_path), _tape(tmp_path)
    _run(cfg, store, con)

    leg = json.loads(_bets(store)[0]["legs_json"])[0]
    assert leg["venue"] == "polymarket" and leg["side"] == "YES"
    assert leg["outcome"] == "Rays"                       # the token's own label
    assert leg["venue_market_id"] == f"{CID}:{YES_TOK}"
    assert Decimal(leg["cost"]) == Decimal("0.51")
    assert Decimal(leg["fee_cost"]) == Decimal("0.007497")   # 0.03 * .51 * .49
    assert Decimal(leg["depth_usd"]) == Decimal("510")
    assert Decimal(leg["mid0"]) == Decimal("0.5")


def test_quotes_rows_land_for_both_tokens(cfg, tmp_path):
    """Rule 7, and markouts attach only under the bet's own key tuple."""
    store, con = _ledger(tmp_path), _tape(tmp_path)
    res = _run(cfg, store, con)

    rows = [dict(r) for r in store.query(
        "SELECT * FROM quotes q JOIN polls p ON p.id=q.poll_id WHERE p.kind='whale'")]
    assert res["quotes_rows"] == 2
    assert {r["venue_market_id"] for r in rows} == {f"{CID}:{YES_TOK}", f"{CID}:{NO_TOK}"}
    assert all(r["event_key"] == EVENT_KEY and r["category"] == "sports"
               and r["side"] == "YES" and r["sport"] == "baseball_mlb"
               and r["start_grade"] == "nominal" for r in rows)
    leg = json.loads(_bets(store)[0]["legs_json"])[0]
    mine = [r for r in rows if r["venue_market_id"] == leg["venue_market_id"]][0]
    assert (mine["event_key"], mine["market_type"], mine["outcome"], mine["side"]) \
        == (_bets(store)[0]["event_key"], leg["market_type"], leg["outcome"], leg["side"])


def test_every_variant_is_persisted_to_signals_fired_or_not(cfg, tmp_path):
    """§7: suppressed signals are this lane's 'left on the table'."""
    store, con = _ledger(tmp_path), _tape(tmp_path)
    res = _run(cfg, store, con)

    rows = {r["variant"]: dict(r) for r in con.execute("SELECT * FROM signals")}
    assert set(rows) == {"V1", "V2", "V3", "V4"}
    assert rows["V1"]["fired"] == 1 and rows["V1"]["suppressed_reason"] is None
    assert rows["V2"]["fired"] == 0
    assert rows["V2"]["suppressed_reason"] == "uncalibrated"   # min_points null
    assert rows["V1"]["condition_id"] == CID and rows["V1"]["side"] == "0"
    assert Decimal(rows["V1"]["points_yes"]) == 3
    assert len(json.loads(rows["V1"]["whale_ids_json"])) == 3
    assert Decimal(rows["V1"]["touch_ask"]) == Decimal("0.51")
    assert res["signals_evaluated"] == 4
    assert res["suppressed"]["uncalibrated"] == 2
    assert res["suppressed"]["ratio"] == 1          # V4: no wallet is ranked here
    bet = _bets(store)[0]
    assert bet["signal_id"] == rows["V1"]["id"]


def test_a_second_cycle_on_the_same_prints_books_nothing(cfg, tmp_path):
    """Fingerprint dedupe: the re-sighting confirms the bet, never doubles it."""
    store, con = _ledger(tmp_path), _tape(tmp_path)
    _run(cfg, store, con)
    res = _run(cfg, store, con, now=NOW + timedelta(seconds=60))

    assert res["booked"] == 0 and res["exited"] == 0
    assert len(_bets(store)) == 2
    assert len(_events(store, "confirmed")) == 2


def test_a_zombie_book_suppresses_a_signal_that_would_otherwise_fire(cfg, tmp_path):
    """Spec §7.5. volume_24h comes from the ledger's latest watch quotes row;
    the pure signal module knows nothing about zombies."""
    store, con = _ledger(tmp_path, volume="0"), _tape(tmp_path)
    res = _run(cfg, store, con, rows=_three(price="0.93"),
               fetch=_fetch(**{YES_TOK: _book(ask="0.95", bid="0.02"),
                               NO_TOK: _book(ask="0.95", bid="0.02")}))

    row = con.execute("SELECT * FROM signals WHERE variant='V1'").fetchone()
    assert row["fired"] == 0 and row["suppressed_reason"] == "zombie"
    assert res["booked"] == 0 and _bets(store) == []


def test_volumes_come_from_the_same_fallback_poll_the_universe_used(cfg, tmp_path):
    """VOLUME_SQL used to hard-code MAX(id) of watch polls (2026-09-04 fix):
    when the newest watch poll's Polymarket leg is empty, `universe()` already
    falls back one poll for its markets, but volumes stayed on the dead
    newest poll — empty there, so `_zombied` read volume=None (unknown ->
    NOT a zombie) and the zombie guard failed open lane-wide. A newer,
    PM-empty poll must not stop the SAME fallback poll from pricing volumes."""
    store, con = _ledger(tmp_path, volume="0"), _tape(tmp_path)
    store.insert("polls", {"started_at": (NOW - timedelta(minutes=1)).isoformat(),
                           "kind": "watch", "n_quotes": 0, "n_opps": 0})
    res = _run(cfg, store, con, rows=_three(price="0.93"),
               fetch=_fetch(**{YES_TOK: _book(ask="0.95", bid="0.02"),
                               NO_TOK: _book(ask="0.95", bid="0.02")}))

    row = con.execute("SELECT * FROM signals WHERE variant='V1'").fetchone()
    assert row["fired"] == 0 and row["suppressed_reason"] == "zombie"
    assert res["booked"] == 0 and _bets(store) == []


def test_hygiene_excludes_before_scoring_and_signalling(cfg, tmp_path):
    """Task 5 decision: the lane drops excluded positions BEFORE scoring, so a
    washing wallet cannot make up the third vote V1 needs."""
    store, con = _ledger(tmp_path), _tape(tmp_path)
    # the SELL is dust (100 x 0.50 < min_print_usd), so it never reduces the
    # position: without hygiene w2 is still a live third vote, which is what
    # makes this test discriminate. The wash flag reads raw PRINTS.
    wash = [_trade("w2", price="0.50", age=60, tx="0xwash1"),
            dict(_trade("w2", price="0.50", size="100", age=59, tx="0xwash2"),
                 side="SELL")]
    res = _run(cfg, store, con, rows=_three()[:2] + wash)

    assert res["hygiene_excluded"] > 0
    row = con.execute("SELECT * FROM signals WHERE variant='V1'").fetchone()
    assert row["suppressed_reason"] == "min-points"      # 2 whales, floor is 3
    assert _bets(store) == []


def test_calibration_guard_logs_critical_and_books_nothing(cfg, tmp_path, caplog):
    """§7.2: min_points.V2/V3 may be null only inside the calibration window.
    Past it, the lane logs CRITICAL and books nothing for that variant. 'Null
    for the first 14 days' reads literally: 14 distinct days of signals is
    still inside the window, so the guard needs a 15th."""
    store, con = _ledger(tmp_path), _tape(tmp_path)
    for day in range(int(cfg["whale"]["fire"]["calibration_days"]) + 1):
        con.execute("INSERT INTO signals (evaluated_at, condition_id, variant) "
                    "VALUES (?,?,?)",
                    (f"2026-08-{day + 1:02d}T00:00:00+00:00", CID, "V2"))
    con.commit()
    with caplog.at_level(logging.CRITICAL, logger="arb.whale"):
        _run(cfg, store, con)

    assert [r for r in caplog.records if "min_points.V2" in r.getMessage()]
    assert [b["variant"] for b in _bets(store)] == ["V1/flat", "V1/conf"]


def test_disabled_books_nothing_and_records_nothing(cfg, tmp_path):
    store, con = _ledger(tmp_path), _tape(tmp_path)
    cfg["whale"]["enabled"] = False
    res = _run(cfg, store, con)

    assert res["status"] == "disabled"
    assert _bets(store) == []
    assert store.query("SELECT * FROM polls WHERE kind='whale'") == []
    assert con.execute("SELECT COUNT(*) c FROM signals").fetchone()["c"] == 0
    assert con.execute("SELECT COUNT(*) c FROM prints").fetchone()["c"] == 0


def test_an_empty_universe_skips_poll_prints_and_leaves_the_watermark_untouched(cfg, tmp_path):
    """Universe resilience (2026-09-04): when nothing in `universe()`'s own
    lookback window has a Polymarket sports row, the universe is genuinely
    empty this cycle. The lane must not spend a `/trades` call or the
    watermark on it — no store, no `_ledger(tmp_path)` watch poll at all."""
    from arb.lanes.whale import run_whale_cycle
    store, con = Store(tmp_path / "arb.sqlite"), _tape(tmp_path)
    whale_tape._set_meta(con, "last_seen_ts", "12345")
    get = Get(_three())              # would answer /trades if ever asked — must not be
    res = run_whale_cycle(cfg, store, con, NOW, get=get, fetch=_fetch())

    assert res["status"] == "no-universe" and res["markets"] == 0
    assert not any("/trades" in u for u in get.urls)
    assert whale_tape._meta(con, "last_seen_ts") == "12345"


def test_the_cycle_never_backfills(cfg, tmp_path, monkeypatch):
    """Spec §5 step 4: backfill and the leaderboard snapshot are their own
    supervisor process — Task 3 measured 353 calls / 11 min per cycle."""
    store, con = _ledger(tmp_path), _tape(tmp_path)
    for name in ("backfill", "snapshot_leaderboard"):
        monkeypatch.setattr(whale_backfill, name,
                            lambda *a, **k: pytest.fail(f"{name} ran in the 60 s cycle"))
    _run(cfg, store, con)


def test_a_held_market_is_priced_even_when_nothing_qualifies(cfg, tmp_path):
    """§8: markouts attach because the lane writes quotes rows under the bet's
    own key tuple EVERY cycle for a market it holds — including the cycles
    where the prints that fired it have aged out of signal_window_secs."""
    store, con = _ledger(tmp_path), _tape(tmp_path)
    _run(cfg, store, con)
    later = NOW + timedelta(seconds=int(cfg["whale"]["signal_window_secs"]) + 60)
    res = _run(cfg, store, con, now=later)

    assert res["signals_evaluated"] == 0            # every print aged out
    assert res["quotes_rows"] == 2                  # priced anyway: we hold it
    rows = store.query("SELECT q.id FROM quotes q JOIN polls p ON p.id=q.poll_id "
                       "WHERE p.kind='whale'")
    assert len(rows) == 4                           # both tokens, both cycles


def test_a_variant_that_first_fires_on_a_later_cycle_is_still_booked(cfg, tmp_path):
    """§8 (pinned 2026-09-04): `position_key` carries `|<variant>/<sizing>`.
    The tracker dedups active bets by position_key, so a variant whose first
    fire lands on a later cycle than another's would otherwise be absorbed as
    a re-sighting of that other variant and never booked at all."""
    store, con = _ledger(tmp_path), _tape(tmp_path)
    _run(cfg, store, con)                      # V1 only: V2/V3 min_points null
    v1 = _bets(store)
    assert [b["variant"] for b in v1] == ["V1/flat", "V1/conf"]

    cfg["whale"]["fire"]["min_points"]["V2"] = Decimal("0.01")
    res = _run(cfg, store, con, now=NOW + timedelta(seconds=60))

    bets = _bets(store)
    assert res["booked"] == 2
    assert [b["variant"] for b in bets] == ["V1/flat", "V1/conf",
                                            "V2/flat", "V2/conf"]
    assert len({b["position_key"] for b in bets}) == 4
    assert bets[:2] == v1                      # rule 9: V1's rows untouched


def test_every_variant_of_one_cycle_gets_its_own_position_key(cfg, tmp_path):
    """The same-cycle case, which passed only because `process_poll` snapshots
    active positions before its loop: six rows, six distinct position keys."""
    store, con = _ledger(tmp_path), _tape(tmp_path)
    cfg["whale"]["fire"]["min_points"].update(V2=Decimal("0.01"), V3=Decimal("0.01"))
    res = _run(cfg, store, con)

    bets = _bets(store)
    assert res["booked"] == 6 and len(bets) == 6
    assert len({b["position_key"] for b in bets}) == 6
    assert {b["variant"] for b in bets} == {f"{v}/{s}" for v in ("V1", "V2", "V3")
                                            for s in ("flat", "conf")}
    assert all(b["position_key"].endswith("|" + b["variant"]) for b in bets)


def test_the_lane_books_v4_flat_and_conf_for_a_ranked_consensus(cfg, tmp_path):
    """§7/§8 amended 2026-09-04: three ranked qualifying whales fire V4, which
    books its own two rows — distinct fingerprints and position keys, one
    shared signal_id, exactly like every other variant."""
    store, con = _ledger(tmp_path), _tape(tmp_path)
    _ranked(con, ["w0", "w1", "w2"])

    res = _run(cfg, store, con)

    bets = _bets(store)
    assert [b["variant"] for b in bets] == ["V1/flat", "V1/conf", "V4/flat", "V4/conf"]
    v4 = [b for b in bets if b["variant"].startswith("V4")]
    assert len({b["fingerprint"] for b in v4}) == 2
    assert len({b["position_key"] for b in v4}) == 2
    assert len({b["signal_id"] for b in v4}) == 1
    assert all(b["position_key"].endswith("|" + b["variant"]) for b in v4)
    assert res["fired"] == 2 and res["booked"] == 4
    row = con.execute("SELECT * FROM signals WHERE variant='V4'").fetchone()
    assert row["fired"] == 1 and Decimal(row["points_yes"]) == 3
    assert v4[0]["signal_id"] == row["id"]


def test_ranked_wallets_unions_every_window_from_the_latest_snapshot_date(tmp_path):
    """Fix 1 (V4 review of 3a455fd): one daily run stamps each of the four
    leaderboard windows with its OWN `now` (`_snapshot_window`'s per-window
    meta claim), so a retry after a failed window lands the remaining
    windows LATER THE SAME UTC DAY. RANKED_SQL used to key on
    `snapshot_at = MAX(snapshot_at)` — exact match — so that same-day retry
    returned only the retried windows' wallets and silently dropped whoever
    was ranked only in the windows claimed earlier that day. Spec §7: "any
    window" of the latest snapshot DATE, not the latest exact timestamp."""
    con = _tape(tmp_path)
    t1, t2 = NOW.replace(hour=1), NOW.replace(hour=2)   # same UTC day, t1 < t2
    _ranked(con, ["w0"], at=t1, period="DAY")
    _ranked(con, ["w1"], at=t2, period="WEEK")
    _ranked(con, ["w2"], at=t2, period="MONTH")
    _ranked(con, ["w3"], at=t2, period="ALL")
    _ranked(con, ["stale"], at=NOW - timedelta(days=1), period="DAY")  # yesterday: not ranked

    assert whale_book.ranked_wallets(con) == frozenset({"w0", "w1", "w2", "w3"})


def test_only_the_latest_leaderboard_snapshot_ranks_a_wallet(cfg, tmp_path):
    """V4 reads membership in the LATEST snapshot (any window). Yesterday's
    board is not a rank today, so the same three whales score zero points."""
    store, con = _ledger(tmp_path), _tape(tmp_path)
    _ranked(con, ["w0", "w1", "w2"], at=NOW - timedelta(days=1))
    _ranked(con, ["0xsomebody-else"], at=NOW)

    _run(cfg, store, con)

    row = con.execute("SELECT * FROM signals WHERE variant='V4'").fetchone()
    assert Decimal(row["points_yes"]) == 0 and row["fired"] == 0
    assert row["suppressed_reason"] == "ratio"
    assert [b["variant"] for b in _bets(store)] == ["V1/flat", "V1/conf"]


def test_a_v4_exit_reads_the_same_ranked_set_the_cycle_loaded(cfg, tmp_path):
    """§9: the exit recomputes under the bet's own variant, so a V4 bet's flip
    must apply the ranked filter too — the whole opposition here is unranked,
    which flips V1 and leaves V4 held."""
    store, con = _ledger(tmp_path), _tape(tmp_path)
    _ranked(con, ["w0", "w1", "w2"])
    _run(cfg, store, con)                     # V1 x2 and V4 x2 booked
    _flip(con)                                # four UNRANKED whales on NO
    res = _run(cfg, store, con, now=NOW + timedelta(seconds=60))

    evs = _events(store)
    exited = {b["variant"] for b in _bets(store)
              if any(e["bet_id"] == b["id"] for e in evs)}
    assert res["exited"] == 2 and exited == {"V1/flat", "V1/conf"}


def test_one_bad_market_never_costs_the_cycle_its_poll_row(cfg, tmp_path, monkeypatch):
    """Per-market isolation: evaluate/persist/book for ONE market is wrapped, so
    a market the tape or the venue left malformed cannot cost the cycle its
    quotes rows, its polls row, its heartbeat or the other market's bets."""
    from arb import whale_signal
    store, con = _ledger(tmp_path), _tape(tmp_path)
    _second_market(store)
    real = whale_signal.evaluate

    def boom(market, *a, **kw):
        if market["condition_id"] == CID2:
            raise ValueError("unparseable start_utc on the tape")
        return real(market, *a, **kw)
    monkeypatch.setattr(whale_signal, "evaluate", boom)
    res = _run(cfg, store, con, rows=_three() + _second_trades())

    assert res["market_errors"] == 1
    assert res["booked"] == 2                      # the good market still books
    assert {b["event_key"] for b in _bets(store)} == {EVENT_KEY}
    assert res["quotes_rows"] == 4                 # both markets still priced
    assert len(store.query("SELECT * FROM polls WHERE kind='whale'")) == 1
    assert store.query("SELECT * FROM heartbeats WHERE process='whale'")


# -------------------------------------------------------------------- exits

def _flip(con):
    """Four whales on NO: our side's ratio falls to 3/7 = 0.43 < flip_ratio."""
    from arb.whale_rows import _keep
    res = {}
    _keep(con, [_trade(f"n{i}", idx=1, age=60 + 30 * i) for i in range(4)],
          {CID}, NOW, res)


def test_a_flipped_consensus_exits_the_bet_at_the_bid(cfg, tmp_path):
    """§9: one appended `settled` event, never settle_bet (whose future-start
    guard rejects a pregame settlement)."""
    store, con = _ledger(tmp_path), _tape(tmp_path)
    _run(cfg, store, con)
    _flip(con)
    res = _run(cfg, store, con, now=NOW + timedelta(seconds=60))

    assert res["exited"] == 2
    evs = _events(store)
    assert len(evs) == 2
    payload = json.loads(evs[0]["payload_json"])
    assert payload["result"] == "EXITED" and payload["source"] == "exit:flip"
    assert Decimal(payload["exit_price"]) == Decimal("0.49")
    # contracts = stake / (cost + fee) = 1 / 0.517497; pnl = (bid - exit fee) x
    # contracts - stake = (0.49 - 0.007497) x 1.932377... - 1
    assert abs(Decimal(payload["pnl"]) - Decimal("-0.067622")) <= Decimal("0.000002")
    assert Decimal(payload["detail"]["ratio"]) < Decimal("0.5")   # 3/7


def test_a_lifetime_closed_bet_is_still_exited(cfg, tmp_path):
    """§9 (pinned 2026-09-04): for exit purposes an open WHALE bet is one with
    no `settled`/`void` event. `lifetime_closed` means the FLAG left the feed —
    here, that the prints aged past `max_print_age_secs` so nothing re-fired —
    not that the position closed. Excluding it stopped every exit after
    `dead_checks` quiet cycles, which is most of a pregame window."""
    store, con = _ledger(tmp_path), _tape(tmp_path)
    _run(cfg, store, con)
    cycles = int(cfg["tracker"]["dead_checks"]) + 5
    for i in range(1, cycles + 1):                     # no new prints: they hold
        _run(cfg, store, con, now=NOW + timedelta(seconds=60 * i), rows=[])
    assert len(_events(store, "lifetime_closed")) == 2       # both bets, once each
    assert _events(store) == []                              # nothing settled yet

    _flip(con)
    res = _run(cfg, store, con, now=NOW + timedelta(seconds=60 * (cycles + 1)),
               rows=[])

    assert res["exited"] == 2
    evs = _events(store)
    assert len(evs) == 2
    assert json.loads(evs[0]["payload_json"])["source"] == "exit:flip"


def test_an_exit_with_no_bid_annotates_and_appends_no_event(cfg, tmp_path):
    store, con = _ledger(tmp_path), _tape(tmp_path)
    _run(cfg, store, con)
    _flip(con)
    res = _run(cfg, store, con, now=NOW + timedelta(seconds=60),
               fetch=_fetch(**{YES_TOK: _book(bid=None), NO_TOK: _book(bid=None)}))

    assert res["exited"] == 0 and res["exit_no_bid"] == 2
    assert _events(store) == []
    kinds = [r["kind"] for r in store.query(
        "SELECT kind FROM annotations WHERE target_kind='bet'")]
    assert kinds.count("exit-no-bid") == 2
    assert all("venue_ok=True" in r["detail"] for r in store.query(
        "SELECT detail FROM annotations WHERE kind='exit-no-bid'"))


def test_a_venue_outage_is_annotated_as_an_outage_not_a_thin_book(cfg, tmp_path):
    """Same annotation, opposite causes: `venue_ok` is what tells them apart
    when the exit is re-read later (nobody bidding vs nobody answering)."""
    store, con = _ledger(tmp_path), _tape(tmp_path)
    _run(cfg, store, con)
    _flip(con)

    def dead(cfg, token_ids, deadline=None):
        raise RuntimeError("polymarket 503")
    res = _run(cfg, store, con, now=NOW + timedelta(seconds=60), fetch=dead)

    assert res["exited"] == 0 and res["exit_no_bid"] == 2
    assert _events(store) == []
    assert all("venue_ok=False" in r["detail"] for r in store.query(
        "SELECT detail FROM annotations WHERE kind='exit-no-bid'"))


def test_a_bid_thinner_than_the_depth_floor_is_no_bid(cfg, tmp_path):
    store, con = _ledger(tmp_path), _tape(tmp_path)
    _run(cfg, store, con)
    _flip(con)
    thin = _book(bid="0.49", size="10")          # 4.90 < min_executable_usd 50
    res = _run(cfg, store, con, now=NOW + timedelta(seconds=60),
               fetch=_fetch(**{YES_TOK: thin, NO_TOK: thin}))

    assert res["exited"] == 0 and res["exit_no_bid"] == 2
    assert _events(store) == []


def test_no_exit_is_evaluated_once_the_market_has_started(cfg, tmp_path):
    """§9: post-start a bet is held to resolution — in-play prices are game
    state, not information about the whales."""
    store, con = _ledger(tmp_path), _tape(tmp_path)
    _run(cfg, store, con)
    _flip(con)
    res = _run(cfg, store, con, now=START + timedelta(minutes=30))

    assert res["exited"] == 0 and res["exit_no_bid"] == 0
    assert _events(store) == []


def test_an_exited_bet_is_not_exited_twice(cfg, tmp_path):
    store, con = _ledger(tmp_path), _tape(tmp_path)
    _run(cfg, store, con)
    _flip(con)
    _run(cfg, store, con, now=NOW + timedelta(seconds=60))
    res = _run(cfg, store, con, now=NOW + timedelta(seconds=120))

    assert res["exited"] == 0
    assert len(_events(store)) == 2


# ------------------------------------------------------- supervisor wiring

def test_heartbeat_knows_both_whale_processes(cfg):
    from arb.heartbeat import POLL_KIND, _base_lane_interval
    assert _base_lane_interval(cfg, "whale") == float(cfg["whale"]["poll_secs"])
    assert _base_lane_interval(cfg, "whale-backfill") \
        == float(cfg["whale"]["backfill"]["cadence_secs"])
    assert POLL_KIND["whale"] == "whale"
    assert "whale-backfill" not in POLL_KIND      # it writes no polls (like verify)
    # enabled by the operator on 2026-09-04 (Task 11; ToS decision recorded in
    # KNOWN_ISSUES.md #32) — both processes run under the supervisor
    assert cfg["supervisor"]["processes"]["whale"] is True
    assert cfg["supervisor"]["processes"]["whale-backfill"] is True
    assert cfg["supervisor"]["commands"]["whale"] == "whale --once"
    assert cfg["supervisor"]["commands"]["whale-backfill"] == "whale-backfill --once"
    assert cfg["supervisor"]["max_child_runtime_secs"]["whale"] == 120


def test_stale_lanes_recognises_a_silent_whale_lane(cfg, tmp_path):
    from arb.heartbeat import stale_lanes
    import copy
    cfg = copy.deepcopy(cfg)
    cfg["supervisor"]["processes"]["whale"] = True  # the check only covers enabled lanes
    store = Store(tmp_path / "t.sqlite")
    store.insert("polls", {"started_at": (NOW - timedelta(hours=3)).isoformat(),
                           "kind": "whale", "n_quotes": 0, "n_opps": 0})
    stale = dict(stale_lanes(cfg, store, NOW))
    assert "whale" in stale and stale["whale"] > 3 * 3600 - 60


def test_the_cycle_records_its_result_in_the_tape_meta(cfg, tmp_path):
    """Task 9: the daily report reads collector health from the tape, so the
    cycle's own counters (status, rows, bad_rows) have to survive it. `polls`
    carries no column for them and rule 9 forbids inventing one on the ledger."""
    store, con = _ledger(tmp_path), _tape(tmp_path)
    _run(cfg, store, con)

    last = json.loads(whale_tape._meta(con, "last_cycle"))
    assert last["status"] == "ok" and last["prints"] == 3
    assert last["bad_rows"] == 0 and last["booked"] == 2
    assert last["at"] == NOW.isoformat()


def test_last_cycle_meta_survives_a_markout_failure(cfg, tmp_path, monkeypatch):
    """`last_cycle` is written right after the cycle's own work — before
    persist_marks/beat — so a markout failure below cannot leave a stale
    value for the report to read."""
    from arb import markout
    store, con = _ledger(tmp_path), _tape(tmp_path)

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(markout, "persist_marks", _boom)
    with pytest.raises(RuntimeError):
        _run(cfg, store, con)

    last = json.loads(whale_tape._meta(con, "last_cycle"))
    assert last["status"] == "ok" and last["booked"] == 2


# --------------------------------------------- the position-held guard (C2)

def _fresh(now, tag):
    """`_three()`, but printed relative to `now` so the prints are inside
    max_print_age_secs on a later cycle."""
    rows = []
    for i in range(3):
        r = _trade(f"w{i}", age=60 + 30 * i, tx=f"0x{tag}{i}")
        r["timestamp"] = int((now - timedelta(seconds=60 + 30 * i)).timestamp())
        rows.append(r)
    return rows


def test_a_held_position_is_never_re_booked_after_its_lifetime_closed(cfg, tmp_path):
    """§9 (final review 2026-09-04): the tracker's dedupe only sees ACTIVE bets,
    and `lifetime_closed` drops a WHALE bet from that set after `dead_checks`
    quiet cycles — while we still hold it. Fresh prints then re-book the same
    variant/sizing on top of the open one."""
    store, con = _ledger(tmp_path), _tape(tmp_path)
    _run(cfg, store, con)
    assert len(_bets(store)) == 2
    cycles = int(cfg["tracker"]["dead_checks"]) + 5
    for i in range(1, cycles + 1):
        _run(cfg, store, con, now=NOW + timedelta(seconds=60 * i), rows=[])
    assert len(_events(store, "lifetime_closed")) == 2
    assert _events(store) == []                       # still held: nothing settled

    later = NOW + timedelta(seconds=60 * (cycles + 1))
    res = _run(cfg, store, con, now=later, rows=_fresh(later, "b"))

    assert res["fired"] == 1                          # the signal still fires ...
    assert res["booked"] == 0                         # ... but we already hold it
    assert len(_bets(store)) == 2
    lots = [dict(r) for r in store.query("SELECT * FROM left_on_table ORDER BY id")]
    assert [r["reason"] for r in lots] == ["position-held", "position-held"]
    assert len({r["fingerprint"] for r in lots}) == 2   # flat and conf, one each


# ------------------------------------------- the SHIPPED bar (final review C1)

def _resolved_history(con, wallets, n=400, edge="0.08"):
    """tests/test_whale_score.py's `_positions` seeding, as TAPE rows: one
    resolved market per position, bought pregame at `1 - edge` and paid out, so
    the wallet's realized edge is exactly `edge` at every level of the
    hierarchy. This is the history the resolution sweep (spec §5 step 5) is
    there to accumulate."""
    price = Decimal(1) - Decimal(edge)
    for w in wallets:
        for i in range(n):
            cid, start = f"0xh{w}{i}", NOW - timedelta(days=2)
            con.execute(
                "INSERT INTO markets (condition_id, sport, market_type, start_utc,"
                " tokens_json, first_seen, last_seen) VALUES (?,?,?,?,?,?,?)",
                (cid, "baseball_mlb", "WINNER", start.isoformat(), "[]",
                 start.isoformat(), start.isoformat()))
            con.execute(
                "INSERT INTO prints (observed_at, ts, tx_hash, proxy_wallet, pseudonym,"
                " condition_id, asset, outcome, outcome_index, side, price, size,"
                " notional_usd, event_slug, slug, title, source, raw_json)"
                " VALUES (?,?,?,?,?,?,?,?,0,'BUY',?,?,?,?,?,?,'tape','{}')",
                (start.isoformat(), int(start.timestamp()) - 3600, f"0x{w}{i}", w, w,
                 cid, "tok", "Yes", str(price), "1000", str(price * 1000), "e", "s", "t"))
            con.execute(
                "INSERT INTO resolutions (condition_id, resolved_at, winner_outcome_index,"
                " status, source, raw_json, fetched_at) VALUES (?,?,0,'resolved','gamma','{}',?)",
                (cid, (NOW - timedelta(days=1)).isoformat(), NOW.isoformat()))
    con.commit()


def test_the_lane_books_at_the_shipped_min_score_lb(tmp_path):
    """The -0.10 bar every other test in this file uses is a CRUTCH: with an
    empty tape every wallet scores the prior (lb80 = -0.0719) and nothing could
    clear the shipped 0.00. That masked C1 — no resolution sweep meant `agg`
    stayed empty in production too, so no wallet could ever qualify and the
    lane would have booked nothing forever. This one runs the shipped config
    against real resolved history."""
    cfg = copy.deepcopy(load(ROOT / "config.yaml"))
    assert cfg["whale"]["min_score_lb"] == Decimal("0.0")        # the shipped bar
    store, con = _ledger(tmp_path), _tape(tmp_path)
    _resolved_history(con, [f"w{i}" for i in range(3)])

    res = _run(cfg, store, con)

    assert res["fired"] == 1 and res["booked"] == 2
    assert [b["variant"] for b in _bets(store)] == ["V1/flat", "V1/conf"]


def test_the_cycle_records_its_own_wall_duration(cfg, tmp_path):
    """I5(c): the supervisor kills a `whale` child at 120 s (config
    `max_child_runtime_secs`) and nothing measured how close a cycle ran to
    it. `last_cycle` carries the wall time the report and the panel show."""
    store, con = _ledger(tmp_path), _tape(tmp_path)
    _run(cfg, store, con)

    last = json.loads(whale_tape._meta(con, "last_cycle"))
    assert 0 <= last["duration_secs"] < float(
        cfg["supervisor"]["max_child_runtime_secs"]["whale"])


def test_an_outage_annotates_exit_no_bid_once_per_bet_not_once_per_cycle(cfg, tmp_path):
    """Minor (final review): during a Polymarket outage every cycle appended an
    identical `exit-no-bid` annotation per bet — 60 a minute a bet, forever."""
    store, con = _ledger(tmp_path), _tape(tmp_path)
    _run(cfg, store, con)
    _flip(con)
    dead = _fetch(**{YES_TOK: _book(bid=None), NO_TOK: _book(bid=None)})
    for i in (1, 2, 3):
        res = _run(cfg, store, con, now=NOW + timedelta(seconds=60 * i), fetch=dead)

    assert res["exit_no_bid"] == 2                 # still counted every cycle
    assert len(store.query("SELECT 1 FROM annotations WHERE kind='exit-no-bid'")) == 2


def test_a_market_whose_tokens_collide_is_counted_in_the_cycle(cfg, tmp_path):
    """I2's diagnostics reach `meta.last_cycle`, so a venue shape the universe
    refuses is visible in the report instead of being a silently short list."""
    store, con = _ledger(tmp_path), _tape(tmp_path)
    store.query("SELECT 1")
    for token in LABELS:                    # both tokens under one label + side
        store.insert("quotes", {
            "poll_id": store.query("SELECT MAX(id) id FROM polls")[0]["id"],
            "fetched_at": NOW.isoformat(), "event_key": "k", "category": "sports",
            "sport": "baseball_mlb", "venue": "polymarket", "book": "polymarket",
            "market_type": "WINNER", "line": None, "outcome": "Same", "side": "YES",
            "raw_price": Decimal("0.5"), "cost": Decimal("0.5"),
            "fee_cost": Decimal("0"), "depth_usd": Decimal("500"),
            "ladder_json": None, "live": False, "bid": Decimal("0.49"),
            "volume_24h": Decimal("1000"), "start_utc": START.isoformat(),
            "start_grade": "nominal", "venue_market_id": f"0xdup:{token}"})

    res = _run(cfg, store, con)

    assert res["label_collision"] == 1 and res["markets"] == 1     # ours, not 0xdup
    assert json.loads(whale_tape._meta(con, "last_cycle"))["label_collision"] == 1
