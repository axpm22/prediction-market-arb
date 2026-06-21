"""Favorite-longshot bias — BUILD_PLAN S2 (Grade A).

The most robust, decades-persistent bias in the literature: longshots are overpriced,
favorites underpriced. No cross-venue matching, no live orders — a pure directional
signal you BACKTEST on the settled bets you already store, then forward-test flat-$1.

The deliverable is the calibration curve: per price band, realized win rate vs implied
probability. Bands where realized > implied by >= flb_min_edge are back-able; bands where
realized < implied (longshots) are avoid/short. Bands are fitted from data, not constants.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .store import Store


@dataclass(frozen=True)
class BandStat:
    lo: float
    hi: float
    n: int
    implied_mid: float        # midpoint of the band = implied probability proxy
    realized_win_rate: float
    edge: float               # realized - implied (positive => underpriced => back)
    signal: str               # 'back' | 'avoid' | 'INSUFFICIENT_DATA'


def _settled_binaries(store: Store) -> list[tuple[float, bool]]:
    """(entry_effective_cost, won) for each settled clean binary bet with a numeric cost."""
    out: list[tuple[float, bool]] = []
    with store.connect() as con:
        rows = con.execute(
            """SELECT optimistic_cost, result FROM flat_bets
                WHERE status='SETTLED' AND result IN ('WON','LOST')
                  AND quality_flag IS NULL AND optimistic_cost IS NOT NULL"""
        )
        for r in rows:
            try:
                c = float(r["optimistic_cost"])
            except (TypeError, ValueError):
                continue
            if 0.0 < c < 1.0:
                out.append((c, r["result"] == "WON"))
    return out


def calibration(store: Store, bands: list[float], min_samples: int, flb_min_edge: float) -> list[BandStat]:
    data = _settled_binaries(store)
    stats: list[BandStat] = []
    for lo, hi in zip(bands, bands[1:]):
        in_band = [won for c, won in data if lo <= c < hi]
        n = len(in_band)
        implied_mid = (lo + hi) / 2.0
        if n < min_samples:
            stats.append(BandStat(lo, hi, n, implied_mid, 0.0, 0.0, "INSUFFICIENT_DATA"))
            continue
        wr = sum(1 for w in in_band if w) / n
        edge = wr - implied_mid
        signal = "back" if edge >= flb_min_edge else ("avoid" if edge <= -flb_min_edge else "neutral")
        stats.append(BandStat(lo, hi, n, implied_mid, round(wr, 4), round(edge, 4), signal))
    return stats
