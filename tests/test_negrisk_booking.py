"""Phase 2 (D1) — NEGRISK baskets actually book, settle, and derive honest P&L.
The V1-era candidate_from_basket was dead code reading nonexistent keys; this
is the wired path: scan_group -> candidate_from_basket -> process_poll ->
settle_manual -> metrics."""
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from arb.config import load
from arb.metrics import bet_confirmed, bet_pnl_optimistic
from arb.negrisk import scan_group
from arb.settlement import settle_manual
from arb.store import Store
from arb.tracker import candidate_from_basket, process_poll

from tests.test_negrisk import out

CFG = load(Path(__file__).resolve().parent.parent / "config.yaml")
NOW = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)

# the pinned clean NO-basket shape (test_negrisk.test_no_basket_can_be_clean):
# yes mids 0.339 x3 (window ok), NO asks 0.6625 x3 -> S=1.9875 < payout 2
OUTS = [out(f"o{i}", "0.3405", "0.3375", no_ask="0.6625", no_bid="0.6595")
        for i in range(3)]


def _no_basket():
    baskets = scan_group(CFG, "polymarket", "geopolitics", "nonsports:pm:E1",
                         "Who wins?", OUTS, venue_flag=True)
    no = [b for b in baskets if b["side"] == "NO"]
    assert no, "test setup: the NO basket must flag"
    return no[0]


def test_basket_legs_carry_consumer_schema():
    legs = json.loads(_no_basket()["legs_json"])
    for leg in legs:
        assert leg["venue"] == "polymarket"
        assert leg["market_type"] == "WINNER" and leg["line"] is None
        assert leg["outcome"] and leg["side"] == "NO"
        # cost and fee split (capacity/sizing/settle all need both)
        assert Decimal(leg["cost"]) > 0 and Decimal(leg["fee_cost"]) >= 0


def test_basket_books_settles_and_derives_pnl(cfg=None, tmp_path=None):
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    store = Store(tmp / "n.sqlite")
    b = _no_basket()
    poll_id = store.insert("polls", {"started_at": NOW.isoformat(),
                                     "kind": "negrisk", "n_quotes": 6, "n_opps": 1})
    process_poll(CFG, store, poll_id, "negrisk", [candidate_from_basket(b)],
                 {"polymarket": {"ok": True, "n": 6, "latency_ms": 0}}, now=NOW)
    bets = [dict(r) for r in store.query("SELECT * FROM flat_bets")]
    assert len(bets) == 1
    bet = bets[0]
    assert bet["kind"] == "NEGRISK" and bet["category"] == "nonsports"
    assert Decimal(bet["payout"]) == Decimal(2)  # NO basket, N-1
    # resolution-settled: no entry-settle event, waits for the real world
    assert not store.query("SELECT 1 FROM bet_events WHERE kind='settled'")

    # outcome o1 happens: its NO loses, the other two NOs win
    n = settle_manual(CFG, store, "nonsports:pm:E1", "o1",
                      now=NOW.replace(hour=23))
    assert n == 1
    opt = bet_pnl_optimistic(store, bet)
    legs = json.loads(bet["legs_json"])
    costs = [Decimal(l["cost"]) + Decimal(l["fee_cost"]) for l in legs]
    s = sum(costs)
    # per-leg identity == locked NO-basket value: contracts*((N-1) - S)
    assert opt == (Decimal(1) / s * (2 - s)).quantize(Decimal("0.000001"))

    # confirmed P&L uses the stored payout, not payout=1 (review H3)
    store.insert("bet_events", {"bet_id": bet["id"], "kind": "confirmed",
                                "payload_json": json.dumps({"cost_sum": str(s)})})
    ok, pnl = bet_confirmed(store, bet)
    assert ok and pnl == (Decimal(2) / s - 1).quantize(Decimal("0.000001"))


def test_negrisk_quote_rows_persist_every_outcome_side():
    from arb.negrisk import quote_rows
    rows = quote_rows(CFG, "polymarket", "geopolitics", "nonsports:pm:E1",
                      OUTS, NOW)
    # 3 outcomes x YES+NO sides, all with two-sided data in the fixture
    assert len(rows) == 6
    for r in rows:
        assert r["event_key"] == "nonsports:pm:E1"
        assert r["category"] == "nonsports"
        assert r["side"] in ("YES", "NO") and Decimal(r["cost"]) > 0


def test_supervisor_interval_reads_lane_config():
    from arb.heartbeat import lane_interval
    assert lane_interval(CFG, "watch") == float(CFG["watch_interval_secs"])
    assert lane_interval(CFG, "live") == float(CFG["live_lane"]["cadence_secs"])
    assert lane_interval(CFG, "negrisk") == \
        float(CFG["nonsports"]["watch_interval_secs"])
    assert lane_interval(CFG, "political") == \
        float(CFG["nonsports"]["watch_interval_secs"])
    assert lane_interval(CFG, "verify") == float(CFG["verify"]["poll_secs"])


def test_migration_5_payout_column_exists(tmp_path):
    store = Store(tmp_path / "m.sqlite")
    cols = {r[1] for r in store.conn.execute("PRAGMA table_info(flat_bets)")}
    assert "payout" in cols


def test_negrisk_scan_persists_quotes_even_for_one_sided_events(tmp_path, monkeypatch):
    # rule 7: the fetched books are backtest data whether or not the group is
    # scannable — the one-sided skip must not skip persistence
    from types import SimpleNamespace

    from arb.commands import cmd_negrisk_scan
    import arb.venues.polymarket as pm
    ev = {"id": "E9", "negRisk": True, "_tag": "politics", "title": "t",
          "markets": [{"clobTokenIds": json.dumps(["y1", "n1"]),
                       "groupItemTitle": "o0", "volume24hr": 1}]}
    books = {"y1": {"asks": [{"price": "0.40", "size": "10"}], "bids": []},
             "n1": {"asks": [{"price": "0.65", "size": "10"}], "bids": []}}
    import arb.nonsports.kalshi_events as ke
    monkeypatch.setattr(pm, "fetch_negrisk_events", lambda cfg, tags: [ev])
    monkeypatch.setattr(pm, "fetch_books", lambda cfg, tokens: books)
    monkeypatch.setattr(ke, "scan_kalshi_events", lambda cfg, now: ([], [], 0))
    store = Store(tmp_path / "q.sqlite")
    cmd_negrisk_scan(CFG, store, SimpleNamespace(once=True))
    # yes_bid is None (one-sided) -> group unscannable, but the YES/NO asks
    # observed must still be quotes rows
    rows = store.query("SELECT side, cost FROM quotes WHERE event_key='nonsports:pm:E9'")
    assert {(r["side"], r["cost"]) for r in rows} == \
        {("YES", "0.400000"), ("NO", "0.650000")}
    assert store.query("SELECT n_quotes FROM polls")[0]["n_quotes"] == 2


def test_resolution_horizon_gate_lots_far_baskets(tmp_path):
    # D2: nonsports_max_days now bites via resolution_date (Gamma endDate) —
    # the old gate keyed on event_start_utc, which nonsports never has
    from datetime import timedelta
    store = Store(tmp_path / "h.sqlite")
    b = _no_basket()
    far = dict(candidate_from_basket(b),
               resolution_date=(NOW + timedelta(days=60)).isoformat())
    near = dict(candidate_from_basket(b),
                resolution_date=(NOW + timedelta(days=3)).isoformat(),
                fingerprint="fp-near", position_key="pos-near")
    poll_id = store.insert("polls", {"started_at": NOW.isoformat(),
                                     "kind": "negrisk", "n_quotes": 0, "n_opps": 2})
    process_poll(CFG, store, poll_id, "negrisk", [far, near],
                 {"polymarket": {"ok": True, "n": 0, "latency_ms": 0}}, now=NOW)
    bets = store.query("SELECT fingerprint FROM flat_bets")
    assert [r["fingerprint"] for r in bets] == ["fp-near"]
    lot = store.query("SELECT reason FROM left_on_table")
    assert [r["reason"] for r in lot] == ["resolution-horizon"]


def test_negrisk_scan_lots_skipped_events(tmp_path, monkeypatch):
    # M9: silent drops contradicted the bug-E2 doctrine — skips are now LOT rows
    from types import SimpleNamespace

    from arb.commands import cmd_negrisk_scan
    import arb.venues.polymarket as pm
    big = {"id": "BIG", "negRisk": True, "_tag": "politics", "title": "big",
           "markets": [{"clobTokenIds": json.dumps([f"y{i}", f"n{i}"])}
                       for i in range(31)]}  # > max_outcomes 30
    import arb.nonsports.kalshi_events as ke
    monkeypatch.setattr(pm, "fetch_negrisk_events", lambda cfg, tags: [big])
    monkeypatch.setattr(pm, "fetch_books", lambda cfg, tokens: {})
    monkeypatch.setattr(ke, "scan_kalshi_events", lambda cfg, now: ([], [], 0))
    store = Store(tmp_path / "l.sqlite")
    cmd_negrisk_scan(CFG, store, SimpleNamespace(once=True))
    lot = store.query("SELECT reason, event_key FROM left_on_table")
    assert [(r["reason"], r["event_key"]) for r in lot] == \
        [("too-many-outcomes", "nonsports:pm:BIG")]


def test_migration_6_indexes_exist(tmp_path):
    store = Store(tmp_path / "i.sqlite")
    idx = {r[1] for r in store.conn.execute(
        "SELECT * FROM sqlite_master WHERE type='index'").fetchall()}
    for want in ("idx_flat_bets_created", "idx_polls_started",
                 "idx_heartbeats_process", "idx_annotations_target"):
        assert want in idx, want
