"""MODEL_FOUNDATIONS Phase A — is the consensus actually right?

Scores three forecasters on settled events — de-vigged soft-book consensus (p_hat), Kalshi mid,
Polymarket mid — by Brier / log-loss / ECE, overall and per sport. Then the EV-subset test: on the
bets the model actually flagged, did the venue side or the consensus side win more often? If the
venues beat the consensus, "venue cheaper than consensus" selects consensus error, not edge — and
the EV lane's sign is inverted. Read-only; reuses the existing MLB scores resolver + forecast
scoring. Annotated loop rows are excluded from the EV-subset (PURGE_LOOP_PNL)."""
from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from arb.forecast.scoring import brier, log_loss
from arb.experimental.analysis.devig_brier import score_pairs
from arb.matching import load_aliases
from arb.quote_settlement import _mlb_scores_resolver
from arb.store import Store


@dataclass
class SourceScore:
    source: str
    sport: str
    n: int
    brier: float
    log_loss: float
    ece: float


def _mid(cost: str, best_bid: str | None) -> Decimal | None:
    """Venue mid for an outcome: (best_bid + ask)/2 per $1 payout. Falls back to the ask alone
    when no resting bid is stored (a one-sided book is still a usable point estimate)."""
    try:
        ask = Decimal(str(cost))
    except (TypeError, ArithmeticError):
        return None
    if best_bid in (None, ""):
        return ask
    bid = Decimal(str(best_bid))
    return (bid + ask) / 2 if Decimal("0") < bid < ask else ask


def _pairs_by_source(store: Store, sport: str, fixture_dir: Path | None) -> dict[str, list[tuple[float, bool]]]:
    """(prob, won) pairs per source ∈ {consensus, kalshi_mid, polymarket_mid} for settled events."""
    with store.connect() as con:
        quote_rows = [dict(r) for r in con.execute(
            "SELECT event_key, venue, book, outcome, cost, best_bid, fetched_at, sport "
            "FROM quotes WHERE market_type='WINNER' AND live=0 AND sport=? "
            "AND venue IN ('kalshi','polymarket','oddsapi')", (sport,))]
        consensus_rows = [dict(r) for r in con.execute(
            "SELECT event_key, outcome, p_hat FROM consensus")]
    if not quote_rows:
        return {}
    scores_for = _mlb_scores_resolver(quote_rows, load_aliases(), fixture_dir)

    out: dict[str, list[tuple[float, bool]]] = defaultdict(list)

    def _won(event_key: str, outcome: str) -> bool | None:
        scores = scores_for(event_key)
        if scores is None:
            return None
        best = max(scores.values())
        winners = [team for team, s in scores.items() if s == best]
        return outcome == winners[0] if len(winners) == 1 else None

    # consensus
    for row in consensus_rows:
        won = _won(row["event_key"], row["outcome"])
        if won is not None:
            out["consensus"].append((float(Decimal(str(row["p_hat"]))), won))
    # venue mids — one closing quote per (event, venue, book, outcome)
    closing: dict[tuple, dict] = {}
    for row in quote_rows:
        if row["venue"] == "oddsapi":
            continue
        key = (row["event_key"], row["venue"], row["book"], row["outcome"])
        if key not in closing or row["fetched_at"] > closing[key]["fetched_at"]:
            closing[key] = row
    for row in closing.values():
        mid = _mid(row["cost"], row["best_bid"])
        won = _won(row["event_key"], row["outcome"])
        if mid is not None and 0 < float(mid) < 1 and won is not None:
            out[f"{row['venue']}_mid"].append((float(mid), won))
    return out


def score_sources(store: Store, sport: str = "baseball_mlb",
                  fixture_dir: Path | None = None) -> list[SourceScore]:
    scored: list[SourceScore] = []
    for source, pairs in sorted(_pairs_by_source(store, sport, fixture_dir).items()):
        n, b, ll, ece = score_pairs(pairs)
        scored.append(SourceScore(source, sport, n, b, ll, ece))
    return scored


def _wilson_ci(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a win rate — honest small-N bounds (27 bets is small)."""
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (round(center - half, 4), round(center + half, 4))


def ev_subset_test(store: Store) -> dict:
    """On settled EV bets (annotated loop rows excluded), did the outcome favor the VENUE side
    (bet WON) or the CONSENSUS side? A venue win rate <= 0.5 means flags are anti-edge."""
    from arb.experimental.annotations import annotated_ids
    with store.connect() as con:
        excluded = annotated_ids(con)
        rows = [r for r in con.execute(
            "SELECT id, result FROM flat_bets WHERE kind='EV' AND status='SETTLED' "
            "AND result IN ('WON','LOST')")
            if int(r["id"]) not in excluded]
    wins = sum(1 for r in rows if r["result"] == "WON")
    n = len(rows)
    lo, hi = _wilson_ci(wins, n)
    return {"n": n, "venue_wins": wins, "venue_win_rate": round(wins / n, 4) if n else None,
            "ci95": (lo, hi)}


def rolling_brier(store: Store, days: int = 30, sport: str = "baseball_mlb",
                  fixture_dir: Path | None = None) -> dict[str, float | int]:
    """One-line premise monitor for the daily report: consensus vs best venue mid Brier."""
    pairs = _pairs_by_source(store, sport, fixture_dir)
    def mean_brier(source):
        ps = pairs.get(source, [])
        return round(sum(brier(p, 1 if w else 0) for p, w in ps) / len(ps), 6) if ps else None
    venue_briers = [mean_brier(s) for s in ("kalshi_mid", "polymarket_mid") if mean_brier(s) is not None]
    return {"consensus_brier": mean_brier("consensus"),
            "venue_brier": min(venue_briers) if venue_briers else None,
            "n_consensus": len(pairs.get("consensus", []))}


def format_regrade(sources: list[SourceScore], ev_subset: dict) -> str:
    lines = ["Source forecaster scores (lower Brier/log-loss = sharper):",
             f"  {'source':<16}{'sport':<14}{'n':>5}{'brier':>10}{'logloss':>10}{'ece':>8}"]
    for s in sorted(sources, key=lambda s: (s.sport, s.brier)):
        lines.append(f"  {s.source:<16}{s.sport:<14}{s.n:>5}{s.brier:>10.4f}{s.log_loss:>10.4f}{s.ece:>8.4f}")
    lines.append("")
    ev = ev_subset
    lines.append(f"EV-subset test: venue side won {ev['venue_wins']}/{ev['n']} "
                 f"(win rate {ev['venue_win_rate']}, 95% CI {ev['ci95']})")
    if ev["n"] == 0:
        lines.append("  (no clean settled EV bets yet — verdict pending; loop rows excluded)")
    elif ev["venue_win_rate"] is not None and ev["ci95"][1] < 0.5:
        lines.append("  DECISION: venue side beats consensus (CI upper < 0.5) — EV lane sign is inverted (Phase A rule).")
    return "\n".join(lines)
