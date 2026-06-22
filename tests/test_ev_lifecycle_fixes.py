import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from arb.cli import detect_ev_from_consensus
from arb.config import load_config
from arb.experimental.ev_exit import exit_decision
from arb.models import Quote
from arb.store import Store
from arb.tracker import _bet_records_for_date, _record_pnl

EVENT = "baseball_mlb:2026-06-12:kansas city royals:texas rangers"


def q(venue: str, cost: str) -> Quote:
    c = Decimal(cost)
    return Quote(event_key=EVENT, venue=venue, book=f"{venue}-KC", market_type="WINNER", line=None,
                 outcome_label="kansas city royals", raw_price=c, cost=c, fee_cost=Decimal("0"),
                 depth_usd=Decimal("500"), live=False, fetched_at=datetime.now(timezone.utc),
                 start_utc=datetime.now(timezone.utc) + timedelta(hours=4))


# ---- #2 one EV bet per opinion (cheapest venue) ----

def test_two_venues_same_outcome_yield_one_ev() -> None:
    consensus = {(EVENT, "kansas city royals"): (Decimal("0.55"), datetime.now(timezone.utc))}
    opps = detect_ev_from_consensus([q("kalshi", "0.50"), q("polymarket", "0.48")],
                                    consensus, 8, set(), e_min=Decimal("0.01"))
    assert len(opps) == 1
    assert opps[0].legs[0].venue == "polymarket"   # cheapest effective cost wins


# ---- #3 stale pregame consensus is quarantined ----

def test_stale_pregame_consensus_quarantined() -> None:
    old = datetime.now(timezone.utc) - timedelta(hours=5)   # > 3h pregame window
    consensus = {(EVENT, "kansas city royals"): (Decimal("0.55"), old)}
    [opp] = detect_ev_from_consensus([q("kalshi", "0.50")], consensus, 8, set(),
                                     e_min=Decimal("0.01"), config=load_config(),
                                     pregame_max_age_hours=3)
    assert opp.quality_flag == "stale-consensus"
    assert "stale-consensus" in opp.notes


def test_fresh_pregame_consensus_is_clean() -> None:
    fresh = datetime.now(timezone.utc) - timedelta(hours=1)  # within 3h
    consensus = {(EVENT, "kansas city royals"): (Decimal("0.55"), fresh)}
    [opp] = detect_ev_from_consensus([q("kalshi", "0.50")], consensus, 8, set(),
                                     e_min=Decimal("0.01"), config=load_config(),
                                     pregame_max_age_hours=3)
    assert opp.quality_flag is None


# ---- #1 EXITED is a terminal realized state in the report ----

def _insert(con, **kw) -> int:
    cols = ",".join(kw)
    cur = con.execute(f"INSERT INTO flat_bets({cols}) VALUES ({','.join('?' for _ in kw)})", tuple(kw.values()))
    return int(cur.lastrowid)


def test_exited_bet_is_terminal_realized(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    legs = json.dumps([{"venue": "kalshi", "cost": "0.50"}])
    with store.connect() as con:
        base = _insert(con, fingerprint="fp1", opened_poll_id=1, category="sports", sport="baseball_mlb",
                       kind="EV", trusted=1, live=0, edge="0.03", edge_points="0.05", stake_usd="1.00",
                       legs_json=legs, optimistic_cost="0.50", status="OPEN",
                       placed_at=datetime.now(timezone.utc).isoformat(), notes="")
        _insert(con, fingerprint="fp1", reappearance_of=base, opened_poll_id=1, category="sports",
                sport="baseball_mlb", kind="EV", trusted=1, live=0, edge="0.03", stake_usd="1.00",
                legs_json=legs, optimistic_cost="0.50", pnl_optimistic="0.07", pnl_confirmed="0.07",
                status="EXITED", result="SOLD", placed_at=datetime.now(timezone.utc).isoformat(),
                settled_at=datetime.now(timezone.utc).isoformat(), notes="ev-exit: take-profit")
    [record] = _bet_records_for_date(store, None)
    assert record["settled"] is not None                 # EXITED counts as terminal
    assert _record_pnl(record, "optimistic") == Decimal("0.07")  # take-profit P&L is booked


# ---- #6 symmetric stop-loss ----

def _bet(legs):
    return {"legs_json": json.dumps(legs), "confirmed_entry_cost": None, "optimistic_cost": "0.50",
            "stake_usd": "1.00"}


def _cfg(**over):
    base = {"min_profit_usd": "0.05", "min_profit_roi": "0.03", "exit_edge_ceiling": "0.01",
            "min_exit_depth_usd": "5", "require_fair_value": True,
            "stop_loss_enabled": True, "stop_loss_roi": "-0.15", "stop_edge_floor": "0.0"}
    base.update(over)
    return base


def test_stop_loss_fires_when_losing_and_edge_gone() -> None:
    now = datetime.now(timezone.utc)
    leg = {"venue": "kalshi", "market_type": "WINNER", "outcome_label": "kansas city royals",
           "line": None, "event_key": EVENT, "start_utc": (now + timedelta(hours=4)).isoformat()}
    consensus = {(EVENT, "kansas city royals"): (Decimal("0.38"), now)}  # edge gone (< bid)
    fetchers = {"kalshi": lambda l: (Decimal("0.40"), Decimal("500"))}   # bid 0.40 vs entry 0.50 = -20%
    decision = exit_decision(_bet([leg]), consensus, timedelta(hours=8), fetchers, _cfg(), now)
    assert decision is not None and decision["result"] == "STOPPED"


def test_no_stop_when_edge_still_positive() -> None:
    now = datetime.now(timezone.utc)
    leg = {"venue": "kalshi", "market_type": "WINNER", "outcome_label": "kansas city royals",
           "line": None, "event_key": EVENT, "start_utc": (now + timedelta(hours=4)).isoformat()}
    consensus = {(EVENT, "kansas city royals"): (Decimal("0.60"), now)}  # still +EV vs bid 0.40
    fetchers = {"kalshi": lambda l: (Decimal("0.40"), Decimal("500"))}
    assert exit_decision(_bet([leg]), consensus, timedelta(hours=8), fetchers, _cfg(), now) is None
