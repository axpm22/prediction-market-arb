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
    confirmation_rate: float = 1.0   # I1/I2: fraction of the segment's scored P&L that is real
                                     # (EV: pnl_confirmed not NULL; ARB: lock present). The
                                     # headline finding — a low rate means most P&L is zeroed.


def _sigmoid_norm(x: float, scale: float = 0.1) -> float:
    """Map a signed ROI through a logistic into (0,1); 0 ROI -> 0.5."""
    import math

    return 1.0 / (1.0 + math.exp(-x / scale))


def _settled_clean_bets(store: Store) -> list[dict]:
    with store.connect() as con:
        rows = list(
            con.execute(
                """
                SELECT sport, kind, result, pnl_optimistic, pnl_confirmed, stake_usd,
                       placed_at, settled_at, edge_points, optimistic_cost
                  FROM flat_bets
                 WHERE status = 'SETTLED' AND result IN ('WON','LOST','VOID','RESOLVED')
                   AND (quality_flag IS NULL)
                 ORDER BY id
                """
            )
        )
    return [dict(r) for r in rows]


def segment_calibration(bets_by_segment: dict[str, list[dict]], bins: int = 10,
                        min_n: int | None = None) -> dict[str, float]:
    """EV#10: per-segment ECE from settled EV bets' (p_hat, won). p_hat = optimistic_cost +
    edge_points (edge_points = p_hat - effective_cost). A segment can clear the ROI gate on
    variance while its p_hat is badly calibrated; this measures that directly.

    B10: ECE over ~1 sample/bin is pure noise, so a segment with only `bins` rows must not be
    blocked on it. `min_n` is the minimum calibratable sample before ECE is reported at all;
    a segment below it returns no ECE (and so is never calibration-blocked). Defaults to `bins`
    (legacy behavior); callers should pass a real floor (e.g. 200) when the gate is enabled."""
    from arb.forecast.scoring import calibration_bins, expected_calibration_error
    floor = max(bins, int(min_n)) if min_n is not None else bins
    out: dict[str, float] = {}
    for seg, bets in bets_by_segment.items():
        rows = []
        for b in bets:
            if b.get("edge_points") in (None, "") or b.get("optimistic_cost") in (None, "") or b["result"] not in ("WON", "LOST"):
                continue
            p_hat = float(b["optimistic_cost"]) + float(b["edge_points"])
            if 0.0 < p_hat < 1.0:
                rows.append({"p_you": p_hat, "outcome": 1 if b["result"] == "WON" else 0})
        if len(rows) >= floor:
            out[seg] = expected_calibration_error(calibration_bins(rows, bins), len(rows))
    return out


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
    # pass3#9: walk-forward. Score on a FIT window (drop the most recent holdout fraction) so
    # the gate isn't fitted and judged on the same bets it will act on. 0 = legacy in-sample.
    holdout = float(scoring_cfg.get("walk_forward_holdout", 0.0))
    # EV#10: optionally block a segment whose p_hat is badly calibrated even if ROI looks fine.
    calibration_gate = bool(scoring_cfg.get("calibration_gate", False))
    ece_max = float(scoring_cfg.get("ece_max", 0.10))
    # B10: minimum calibratable sample before the calibration gate may fire (ECE on ~1/bin is
    # noise). Only consulted when calibration_gate is on.
    ece_min_n = int(scoring_cfg.get("ece_min_n", 200))

    by_segment: dict[str, list[dict]] = {}
    for bet in _settled_clean_bets(store):
        seg = f"{bet['sport'] or 'unknown'}:{bet['kind']}"
        by_segment.setdefault(seg, []).append(bet)
    ece_by_segment = segment_calibration(by_segment, min_n=ece_min_n) if calibration_gate else {}

    scores: list[SegmentScore] = []
    for seg, all_bets in sorted(by_segment.items()):
        bets = all_bets[: len(all_bets) - int(len(all_bets) * holdout)] if holdout > 0 else all_bets
        if not bets:
            bets = all_bets
        n = len(bets)
        wins = sum(1 for b in bets if b["result"] == "WON")
        win_rate = wins / n if n else 0.0
        # I1/I2: pick the right P&L per kind. EV scores on the CONFIRMED fill (#6) — a bet that
        # never confirmed (NULL) was uncapturable, so 0 correctly drags ROI. But an ARB locks its
        # realized P&L AT ENTRY (pnl_optimistic); the EV "confirmed fill" concept doesn't map onto
        # a lock, and arbs always have NULL pnl_confirmed — scoring them via _confirmed zeroed
        # every arb segment and could wrongly BLOCK a profitable one. Score arbs on the lock.
        def _pnl(b) -> float:
            if b["kind"] == "ARB":
                return float(b["pnl_optimistic"]) if b["pnl_optimistic"] is not None else 0.0
            return float(b["pnl_confirmed"]) if b["pnl_confirmed"] is not None else 0.0

        def _is_confirmed(b) -> bool:
            return (b["pnl_optimistic"] if b["kind"] == "ARB" else b["pnl_confirmed"]) is not None

        confirmation_rate = sum(1 for b in bets if _is_confirmed(b)) / n if n else 1.0
        pnl = sum(_pnl(b) for b in bets)
        staked = sum(float(b["stake_usd"] or 0) for b in bets) or 1.0
        roi = pnl / staked
        # #10 trend distinct from win_rate: recency-weighted ROI MAGNITUDE over the recent
        # window (not the sign of each bet, which was nearly collinear with win_rate).
        recent = bets[-recent_window:]
        recent_pnl = sum(_pnl(b) for b in recent)
        recent_stake = sum(float(b["stake_usd"] or 0) for b in recent) or 1.0
        trend = recent_pnl / recent_stake
        if n < min_n:
            scores.append(SegmentScore(seg, n, round(win_rate, 4), round(roi, 4), 0.0,
                                       INSUFFICIENT, round(confirmation_rate, 4)))
            continue
        raw = 100.0 * (
            w_roi * _sigmoid_norm(roi)
            + w_trend * _sigmoid_norm(trend)
            + w_n * (min(n, n_cap) / n_cap)
            + w_wr * win_rate
        )
        score = round(raw, 1)
        tier = _tier_for(score, tiers, block_below)
        # EV#10: a poorly-calibrated segment is blocked regardless of ROI (variance can pass
        # the ROI gate while p_hat is wrong).
        if calibration_gate and ece_by_segment.get(seg, 0.0) > ece_max:
            tier = BLOCKED
        scores.append(SegmentScore(seg, n, round(win_rate, 4), round(roi, 4), score, tier,
                                   round(confirmation_rate, 4)))
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
                 roi REAL, score REAL, tier TEXT, confirmation_rate REAL)"""
        )
        # I1/I2: add the column to pre-existing tables (append-only schema evolution).
        cols = {r[1] for r in con.execute("PRAGMA table_info(segment_scores)")}
        if "confirmation_rate" not in cols:
            con.execute("ALTER TABLE segment_scores ADD COLUMN confirmation_rate REAL")
        now = datetime.now(timezone.utc).isoformat()
        con.executemany(
            "INSERT INTO segment_scores(computed_at, segment, n, win_rate, roi, score, tier, confirmation_rate) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [(now, s.segment, s.n, s.win_rate, s.roi, s.score, s.tier, s.confirmation_rate) for s in scores],
        )
