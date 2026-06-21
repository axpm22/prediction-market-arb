"""Per-segment edge scorer — BUILD_PLAN Phase 2.

Edge is not uniform across segments. This scores each segment (sport × kind) from its
settled, CLEAN (Phase 1) bets only, blocks segments with no proven edge, and surfaces the
ones that do. Everything is recomputed from the append-only ledger, never stored as truth.

Score (0-100), all weights config-driven:
  score = 100 * (w_roi·norm(roi) + w_trend·norm(trend) + w_n·norm(min(n,n_cap)/n_cap)
                 + w_wr·win_rate)
A segment with n < score_min_n is INSUFFICIENT_DATA (never blocked or trusted).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .store import Store

INSUFFICIENT = "INSUFFICIENT_DATA"
BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class SegmentScore:
    segment: str
    n: int
    win_rate: float
    roi: float
    score: float
    tier: str


def _sigmoid_norm(x: float, scale: float = 0.1) -> float:
    """Map a signed ROI through a logistic into (0,1); 0 ROI -> 0.5."""
    import math

    return 1.0 / (1.0 + math.exp(-x / scale))


def _settled_clean_bets(store: Store) -> list[dict]:
    with store.connect() as con:
        rows = list(
            con.execute(
                """
                SELECT sport, kind, result, pnl_optimistic, stake_usd, placed_at, settled_at
                  FROM flat_bets
                 WHERE status = 'SETTLED' AND result IN ('WON','LOST','VOID','RESOLVED')
                   AND (quality_flag IS NULL)
                 ORDER BY id
                """
            )
        )
    return [dict(r) for r in rows]


def score_segments(store: Store, scoring_cfg: dict) -> list[SegmentScore]:
    weights = scoring_cfg.get("weights", {})
    w_roi = float(weights.get("roi", 0.40))
    w_trend = float(weights.get("trend", 0.25))
    w_n = float(weights.get("sample", 0.20))
    w_wr = float(weights.get("win_rate", 0.15))
    n_cap = int(scoring_cfg.get("n_cap", 100))
    min_n = int(scoring_cfg.get("score_min_n", 30))
    block_below = float(scoring_cfg.get("block_below", 30))
    recent_window = int(scoring_cfg.get("recent_window", 10))
    tiers = scoring_cfg.get("tiers", {"full": 80, "high": 60, "low": 40, "minimal": 30})

    by_segment: dict[str, list[dict]] = {}
    for bet in _settled_clean_bets(store):
        seg = f"{bet['sport'] or 'unknown'}:{bet['kind']}"
        by_segment.setdefault(seg, []).append(bet)

    scores: list[SegmentScore] = []
    for seg, bets in sorted(by_segment.items()):
        n = len(bets)
        wins = sum(1 for b in bets if b["result"] == "WON")
        win_rate = wins / n if n else 0.0
        pnl = sum(float(b["pnl_optimistic"] or 0) for b in bets)
        staked = sum(float(b["stake_usd"] or 0) for b in bets) or 1.0
        roi = pnl / staked
        # recency-weighted trend: mean signed P&L of the last `recent_window` bets.
        recent = bets[-recent_window:]
        trend = sum((1.0 if (b["pnl_optimistic"] or 0) and float(b["pnl_optimistic"]) > 0 else -1.0)
                    for b in recent) / len(recent) if recent else 0.0
        if n < min_n:
            scores.append(SegmentScore(seg, n, round(win_rate, 4), round(roi, 4), 0.0, INSUFFICIENT))
            continue
        raw = 100.0 * (
            w_roi * _sigmoid_norm(roi)
            + w_trend * ((trend + 1.0) / 2.0)
            + w_n * (min(n, n_cap) / n_cap)
            + w_wr * win_rate
        )
        score = round(raw, 1)
        tier = _tier_for(score, tiers, block_below)
        scores.append(SegmentScore(seg, n, round(win_rate, 4), round(roi, 4), score, tier))
    return scores


def _tier_for(score: float, tiers: dict, block_below: float) -> str:
    if score < block_below:
        return BLOCKED
    if score >= tiers.get("full", 80):
        return "full"
    if score >= tiers.get("high", 60):
        return "high"
    if score >= tiers.get("low", 40):
        return "low"
    return "minimal"


def blocked_segments(store: Store, scoring_cfg: dict) -> set[str]:
    """Segments a new bet must not be recorded as eligible for (still logged, tagged)."""
    return {s.segment for s in score_segments(store, scoring_cfg) if s.tier == BLOCKED}


def persist_scores(store: Store, scores: list[SegmentScore]) -> None:
    from datetime import datetime, timezone

    with store.connect() as con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS segment_scores(
                 computed_at TEXT, segment TEXT, n INTEGER, win_rate REAL,
                 roi REAL, score REAL, tier TEXT)"""
        )
        now = datetime.now(timezone.utc).isoformat()
        con.executemany(
            "INSERT INTO segment_scores(computed_at, segment, n, win_rate, roi, score, tier) VALUES (?,?,?,?,?,?,?)",
            [(now, s.segment, s.n, s.win_rate, s.roi, s.score, s.tier) for s in scores],
        )
