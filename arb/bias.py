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


def calibration(store: Store, bands: list[float], min_samples: int, flb_min_edge: float,
                data: list[tuple[float, bool]] | None = None) -> list[BandStat]:
    # `data` lets the caller pass the population sample (settled persisted quotes); when None
    # we fall back to the selection-biased flagged-bet sample (see SELECTION_CAVEAT).
    data = _settled_binaries(store) if data is None else data
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


# --- EV#5: apply the measured calibration back into p_hat (isotonic) ---------------------
# The FLB curve IS a correction function for p_hat (realized win rate vs implied prob). EV
# detection historically used raw p_hat; this fits a monotonic (isotonic) map from the
# population sample and lets detection apply p_hat_calibrated. Gated by config (default off).

def _pav(xs: list[float], ys: list[float], ws: list[float],
         min_block_weight: float = 1.0) -> tuple[list[float], list[float]]:
    """Pool-adjacent-violators: weighted isotonic (non-decreasing) fit of ys against sorted xs.

    B9: `min_block_weight` regularizes the fit by enforcing a minimum mass per node. Raw PAV on
    binary outcomes can emit single-sample blocks (a node defined by one settled bet) — pure
    noise that an in-sample calibrator then bakes into p_hat. After the monotone pool, adjacent
    under-mass blocks are merged into a neighbor so every surviving node rests on >= that many
    observations. Default 1.0 == legacy behavior (every point is already weight 1)."""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    sx = [xs[i] for i in order]
    blocks = [[ys[i], ws[i], sx_i] for i, sx_i in zip(order, sx)]  # value, weight, x
    i = 0
    while i < len(blocks) - 1:
        if blocks[i][0] <= blocks[i + 1][0] + 1e-12:
            i += 1
            continue
        v0, w0, _ = blocks[i]
        v1, w1, x1 = blocks[i + 1]
        merged = [(v0 * w0 + v1 * w1) / (w0 + w1), w0 + w1, x1]
        blocks[i:i + 2] = [merged]
        if i > 0:
            i -= 1
    if min_block_weight > 1.0:  # B9: enforce minimum node mass (monotonicity preserved)
        i = 0
        while i < len(blocks) and len(blocks) > 1:
            if blocks[i][1] >= min_block_weight:
                i += 1
                continue
            j = i + 1 if i == 0 else i - 1  # merge into the only/previous neighbor
            a, b = sorted((i, j))
            v0, w0, _ = blocks[a]
            v1, w1, x1 = blocks[b]
            blocks[a:b + 1] = [[(v0 * w0 + v1 * w1) / (w0 + w1), w0 + w1, x1]]
            i = max(0, a - 1)
    return [b[2] for b in blocks], [b[0] for b in blocks]


def fit_calibrator(data: list[tuple[float, bool]], min_samples: int = 50,
                   min_block_weight: float = 1.0, holdout: float = 0.0) -> tuple[list[float], list[float]] | None:
    """Isotonic calibrator nodes (x=implied prob, y=fitted realized) from settled raw quotes.
    None when the sample is too small to trust.

    B9: `holdout` fits a walk-forward FIT WINDOW only — drop the most recent `holdout` fraction
    (the data arrives time-ordered) so the map that detection applies going forward was not fit
    on the very bets it will grade, mirroring the scorer's walk_forward_holdout. `min_block_weight`
    regularizes node mass (see _pav). Both default to the legacy in-sample, unregularized fit."""
    if len(data) < min_samples:
        return None
    fit = data[: len(data) - int(len(data) * holdout)] if holdout > 0 else data
    if len(fit) < min_samples:
        return None
    xs = [x for x, _ in fit]
    ys = [1.0 if won else 0.0 for _, won in fit]
    ws = [1.0] * len(fit)
    nx, ny = _pav(xs, ys, ws, min_block_weight)
    return (nx, ny) if len(nx) >= 2 else None


def crossval_calibration_gain(data: list[tuple[float, bool]], folds: int = 5,
                              min_block_weight: float = 1.0) -> float | None:
    """B9: out-of-fold Brier improvement of the calibrator vs the raw implied prob. Positive
    means the FLB map generalizes (helps on held-out bets); <= 0 means it is fitting noise and
    should NOT be applied. The caller can gate `apply_calibration` on this being > 0. None when
    the sample is too thin to cross-validate."""
    n = len(data)
    if folds < 2 or n < folds * 10:
        return None
    raw_sq = 0.0
    cal_sq = 0.0
    cnt = 0
    for k in range(folds):
        test = [d for i, d in enumerate(data) if i % folds == k]
        train = [d for i, d in enumerate(data) if i % folds != k]
        nodes = fit_calibrator(train, min_samples=max(10, len(train) // 2),
                               min_block_weight=min_block_weight)
        if nodes is None:
            continue
        for x, won in test:
            y = 1.0 if won else 0.0
            raw_sq += (x - y) ** 2
            cal_sq += (calibrate_probability(x, nodes) - y) ** 2
            cnt += 1
    if cnt == 0:
        return None
    return round((raw_sq - cal_sq) / cnt, 6)  # >0: calibrator beats raw out-of-fold


def calibrate_probability(p: float, nodes: tuple[list[float], list[float]] | None) -> float:
    """Map a raw probability through the isotonic calibrator (linear interp, clamped to ends)."""
    if nodes is None:
        return p
    xs, ys = nodes
    if p <= xs[0]:
        return ys[0]
    if p >= xs[-1]:
        return ys[-1]
    for i in range(1, len(xs)):
        if p <= xs[i]:
            x0, x1, y0, y1 = xs[i - 1], xs[i], ys[i - 1], ys[i]
            if x1 == x0:
                return y1
            return y0 + (y1 - y0) * (p - x0) / (x1 - x0)
    return ys[-1]
