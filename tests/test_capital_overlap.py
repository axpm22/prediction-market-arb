from datetime import datetime, timedelta, timezone
from decimal import Decimal

from arb.arbitrage import detect_true_arbs
from arb.cli import cross_venue_overlap
from arb.models import Quote
from arb.pricing import capital_charge, is_economic


# ---- #6 capital charge ----

def test_capital_charge_prorates_over_holding_period() -> None:
    # 5%/yr over 14 days.
    assert capital_charge(14, Decimal("0.05")) == (Decimal("0.05") * Decimal("14") / Decimal("365")).quantize(Decimal("0.000001"))


def test_small_lock_held_long_is_uneconomic() -> None:
    # 0.2% locked, held 14 days, 5%/yr capital charge (~0.19%) -> still economic by a hair;
    # 0.1% locked is not.
    assert is_economic(Decimal("0.05"), 14, Decimal("0.05")) is True   # 5% lock easily clears
    assert is_economic(Decimal("0.001"), 14, Decimal("0.05")) is False  # 0.1% over 14d loses to cash


def test_uneconomic_tag_on_tiny_far_dated_arb() -> None:
    far = datetime.now(timezone.utc) + timedelta(days=14)

    def q(venue, outcome, cost):
        c = Decimal(cost)
        return Quote(event_key="baseball_mlb:2026-07-01:a:b:0", venue=venue, book=f"{venue}-{outcome}",
                     market_type="WINNER", line=None, outcome_label=outcome, raw_price=c, cost=c,
                     fee_cost=Decimal("0"), depth_usd=Decimal("500"), live=False,
                     fetched_at=datetime.now(timezone.utc), start_utc=far)

    # 0.499 + 0.5 = 0.999 -> ~0.1% lock, held 14 days -> uneconomic vs 5%/yr.
    opps = detect_true_arbs([q("kalshi", "a", "0.499"), q("polymarket", "b", "0.50")],
                            bankroll=Decimal("1000"), max_stake_pct=Decimal("0.1"),
                            trusted_pairs=set(), stale_warn=Decimal("0.04"),
                            capital_charge_annual=Decimal("0.05"))
    assert opps and any("uneconomic" in o.notes for o in opps)


# ---- #4 cross-venue overlap ----

def test_overlap_counts_multi_venue_events() -> None:
    def q(venue, ek):
        return Quote(event_key=ek, venue=venue, book="b", market_type="WINNER", line=None,
                     outcome_label="x", raw_price=Decimal("0.5"), cost=Decimal("0.5"),
                     fee_cost=Decimal("0"), depth_usd=None, live=False, fetched_at=datetime.now(timezone.utc))
    quotes = [q("kalshi", "e1"), q("polymarket", "e1"),  # overlap
              q("kalshi", "e2")]                          # single venue
    overlap, events = cross_venue_overlap(quotes)
    assert overlap == 1 and events == 2
