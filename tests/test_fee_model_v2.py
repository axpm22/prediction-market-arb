"""05 — Polymarket quadratic per-category fees, Kalshi vector unchanged, SX singles 0 / parlay 5%.

Versioned change 2026-07-02: Polymarket's published taker fee is quadratic (rate·p·(1−p), per
category; docs.polymarket.com/trading/fees, effective 2026-03-30). The old linear model
double-charged Polymarket legs at mid prices. These vectors lock the corrected model.
"""
from decimal import Decimal

from arb.config import load_config
from arb.pricing import all_in_fee_cost
from arb.settlement import parlay_sx_win_fee
from arb.venues.slippage import FEE_BASIS, leg_fee_cost

HALF = Decimal("0.50")


def test_polymarket_is_priced_quadratic() -> None:
    assert FEE_BASIS["polymarket"] == "quadratic"


def test_polymarket_per_category_fee_vectors() -> None:
    cfg = load_config()
    # sports 0.03 @ p=0.5 → 0.03*0.5*0.5 = 0.0075 (was 0.015 under the superseded linear model)
    assert all_in_fee_cost(HALF, cfg.polymarket_sports_fee_rate, basis="quadratic") == Decimal("0.007500")
    # crypto 0.07 @ p=0.5 → 0.0175
    assert all_in_fee_cost(HALF, cfg.polymarket_crypto_fee_rate, basis="quadratic") == Decimal("0.017500")
    # politics 0.04 @ p=0.25 → 0.04*0.25*0.75 = 0.0075
    assert all_in_fee_cost(Decimal("0.25"), cfg.polymarket_politics_fee_rate, basis="quadratic") == Decimal("0.007500")
    # econ/weather default 0.05 @ p=0.5 → 0.0125
    assert all_in_fee_cost(HALF, cfg.polymarket_econ_weather_fee_rate, basis="quadratic") == Decimal("0.012500")
    # geopolitics 0 → no fee
    assert all_in_fee_cost(HALF, cfg.polymarket_geopolitics_fee_rate, basis="quadratic") == Decimal("0.000000")


def test_kalshi_vector_unchanged() -> None:
    cfg = load_config()
    assert all_in_fee_cost(HALF, cfg.kalshi_fee_rate, basis="quadratic") == Decimal("0.017500")


def test_leg_fee_cost_routes_polymarket_to_quadratic() -> None:
    # K1/K2 parity: leg_fee_cost (adapters + flash verifier) prices Polymarket on the same
    # quadratic curve as all_in_fee_cost — one implementation, no divergence.
    rate = Decimal("0.03")
    via_leg = leg_fee_cost("polymarket", HALF, rate, None, "fixed", Decimal("0"), Decimal("0"))
    assert via_leg == all_in_fee_cost(HALF, rate, Decimal("0"), basis="quadratic") == Decimal("0.007500")


def test_sx_single_fee_is_zero() -> None:
    cfg = load_config()
    # SX singles: taker/maker 0 and win_fee 0 → a single SX leg carries no fee.
    fee = leg_fee_cost("sx", Decimal("0.60"), cfg.sx_fee_rate or Decimal("0"), None, "fixed",
                       Decimal("0"), Decimal("0"), win_fee_rate=cfg.sx_win_fee_rate)
    assert fee == Decimal("0.000000")


def test_sx_parlay_win_fee_nets_five_percent_only_on_winning_sx_parlays() -> None:
    rate = Decimal("0.05")
    sx_legs = [{"venue": "kalshi"}, {"venue": "sx"}]
    # winning SX parlay: 5% off the winnings
    assert parlay_sx_win_fee(Decimal("1.00"), sx_legs, rate) == Decimal("0.950000")
    # loss / void: no fee
    assert parlay_sx_win_fee(Decimal("-1.00"), sx_legs, rate) == Decimal("-1.00")
    assert parlay_sx_win_fee(Decimal("0.00"), sx_legs, rate) == Decimal("0.00")
    # winning parlay with no SX leg: untouched
    assert parlay_sx_win_fee(Decimal("1.00"), [{"venue": "kalshi"}, {"venue": "polymarket"}], rate) == Decimal("1.00")


def test_config_loads_new_fee_keys() -> None:
    cfg = load_config()
    assert cfg.polymarket_politics_fee_rate == Decimal("0.04")
    assert cfg.polymarket_geopolitics_fee_rate == Decimal("0")
    assert cfg.parlay_sx_win_fee_rate == Decimal("0.05")
