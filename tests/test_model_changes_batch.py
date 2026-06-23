from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from arb.cli import detect_ev_from_consensus
from arb.config import load_config
from arb.matching import event_key
from arb.models import Quote
from arb.experimental.whales import wallet_stats

EVENT = "baseball_mlb:2026-06-12:kansas city royals:texas rangers"


# ---- weakness#7: event_key keyed on sports-local date, no 00:00 UTC seam ----

def test_event_key_no_utc_midnight_seam() -> None:
    # Same night in ET, but one venue's start lands after 00:00 UTC.
    a = event_key("baseball_mlb", "Athletics", "Mariners",
                  datetime(2026, 6, 9, 23, 55, tzinfo=timezone.utc))   # 19:55 ET Jun 9
    b = event_key("baseball_mlb", "Athletics", "Mariners",
                  datetime(2026, 6, 10, 2, 30, tzinfo=timezone.utc))   # 22:30 ET Jun 9
    assert a == b
    assert ":2026-06-09:" in a   # ET date, not the 06-10 UTC date


# ---- pass3#3: EV horizon / capital-charge gate ----

def _quote(cost: str, days_out: int) -> Quote:
    c = Decimal(cost)
    return Quote(event_key=EVENT, venue="kalshi", book="KX", market_type="WINNER", line=None,
                 outcome_label="kansas city royals", raw_price=c, cost=c, fee_cost=Decimal("0"),
                 depth_usd=Decimal("500"), live=False, fetched_at=datetime.now(timezone.utc),
                 start_utc=datetime.now(timezone.utc) + timedelta(days=days_out),
                 home="kansas city royals", away="texas rangers")


def test_far_dated_tiny_edge_ev_is_uneconomic_horizon() -> None:
    config = load_config()
    fresh = datetime.now(timezone.utc)
    # 0.5008 vs cost 0.50 -> ratio 0.0016 expected return; over 13 days the 5%/yr capital
    # charge is 0.00178, so this loses to cash. (e_min tiny so it isn't caught as below-e-min.)
    consensus = {(EVENT, "kansas city royals"): (Decimal("0.5008"), fresh)}
    [opp] = detect_ev_from_consensus([_quote("0.50", 13)], consensus, max_age_hours=8,
                                     trusted_pairs=set(), e_min=Decimal("0.0001"), config=config)
    assert opp.quality_flag == "uneconomic-horizon"


def test_near_dated_normal_edge_is_clean() -> None:
    config = load_config()
    fresh = datetime.now(timezone.utc)
    consensus = {(EVENT, "kansas city royals"): (Decimal("0.56"), fresh)}
    [opp] = detect_ev_from_consensus([_quote("0.50", 1)], consensus, max_age_hours=8,
                                     trusted_pairs=set(), e_min=Decimal("0.01"), config=config)
    assert opp.quality_flag is None


# ---- pass3#6 / weakness#8: stale-fee venue quarantine ----

def test_stale_fee_venue_is_quarantined() -> None:
    config = replace(load_config(), stale_fee_venues=frozenset({"kalshi"}))
    fresh = datetime.now(timezone.utc)
    consensus = {(EVENT, "kansas city royals"): (Decimal("0.56"), fresh)}
    [opp] = detect_ev_from_consensus([_quote("0.50", 1)], consensus, max_age_hours=8,
                                     trusted_pairs=set(), e_min=Decimal("0.01"), config=config)
    assert opp.quality_flag == "stale-fee"
    assert "stale-fee" in opp.notes


# ---- pass3#10: whales qualify on ROI, not hit rate ----

def test_high_win_rate_but_unprofitable_whale_not_qualified() -> None:
    cfg = {"min_trades": 100, "wr_ceiling": 0.95, "min_category_share": 0.50, "min_roi": 0.0}
    # 65% win rate, but each share bought at 0.90: winners make +0.10, losers lose -0.90 -> ROI<0.
    ts = [{"proxyWallet": "0xdog", "conditionId": f"c{i}", "side": "BUY", "price": 0.9, "size": 1}
          for i in range(120)]
    settled = {f"c{i}": (i < 78) for i in range(120)}   # 78/120 = 0.65 win rate
    cats = {f"c{i}": "politics" for i in range(120)}
    [w] = wallet_stats(ts, settled, cats, cfg)
    assert w.win_rate >= 0.60          # would have passed the old hit-rate gate
    assert w.roi < 0                   # but it loses money
    assert w.qualified is False        # ...so it is not qualified


# ---- EV#7: continuous stale-consensus edge decay ----

def test_consensus_edge_decays_with_age() -> None:
    config = replace(load_config(), ev_edge_decay_per_hour=Decimal("0.02"))
    # consensus 2h old, raw edge 0.06 -> decayed 0.06 - 0.04 = 0.02; below an e_min of 0.03.
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    consensus = {(EVENT, "kansas city royals"): (Decimal("0.56"), old)}
    [opp] = detect_ev_from_consensus([_quote("0.50", 1)], consensus, max_age_hours=8,
                                     trusted_pairs=set(), e_min=Decimal("0.03"), config=config,
                                     pregame_max_age_hours=8)
    assert any(n.startswith("edge_decayed:") for n in opp.notes)
    assert "below-e-min" in opp.notes        # decayed below e_min even though raw edge was 0.06


# ---- EV#3: dispersion gate + lower-bound Kelly ----

def test_dispersion_gate_quarantines_and_shrinks_kelly() -> None:
    config = replace(load_config(), ev_dispersion_gate=True, ev_edge_se_min=Decimal("2.0"),
                     ev_dispersion_kelly_haircut=Decimal("1.0"))
    fresh = datetime.now(timezone.utc)
    consensus = {(EVENT, "kansas city royals"): (Decimal("0.56"), fresh)}
    # edge 0.06 with wide dispersion 0.05 -> edge/SE = 1.2 < 2.0 -> low-edge-se.
    disp = {(EVENT, "kansas city royals"): Decimal("0.05")}
    [opp] = detect_ev_from_consensus([_quote("0.50", 1)], consensus, max_age_hours=8,
                                     trusted_pairs=set(), e_min=Decimal("0.01"), config=config,
                                     dispersion=disp)
    assert opp.quality_flag == "low-edge-se"
    assert any(n.startswith("edge_se:") for n in opp.notes)
    # Kelly sized off the lower bound p_hat-0.05=0.51 is smaller than off 0.56.
    base = detect_ev_from_consensus([_quote("0.50", 1)], consensus, max_age_hours=8,
                                    trusted_pairs=set(), e_min=Decimal("0.01"),
                                    config=replace(config, ev_dispersion_gate=False))[0]
    assert opp.kelly_stake < base.kelly_stake
