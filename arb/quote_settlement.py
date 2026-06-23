"""Population favorite-longshot sample — settle PERSISTED pregame quotes against final
scores (batch-2 #2).

The flagged-bet sample in bias.py is selection-biased (only prices that cleared the EV/arb
filters). This pass instead reads the quotes we persist for every scan (rule #7) and joins
them to final scores, producing (implied_prob, won) pairs across the WHOLE price range — the
representative cross-section a real FLB curve needs. Three validity constraints, all enforced
here:

  1. Bin on the DE-VIGGED implied probability (raw cost renormalized per two-sided book),
     never the fee-loaded optimistic_cost — a cost-based x-axis makes fees masquerade as the
     longshot premium.
  2. One observation per (event, venue, book, outcome): the CLOSING (latest pregame) quote,
     so the curve is weighted by event, not by polling frequency.
  3. Moneyline binaries only (market_type='WINNER'); totals drag in line-matching and pushes.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from .matching import load_aliases, normalize_name
from .settlement import _leg_result
from .store import Store
from .venues.mlbstats import MlbStatsAdapter

ScoresFor = Callable[[str], dict[str, Decimal] | None]


def quote_outcomes(rows: list[dict[str, Any]], scores_for: ScoresFor, aliases: dict) -> list[tuple[float, bool]]:
    """Core (HTTP-free, testable): persisted WINNER quote rows + a score resolver ->
    (de-vigged implied prob, won) pairs. Each row is a dict with event_key, venue, book,
    outcome, cost, fetched_at, sport."""
    # #2 dedup to the closing quote per (event, venue, book, outcome).
    closing: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["event_key"], row["venue"], row["book"], row["outcome"])
        prev = closing.get(key)
        if prev is None or str(row["fetched_at"]) > str(prev["fetched_at"]):
            closing[key] = row

    # Regroup the closing quotes into two-sided books so we can de-vig.
    books: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for (event_key, venue, book, _outcome), row in closing.items():
        books[(event_key, venue, book)].append(row)

    out: list[tuple[float, bool]] = []
    for (event_key, _venue, _book), outcomes in books.items():
        if len(outcomes) != 2:
            continue  # #1 can't de-vig a one-sided book (e.g. a lone Kalshi market)
        total = sum((Decimal(str(o["cost"])) for o in outcomes), Decimal("0"))
        if total <= 0:
            continue
        scores = scores_for(event_key)
        if scores is None:
            continue
        sport = str(outcomes[0]["sport"])
        for o in outcomes:
            devigged = Decimal(str(o["cost"])) / total  # #1 renormalize out the book's vig
            leg = {
                "outcome_label": normalize_name(str(o["outcome"]), sport, aliases),
                "market_type": "WINNER",
                "line": None,
            }
            result = _leg_result(leg, scores)
            if result not in ("WON", "LOST"):
                continue  # skip VOID/unresolvable; moneyline has no pushes
            if 0.0 < float(devigged) < 1.0:
                out.append((float(devigged), result == "WON"))
    return out


def population_binaries(store: Store, fixture_dir: Path | None = None, sport: str = "baseball_mlb") -> list[tuple[float, bool]]:
    """Settle persisted pregame moneyline quotes for `sport` against MLB Stats finals
    (keyless, unlimited — safe outside tests/loops). Non-MLB sports return [] until a keyless
    score source exists for them."""
    if sport != "baseball_mlb":
        return []
    with store.connect() as con:
        rows = [
            dict(r)
            for r in con.execute(
                """SELECT event_key, venue, book, outcome, cost, fetched_at, sport
                     FROM quotes
                    WHERE market_type = 'WINNER' AND live = 0 AND sport = ?""",
                (sport,),
            )
        ]
    if not rows:
        return []
    aliases = load_aliases()
    scores_for = _mlb_scores_resolver(rows, aliases, fixture_dir)
    return quote_outcomes(rows, scores_for, aliases)


def _mlb_scores_resolver(rows: list[dict[str, Any]], aliases: dict, fixture_dir: Path | None) -> ScoresFor:
    needed: set[str] = set()
    for row in rows:
        parts = str(row["event_key"]).split(":")
        if len(parts) >= 2:
            needed.add(parts[1])
    fetch_dates: set[str] = set()
    for d in needed:
        try:
            base = date.fromisoformat(d)
        except ValueError:
            continue
        for delta in (-1, 0, 1):
            fetch_dates.add((base + timedelta(days=delta)).isoformat())

    adapter = MlbStatsAdapter(fixture_dir)
    date_pair_index: dict[tuple[str, frozenset], list[dict[str, Decimal]]] = defaultdict(list)
    for fetch_date in sorted(fetch_dates):
        try:
            games = adapter.final_games(fetch_date)
        except FileNotFoundError:
            continue
        for game in games:
            nh = normalize_name(game["home"], "baseball_mlb", aliases)
            na = normalize_name(game["away"], "baseball_mlb", aliases)
            scores = {nh: Decimal(str(game["home_score"])), na: Decimal(str(game["away_score"]))}
            date_pair_index[(fetch_date, frozenset({nh, na}))].append(scores)

    def scores_for(event_key: str) -> dict[str, Decimal] | None:
        parts = event_key.split(":")
        if len(parts) < 4:
            return None
        pair = frozenset({parts[2], parts[3]})
        base = date.fromisoformat(parts[1]) if _is_iso_date(parts[1]) else None
        if base is None:
            return None
        candidates: list[dict[str, Decimal]] = []
        for delta in (0, -1, 1):
            candidates.extend(date_pair_index.get(((base + timedelta(days=delta)).isoformat(), pair), []))
        unique = _dedup_scores(candidates)
        return unique[0] if len(unique) == 1 else None  # ambiguous doubleheaders -> skip

    return scores_for


def _dedup_scores(candidates: list[dict[str, Decimal]]) -> list[dict[str, Decimal]]:
    seen: list[dict[str, Decimal]] = []
    for c in candidates:
        if c not in seen:
            seen.append(c)
    return seen


def _is_iso_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        return False
