"""H3 — doubleheader settlement. Two same-pair finals in one day must not
cross-settle: settle by gamePk when known, refuse + annotate when ambiguous."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from arb.config import load
from arb.settlement import settle_auto_mlb
from arb.store import Store

ROOT = Path(__file__).resolve().parent.parent
T1 = datetime(2026, 7, 8, 21, 0, tzinfo=timezone.utc)   # game 1, 17:00 ET
T2 = datetime(2026, 7, 9, 0, 0, tzinfo=timezone.utc)    # game 2, 20:00 ET (3h later)
NOW = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)  # well past both + grace


@pytest.fixture
def cfg():
    return load(ROOT / "config.yaml")


def _final(gamepk, gameno, start, home_score, away_score):
    return {"gamePk": gamepk, "gameNumber": gameno, "home": "mets",
            "away": "royals", "start_utc": start,
            "home_score": home_score, "away_score": away_score}


# game 1: mets win (5-3); game 2: royals win (2-6)
FINALS = [_final(1, 1, T1, 5, 3), _final(2, 2, T2, 2, 6)]


def _bet(store, dh, start, outcome, gamepk=None):
    row = {"created_at": datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc).isoformat(),
           "kind": "EV", "category": "sports", "sport": "baseball_mlb",
           "event_key": f"baseball_mlb:2026-07-08:mets:royals:dh{dh}",
           "event_start_utc": start.isoformat(), "lane": "review", "trusted": 0,
           "stake_usd": "1", "status": "OPEN",
           "legs_json": json.dumps([{"venue": "kalshi", "market_type": "WINNER",
                                     "outcome": outcome, "side": "YES",
                                     "cost": "0.50", "fee_cost": "0"}])}
    if gamepk is not None:
        row["gamepk"] = gamepk
    return store.insert("flat_bets", row)


def _patch_mlb(monkeypatch):
    import arb.venues.mlbstats as m
    monkeypatch.setattr(m, "fetch_schedule", lambda cfg, d: FINALS)
    monkeypatch.setattr(m, "is_final", lambda g: True)


def _settled(store, bet_id):
    rows = store.query("SELECT payload_json FROM bet_events WHERE bet_id=? "
                       "AND kind='settled'", (bet_id,))
    return json.loads(rows[0]["payload_json"]) if rows else None


def test_doubleheader_settles_by_gamepk(cfg, tmp_path, monkeypatch):
    _patch_mlb(monkeypatch)
    store = Store(tmp_path / "dh.sqlite")
    b1 = _bet(store, 1, T1, "mets", gamepk=1)    # game 1 -> mets win -> WON
    b2 = _bet(store, 2, T2, "royals", gamepk=2)  # game 2 -> royals win -> WON
    n = settle_auto_mlb(cfg, store, now=NOW)
    assert n == 2
    assert _settled(store, b1)["per_leg"] == ["WON"]
    assert _settled(store, b2)["per_leg"] == ["WON"]


def test_doubleheader_without_gamepk_refuses_and_annotates(cfg, tmp_path, monkeypatch):
    _patch_mlb(monkeypatch)
    store = Store(tmp_path / "dh2.sqlite")
    b1 = _bet(store, 1, T1, "mets")     # no gamePk: two finals in 6h window
    b2 = _bet(store, 2, T2, "royals")   # -> ambiguous, must NOT settle either
    n = settle_auto_mlb(cfg, store, now=NOW)
    assert n == 0
    assert _settled(store, b1) is None and _settled(store, b2) is None
    for bid in (b1, b2):
        ann = store.query("SELECT kind FROM annotations WHERE target_kind='bet' "
                          "AND target_id=? AND kind='ambiguous-doubleheader'",
                          (str(bid),))
        assert ann, f"bet {bid} missing ambiguous-doubleheader annotation"


def test_partial_settlement_writes_honest_per_leg(cfg, tmp_path):
    # winner="PARTIAL" used to run through _leg_result, recording every YES leg
    # LOST — fiction that reconcile then treated as truth (false demotions)
    from arb.settlement import settle_manual
    store = Store(tmp_path / "pl.sqlite")
    _bet(store, 1, T1, "mets")
    n = settle_manual(cfg, store, "baseball_mlb:2026-07-08:mets:royals:dh1",
                      "PARTIAL:0.4", now=NOW)
    assert n == 1
    payload = json.loads(store.query(
        "SELECT payload_json FROM bet_events WHERE kind='settled'")[0]["payload_json"])
    assert payload["fraction"] == "0.4"
    assert payload["per_leg"] == ["PARTIAL"]


def test_settle_manual_warns_on_unknown_winner_token(cfg, tmp_path, caplog):
    # M6: 'a typo is forever' — a winner token matching no leg outcome across
    # the event's bets at least screams before settling
    import logging

    from arb.settlement import settle_manual
    store = Store(tmp_path / "w.sqlite")
    _bet(store, 1, T1, "mets")
    with caplog.at_level(logging.WARNING):
        n = settle_manual(cfg, store, "baseball_mlb:2026-07-08:mets:royals:dh1",
                          "Mets", now=NOW)  # capital-M typo: matches nothing
    assert n == 1  # still settles (single-leg opponents are legitimate)...
    assert any("matches no leg outcome" in r.getMessage() for r in caplog.records)
