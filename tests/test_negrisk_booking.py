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
