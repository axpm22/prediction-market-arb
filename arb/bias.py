"""Favorite-longshot bias — BUILD_PLAN S2 (Grade A).

The most robust, decades-persistent bias in the literature: longshots are overpriced,
favorites underpriced. No cross-venue matching, no live orders — a pure directional
signal you BACKTEST on the settled bets you already store, then forward-test flat-$1.

The deliverable is the calibration curve: per price band, realized win rate vs implied
probability. Bands where realized > implied by >= flb_min_edge are back-able; bands where
realized < implied (longshots) are avoid/short. Bands are fitted from data, not constants.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal

from .store import Store

# #2 The sample is drawn only from bets that PASSED the EV/arb filters, so this curve
# reflects the selection rule, not the market's population longshot premium. A true FLB
# study needs a representative cross-section of prices across the whole range (e.g. settled
# raw quotes), which this ledger does not yet capture. Treat the signal as in-sample only.
SELECTION_CAVEAT = (
    "CAVEAT: sample is flagged-bets-only (passed EV/arb filters) -> selection-biased; "
    "this measures the filter's calibration, not the population favorite-longshot premium."
)


@dataclass(frozen=True)
class BandStat:
    lo: float
    hi: float
    n: int
    implied_mid: float        # band midpoint (reference only)
    mean_cost: float          # #3 mean PRE-FEE implied prob actually observed in the band
    realized_win_rate: float
    edge: float               # realized - mean_cost (positive => underpriced => back)
    signal: str               # 'back' | 'avoid' | 'neutral' | 'INSUFFICIENT_DATA'


def _settled_binaries(store: Store) -> list[tuple[float, bool]]:
    """(entry PRE-FEE cost, won) for each settled clean binary bet.

    #3 Bins on the de-vigged/raw cost (leg `cost`), NOT optimistic_cost (= cost + fee +
    taker buffer). Fees push every bet into a higher band, so using the fee-loaded price
    makes fees masquerade as the longshot premium (realized < implied purely from fees).
    """
    out: list[tuple[float, bool]] = []
    with store.connect() as con:
        rows = con.execute(
            """SELECT optimistic_cost, legs_json, result FROM flat_bets
                WHERE status='SETTLED' AND result IN ('WON','LOST')
                  AND quality_flag IS NULL"""
        )
        for r in rows:
            c = _pre_fee_cost(r["legs_json"], r["optimistic_cost"])
            if c is not None and 0.0 < c < 1.0:
                out.append((c, r["result"] == "WON"))
    return out


def _pre_fee_cost(legs_json, optimistic_cost) -> float | None:
    try:
        legs = json.loads(legs_json) if legs_json else []
    except (TypeError, ValueError):
        legs = []
    if legs and legs[0].get("cost") is not None:
        try:
            return float(legs[0]["cost"])
        except (TypeError, ValueError):
            pass
    try:
        return float(optimistic_cost)  # fall back only if no per-leg pre-fee cost
    except (TypeError, ValueError):
        return None


def calibration(store: Store, bands: list[float], min_samples: int, flb_min_edge: float) -> list[BandStat]:
    data = _settled_binaries(store)
    stats: list[BandStat] = []
    for lo, hi in zip(bands, bands[1:]):
        in_band = [(c, won) for c, won in data if lo <= c < hi]
        n = len(in_band)
        implied_mid = (lo + hi) / 2.0
        mean_cost = sum(c for c, _ in in_band) / n if n else implied_mid
        wr = sum(1 for _, w in in_band if w) / n if n else 0.0
        # #3 Compare realized to the MEAN actual cost in the band, not the midpoint.
        edge = wr - mean_cost if n else 0.0
        if n < min_samples:
            signal = "INSUFFICIENT_DATA"
        elif edge >= flb_min_edge:
            signal = "back"
        elif edge <= -flb_min_edge:
            signal = "avoid"
        else:
            signal = "neutral"
        stats.append(BandStat(lo, hi, n, implied_mid, round(mean_cost, 4),
                              round(wr, 4), round(edge, 4), signal))
    return stats
