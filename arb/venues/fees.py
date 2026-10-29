"""Single fee implementation (SPEC §6.5). All fees route through leg_fee_cost,
driven by FEE_BASIS. Fees are dated facts; staleness is a badge, never a halt.

mode='paper'      continuous curve, fractional contracts (the flat-$1 ledger)
mode='execution'  whole contracts; Kalshi ceil_to_cent applies
role='taker'|'maker'  maker is a REDUCED Kalshi fee (25% of taker), never zero.
"""
from __future__ import annotations

import datetime
from decimal import ROUND_CEILING, Decimal

CENT = Decimal("0.01")


def ceil_to_cent(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_CEILING)


def _kalshi(cfg, category, price, contracts, mode, role, multiplier=Decimal(1)):
    rate = cfg["kalshi"]["maker_fee_rate"] if role == "maker" else cfg["kalshi"]["fee_rate"]
    fee = multiplier * rate * contracts * price * (1 - price)
    return ceil_to_cent(fee) if mode == "execution" else fee


# Polymarket quadratic per category (schedule effective 2026-03-30)
_PM_CATEGORY_RATE_KEY = {
    "sports": "sports_fee_rate", "crypto": "crypto_fee_rate",
    "politics": "politics_fee_rate", "finance": "politics_fee_rate",
    "tech": "politics_fee_rate",
    "econ": "econ_weather_fee_rate", "culture": "econ_weather_fee_rate",
    "weather": "econ_weather_fee_rate", "other": "econ_weather_fee_rate",
    "geopolitics": "geopolitics_fee_rate",
}


def _polymarket(cfg, category, price, contracts, mode, role):
    if role == "maker":
        return Decimal(0)  # makers pay 0 (+ taker-funded rebate pool, not modeled)
    key = _PM_CATEGORY_RATE_KEY.get(category, "fee_rate")
    return cfg["polymarket"][key] * contracts * price * (1 - price)


def _sx(cfg, category, price, contracts, mode, role):
    return Decimal(0)  # singles 0% both sides; 5% winning PARLAYS modeled in the parlay lane


FEE_BASIS = {"kalshi": _kalshi, "polymarket": _polymarket, "sx": _sx}

# Gamma tag_slug -> fee category (nonsports.polymarket_tags feeds the negrisk
# scan; pricing every basket as "politics" understated economy/weather fees)
PM_TAG_CATEGORY = {"politics": "politics", "economy": "econ", "weather": "weather"}


def leg_fee_cost(cfg, venue: str, category: str, price: Decimal,
                 contracts: Decimal = Decimal(1), mode: str = "paper",
                 role: str = "taker", multiplier: Decimal | None = None) -> Decimal:
    """multiplier: Kalshi per-series fee_multiplier (GET /series/fee_changes),
    e.g. MLB series 0.5 from 2026-08-07. Kalshi-only; None = 1."""
    args = (cfg, category, Decimal(price), Decimal(contracts), mode, role)
    if multiplier is not None:
        return FEE_BASIS[venue](*args, multiplier=Decimal(multiplier))
    return FEE_BASIS[venue](*args)


def fee_is_stale(cfg, venue: str, today: datetime.date | None = None) -> bool:
    today = today or datetime.date.today()
    confirmed = cfg[venue]["confirmed_on"]
    return (today - confirmed).days > int(cfg["fee_stale_days"])
