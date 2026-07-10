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
