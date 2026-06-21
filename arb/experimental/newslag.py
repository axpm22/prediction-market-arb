"""Event / news-latency directional — BUILD_PLAN S4 (Grade B, measure only).

Prices jump on discrete news; whoever ingests it faster wins. You can't beat HFT bots on
raw latency, but you CAN measure whether your venues lag a public, timestamped reference
signal — and that measurement is the resume artifact, not the (infeasible) execution.

Given a reference series (e.g. the BTC index) and a venue mid series, find reference moves
above a threshold and measure how long until the venue re-prices in the same direction.
A consistent, null-corrected positive lag is the (theoretical) edge window. NEVER executes;
it logs a paper bet at the next executable price after the signal and measures CLV-after-lag.
If CLV after realistic lag is <= 0, the edge isn't capturable by you — report that honestly.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from statistics import median


@dataclass(frozen=True)
class LagSample:
    signal_time: datetime
    direction: int          # +1 / -1
    venue_lag_secs: float    # time until the venue re-priced in the same direction


def _moves(series: list[tuple[datetime, float]], threshold: float) -> list[tuple[datetime, int]]:
    out = []
    for (t0, p0), (t1, p1) in zip(series, series[1:]):
        d = p1 - p0
        if abs(d) >= threshold:
            out.append((t1, 1 if d > 0 else -1))
    return out


def measure_lag(reference: list[tuple[datetime, float]], venue: list[tuple[datetime, float]],
                ref_threshold: float, venue_threshold: float, max_lag_secs: float) -> list[LagSample]:
    """For each sharp reference move, the time until the venue moves the same way."""
    ref_moves = _moves(reference, ref_threshold)
    venue_moves = _moves(venue, venue_threshold)
    samples: list[LagSample] = []
    for t_ref, d_ref in ref_moves:
        following = [
            (t_v - t_ref).total_seconds()
            for t_v, d_v in venue_moves
            if d_v == d_ref and 0 < (t_v - t_ref).total_seconds() <= max_lag_secs
        ]
        if following:
            samples.append(LagSample(t_ref, d_ref, min(following)))
    return samples


def lag_summary(samples: list[LagSample]) -> dict:
    if not samples:
        return {"n": 0}
    lags = [s.venue_lag_secs for s in samples]
    return {
        "n": len(samples),
        "median_lag_secs": round(median(lags), 2),
        "mean_lag_secs": round(sum(lags) / len(lags), 2),
        "min_lag_secs": round(min(lags), 2),
    }


def clv_after_lag(signal_price: float, fill_price_after_lag: float, direction: int) -> float:
    """CLV of a paper bet placed at the next executable price after the signal, in the
    direction of the move. Positive = the venue kept moving your way after you filled
    (capturable); <= 0 = you were already too late."""
    return round(direction * (fill_price_after_lag - signal_price), 6)
