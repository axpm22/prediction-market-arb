from __future__ import annotations

import math
import random
from dataclasses import dataclass
from decimal import Decimal
from statistics import mean


@dataclass(frozen=True)
class ForecastSummary:
    n: int
    mean_brier_you: float
    mean_brier_market: float
    brier_skill: float
    mean_brier_diff: float                       # #9 mean per-event (you - market); <0 = you better
    mean_logloss_you: float
    mean_logloss_market: float
    logloss_skill: float
    ece: float
    brier_diff_ci95: tuple[float, float] | None  # #9 bootstrap CI of the PAIRED difference


def brier(probability: Decimal | str | float, outcome: int) -> float:
    p = float(probability)
    y = 1.0 if outcome else 0.0
    return round((p - y) ** 2, 6)


def log_loss(probability: Decimal | str | float, outcome: int, clip: float = 0.000001) -> float:
    p = min(max(float(probability), clip), 1.0 - clip)
    y = 1.0 if outcome else 0.0
    return round(-(y * math.log(p) + (1.0 - y) * math.log(1.0 - p)), 6)


def skill_score(model_score: float, market_score: float) -> float:
    if market_score == 0:
        return 0.0
    return round(1.0 - model_score / market_score, 6)


def calibration_bins(rows: list[dict], bins: int) -> list[dict]:
    out: list[dict] = []
    for i in range(bins):
        lo = i / bins
        hi = (i + 1) / bins
        group = [
            row for row in rows
            if lo <= float(row["p_you"]) < hi or (i == bins - 1 and float(row["p_you"]) == 1.0)
        ]
        if not group:
            out.append({"bin": f"{lo:.1f}-{hi:.1f}", "n": 0, "predicted": None, "realized": None, "abs_error": None})
            continue
        predicted = mean(float(row["p_you"]) for row in group)
        realized = mean(int(row["outcome"]) for row in group)
        out.append({
            "bin": f"{lo:.1f}-{hi:.1f}",
            "n": len(group),
            "predicted": round(predicted, 4),
            "realized": round(realized, 4),
            "abs_error": round(abs(predicted - realized), 4),
        })
    return out


def expected_calibration_error(bins: list[dict], n: int) -> float:
    if n == 0:
        return 0.0
    return round(sum((row["n"] / n) * float(row["abs_error"] or 0.0) for row in bins), 6)


def summarize(rows: list[dict], bins: int = 10, clip: float = 0.000001) -> tuple[ForecastSummary, list[dict]]:
    n = len(rows)
    if n == 0:
        empty = ForecastSummary(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, None)
        return empty, calibration_bins([], bins)
    brier_you = mean(float(row["brier_you"]) for row in rows)
    brier_market = mean(float(row["brier_market"]) for row in rows)
    logloss_you = mean(float(row["logloss_you"]) for row in rows)
    logloss_market = mean(float(row["logloss_market"]) for row in rows)
    # #9 paired difference per event — the correct instrument when your p and the market's
    # are often nearly identical (a ratio-of-means understates variance in exactly that case).
    diffs = [float(row["brier_you"]) - float(row["brier_market"]) for row in rows]
    cal = calibration_bins(rows, bins)
    summary = ForecastSummary(
        n=n,
        mean_brier_you=round(brier_you, 6),
        mean_brier_market=round(brier_market, 6),
        brier_skill=skill_score(brier_you, brier_market),
        mean_brier_diff=round(mean(diffs), 6),
        mean_logloss_you=round(logloss_you, 6),
        mean_logloss_market=round(logloss_market, 6),
        logloss_skill=skill_score(logloss_you, logloss_market),
        ece=expected_calibration_error(cal, n),
        brier_diff_ci95=_bootstrap_paired_diff(diffs) if n >= 30 else None,
    )
    return summary, cal


def _bootstrap_paired_diff(diffs: list[float], resamples: int = 5000) -> tuple[float, float]:
    """Bootstrap CI of the mean per-event (brier_you - brier_market). A CI strictly below 0
    is evidence you out-predict the market; straddling 0 is not."""
    rng = random.Random(12345)
    n = len(diffs)
    values = [mean(rng.choices(diffs, k=n)) for _ in range(resamples)]
    values.sort()
    return (round(values[int(0.025 * resamples)], 6), round(values[int(0.975 * resamples)], 6))
