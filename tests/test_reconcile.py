"""H4 — reconcile must re-attempt unresolved legs across passes, and a
settlement mismatch must actually demote the pair out of the trusted path."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from arb import reconcile as R
from arb.config import load
from arb.store import Store

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def cfg():
    return load(ROOT / "config.yaml")


def _settled_bet(store, venue_pair, per_leg, venues):
    legs = [{"venue": v, "venue_market_id": f"{v}-1", "venue_side": "YES",
             "cost": "0.5"} for v in venues]
    bid = store.insert("flat_bets", {
        "kind": "ARB", "venue_pair": venue_pair, "legs_json": json.dumps(legs),
        "stake_usd": "1", "status": "OPEN"})
    store.insert("bet_events", {"bet_id": bid, "kind": "settled",
                                "payload_json": json.dumps({"per_leg": per_leg,
                                                            "result": per_leg})})
    return bid


def _reconciled_verdicts(store, bid):
    return [json.loads(e["payload_json"])["verdicts"] for e in store.query(
        "SELECT payload_json FROM bet_events WHERE bet_id=? AND kind='reconciled' "
        "ORDER BY id", (bid,))]


def test_reconcile_reattempts_unresolved_leg(cfg, tmp_path, monkeypatch):
    store = Store(tmp_path / "r.sqlite")
    bid = _settled_bet(store, "kalshi:polymarket", ["WON", "WON"],
                       ["kalshi", "polymarket"])
    # pass 1: kalshi resolves, polymarket still pending
    monkeypatch.setattr(R, "RESOLVERS",
                        {"kalshi": lambda c, l: "WON", "polymarket": lambda c, l: None})
    R.reconcile(cfg, store, now=NOW)
    # pass 2: polymarket now resolves — the second leg MUST be re-attempted
    monkeypatch.setattr(R, "RESOLVERS",
                        {"kalshi": lambda c, l: "WON", "polymarket": lambda c, l: "WON"})
    R.reconcile(cfg, store, now=NOW)
    events = _reconciled_verdicts(store, bid)
    covered = {i for ev in events for i, v in enumerate(ev) if v is not None}
    assert covered == {0, 1}, f"leg 1 never re-reconciled: {events}"


def test_mismatch_demotes_pair_and_blocks_trust(cfg, tmp_path, monkeypatch):
    from arb.tracker import booking_trusted
    cfg["trusted_pairs"] = ["kalshi:polymarket"]
    store = Store(tmp_path / "d.sqlite")
    assert booking_trusted(cfg, store, "kalshi:polymarket") is True  # control
    bid = _settled_bet(store, "kalshi:polymarket", ["WON", "WON"],
                       ["kalshi", "polymarket"])
    # kalshi venue says LOST -> mismatch vs the scores' WON
    monkeypatch.setattr(R, "RESOLVERS",
                        {"kalshi": lambda c, l: "LOST", "polymarket": lambda c, l: "WON"})
    R.reconcile(cfg, store, now=NOW)
    assert store.query("SELECT * FROM pair_demotions WHERE pair=?",
                       ("kalshi:polymarket",)), "no pair_demotions row written"
    # the next booking on the demoted pair must NOT be trusted
    assert booking_trusted(cfg, store, "kalshi:polymarket") is False


def test_partial_settled_bet_never_mismatch_demotes(cfg, tmp_path, monkeypatch):
    # venue verdicts are binary; a fraction-settled bet can't evidence a bad
    # pair — the old code compared venue "WON" against garbage per_leg and
    # permanently demoted the pair off fiction
    store = Store(tmp_path / "pf.sqlite")
    legs = [{"venue": "kalshi", "venue_market_id": "k-1", "venue_side": "YES",
             "cost": "0.5"}]
    bid = store.insert("flat_bets", {
        "kind": "EV", "venue_pair": "kalshi:self", "legs_json": json.dumps(legs),
        "stake_usd": "1", "status": "OPEN"})
    store.insert("bet_events", {"bet_id": bid, "kind": "settled",
                                "payload_json": json.dumps(
                                    {"result": ["PARTIAL"], "per_leg": ["PARTIAL"],
                                     "winner": "PARTIAL", "fraction": "0.4"})})
    monkeypatch.setattr(R, "RESOLVERS", {"kalshi": lambda c, l: "WON"})
    R.reconcile(cfg, store, now=NOW)
    assert not store.query("SELECT * FROM pair_demotions")
    assert not store.query(
        "SELECT * FROM annotations WHERE kind='settlement_mismatch'")


def test_reconcile_gives_up_after_settlement_lag_plus_grace(cfg, tmp_path, monkeypatch):
    # D5: SX legs (resolver always None) were re-fetched every pass forever.
    # Past venue_settlement_lag_hours + reconcile_give_up_days the leg records
    # UNRESOLVABLE (covered) and is never attempted again.
    store = Store(tmp_path / "g.sqlite")
    legs = [{"venue": "sx", "venue_market_id": "s-1", "venue_side": "YES",
             "cost": "0.5"}]
    bid = store.insert("flat_bets", {
        "kind": "EV", "venue_pair": "sx:self", "legs_json": json.dumps(legs),
        "stake_usd": "1", "status": "OPEN"})
    settled_at = datetime(2026, 6, 1, tzinfo=timezone.utc)  # 38 days before NOW
    store.insert("bet_events", {"bet_id": bid, "kind": "settled",
                                "created_at": settled_at.isoformat(),
                                "payload_json": json.dumps({"per_leg": ["WON"],
                                                            "result": ["WON"]})})
    calls = {"n": 0}

    def counting_sx(c, l):
        calls["n"] += 1
        return None

    monkeypatch.setattr(R, "RESOLVERS", {"sx": counting_sx})
    NOW2 = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)
    assert R.reconcile(cfg, store, now=NOW2) == 1
    assert calls["n"] == 1
    events = _reconciled_verdicts(store, bid)
    assert events == [["UNRESOLVABLE"]]
    # no mismatch fired off the sentinel, and the next pass never re-fetches
    assert not store.query("SELECT 1 FROM pair_demotions")
    R.reconcile(cfg, store, now=NOW2)
    assert calls["n"] == 1


def test_reconcile_does_not_give_up_inside_the_lag(cfg, tmp_path, monkeypatch):
    store = Store(tmp_path / "f.sqlite")
    legs = [{"venue": "polymarket", "venue_market_id": "0xa:1",
             "venue_side": "YES", "cost": "0.5"}]
    bid = store.insert("flat_bets", {
        "kind": "EV", "venue_pair": "polymarket:self",
        "legs_json": json.dumps(legs), "stake_usd": "1", "status": "OPEN"})
    store.insert("bet_events", {"bet_id": bid, "kind": "settled",
                                "created_at": NOW.isoformat(),
                                "payload_json": json.dumps({"per_leg": ["WON"],
                                                            "result": ["WON"]})})
    monkeypatch.setattr(R, "RESOLVERS", {"polymarket": lambda c, l: None})
    R.reconcile(cfg, store, now=NOW)  # inside PM's 48h lag: stays provisional
    assert _reconciled_verdicts(store, bid) == []


def test_polymarket_resolver_asks_gamma_for_CLOSED_markets(cfg, monkeypatch):
    """Gamma's /markets?condition_ids= defaults to closed=false, so it filters
    out precisely the resolved markets this resolver exists to read. Observed
    2026-07-27: the same condition_id returns 0 rows without `closed=true` and
    1 row with it — which is why 41 Polymarket legs across 49 kalshi:polymarket
    pairs never graded, and why the L2 settlement-agreement audit had no input.
    """
    import arb.venues.base as base
    seen = {}

    def fake_get_json(url, cfg, **kw):
        seen["url"] = url
        if "closed=true" not in url:
            return []          # what gamma actually does without the flag
        return [{"closed": True,
                 "outcomePrices": '["1", "0"]',
                 "outcomes": '["Mets", "Phillies"]',
                 "clobTokenIds": '["tokA", "tokB"]'}]

    monkeypatch.setattr(base, "get_json", fake_get_json)
    leg = {"venue": "polymarket", "venue_market_id": "0xabc:tokA", "venue_side": "YES"}
    assert R._polymarket(cfg, leg) == "WON"
    assert "closed=true" in seen["url"]
