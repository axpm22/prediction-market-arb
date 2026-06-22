from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .matching import event_key, load_aliases, normalize_name
from .pricing import money
from .store import Store
from .venues.mlbstats import MlbStatsAdapter
from .venues.oddsapi import OddsApiAdapter


def settle_sports(store: Store, config, fixture_dir: Path | None = None) -> int:
    """Resolve open flat bets against final scores; append SETTLED rows, never edit.

    ARB bets: the locked-at-entry P&L is PROVISIONAL. When scores show the event
    completed, a SETTLED row realizes it at the same value — venue-resolution
    cross-checking (which is what can make realized differ) appends corrections later.
    """
    bets = [*_open_ev_bets(store), *_open_arb_bets(store)]
    if not bets:
        return 0
    adapter = OddsApiAdapter(config, fixture_dir)
    results: dict[str, dict[str, Decimal]] = {}
    for sport in sorted({str(bet["sport"]) for bet in bets}):
        try:
            events = adapter.fetch_scores(sport, config.settle_days_from)
        except FileNotFoundError:
            continue
        results.update(score_results(events, sport))
    settled = 0
    with store.connect() as con:
        for bet in bets:
            leg = json.loads(bet["legs_json"])[0]
            scores = results.get(str(leg["event_key"]))
            if scores is None:
                continue
            if bet["kind"] == "ARB":
                _append_arb_resolved_row(con, bet)
                settled += 1
                continue
            result = _leg_result(leg, scores)
            if result is None:
                continue
            _append_settled_row(con, bet, result, "score-settled")
            settled += 1
    return settled


def settle_backfill_mlb(store: Store, fixture_dir: Path | None = None) -> int:
    """Resolve open MLB EV/ARB bets against the MLB Stats API (no lookback window).

    Rescues the backlog the Odds API's 3-day window orphaned. Matches each open bet to a
    final game by (UTC date, the two normalized team names) parsed straight from the
    event_key — so it does not depend on start-time buckets. Doubleheaders (two finals,
    same date + same pair) are left OPEN as ambiguous rather than risk settling the wrong
    game. Append-only and idempotent: bets already SETTLED are skipped by the open-bet
    queries; re-running is a no-op.
    """
    bets = [b for b in (*_open_ev_bets(store), *_open_arb_bets(store))
            if str(b["sport"]) == "baseball_mlb"]
    if not bets:
        return 0
    aliases = load_aliases()
    adapter = MlbStatsAdapter(fixture_dir)

    # Which UTC dates do we need? Parse them from the event_keys, and fetch a +/-1 day
    # window around each so a late game logged under a different schedule date is caught.
    needed: set[str] = set()
    for bet in bets:
        parts = str(json.loads(bet["legs_json"])[0]["event_key"]).split(":")
        if len(parts) >= 2:
            needed.add(parts[1])
    fetch_dates: set[str] = set()
    for d in needed:
        base = date.fromisoformat(d)
        for delta in (-1, 0, 1):
            fetch_dates.add((base + timedelta(days=delta)).isoformat())

    # Index final games by (utc_date, frozenset(normalized names)) -> list of score dicts.
    index: dict[tuple[str, frozenset], list[dict[str, Decimal]]] = {}
    for fetch_date in sorted(fetch_dates):
        try:
            games = adapter.final_games(fetch_date)
        except FileNotFoundError:
            continue
        for game in games:
            nh = normalize_name(game["home"], "baseball_mlb", aliases)
            na = normalize_name(game["away"], "baseball_mlb", aliases)
            gd = game.get("game_date")
            if gd:
                utc_date = datetime.fromisoformat(str(gd).replace("Z", "+00:00")).astimezone(timezone.utc).date().isoformat()
            else:
                utc_date = fetch_date
            scores = {nh: Decimal(str(game["home_score"])), na: Decimal(str(game["away_score"]))}
            index.setdefault((utc_date, frozenset({nh, na})), []).append(scores)

    settled = 0
    with store.connect() as con:
        for bet in bets:
            leg = json.loads(bet["legs_json"])[0]
            parts = str(leg["event_key"]).split(":")
            if len(parts) < 4:
                continue
            bucket = index.get((parts[1], frozenset({parts[2], parts[3]})))
            if not bucket or len(bucket) != 1:  # 0 = no final yet; >1 = doubleheader ambiguity
                continue
            scores = bucket[0]
            if bet["kind"] == "ARB":
                _append_arb_resolved_row(con, bet)
                settled += 1
                continue
            result = _leg_result(leg, scores)
            if result is None:
                continue
            _append_settled_row(con, bet, result, "mlbstats-settled")
            settled += 1
    return settled


def _open_arb_bets(store: Store) -> list[sqlite3.Row]:
    with store.connect() as con:
        return list(
            con.execute(
                """
                SELECT * FROM flat_bets b
                 WHERE b.kind = 'ARB' AND b.status = 'OPEN'
                   AND NOT EXISTS (SELECT 1 FROM flat_bets s
                                    WHERE s.status = 'SETTLED' AND s.reappearance_of = b.id)
                 ORDER BY b.id
                """
            )
        )


def _append_arb_resolved_row(con: sqlite3.Connection, bet: sqlite3.Row) -> None:
    # Realized = provisional unless the venues resolved differently; scores alone cannot
    # see that, so the row is marked pending venue-resolution cross-check (corrections
    # append under the same bet id when that lands).
    con.execute(
        """
        INSERT INTO flat_bets(fingerprint, reappearance_of, opened_poll_id, category, sport, kind, trusted,
                              live, edge, stake_usd, legs_json, optimistic_cost, pnl_optimistic, pnl_confirmed,
                              pnl_kelly_optimistic, pnl_kelly_confirmed, status, placed_at, settled_at,
                              result, notes, quality_flag, kelly_f_star, kelly_stake_usd,
                              validation_rule, validation_tag)
        VALUES (?, ?, ?, ?, ?, 'ARB', ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 'SETTLED', ?, ?, 'RESOLVED',
                ?, ?, ?, ?, ?, ?)
        """,
        (
            bet["fingerprint"], bet["id"], bet["opened_poll_id"], bet["category"], bet["sport"],
            bet["trusted"], bet["live"], bet["edge"], bet["stake_usd"], bet["legs_json"],
            bet["optimistic_cost"], bet["pnl_optimistic"], bet["pnl_confirmed"],
            bet["placed_at"], datetime.now(timezone.utc).isoformat(),
            "score-settled; realized=provisional; venue-resolution-cross-check-pending",
            bet["quality_flag"], bet["kelly_f_star"], bet["kelly_stake_usd"],
            bet["validation_rule"], bet["validation_tag"],
        ),
    )


def settle_manual(store: Store, target_event_key: str, winning_outcome: str) -> int:
    """SPEC §9: non-sports (and anything scores can't resolve) settles by hand."""
    settled = 0
    with store.connect() as con:
        for bet in _open_ev_bets(store):
            leg = json.loads(bet["legs_json"])[0]
            if str(leg["event_key"]) != target_event_key:
                continue
            result = "WON" if str(leg["outcome_label"]) == winning_outcome else "LOST"
            _append_settled_row(con, bet, result, "manual-settled")
            settled += 1
    return settled


def score_results(events: list[dict[str, Any]], sport: str) -> dict[str, dict[str, Decimal]]:
    aliases = load_aliases()
    results: dict[str, dict[str, Decimal]] = {}
    for event in events:
        if not event.get("completed"):
            continue
        scores = {
            normalize_name(str(entry["name"]), sport, aliases): Decimal(str(entry["score"]))
            for entry in event.get("scores") or []
        }
        if len(scores) != 2:
            continue
        start = datetime.fromisoformat(str(event["commence_time"]).replace("Z", "+00:00"))
        key = event_key(sport, str(event["home_team"]), str(event["away_team"]), start, aliases)
        results[key] = scores
    return results


def _leg_result(leg: dict[str, Any], scores: dict[str, Decimal]) -> str | None:
    label = str(leg["outcome_label"])
    if leg.get("market_type") == "TOTAL":
        if leg.get("line") is None:
            return None
        total = sum(scores.values(), Decimal("0"))
        line = Decimal(str(leg["line"]))
        if total == line:  # push on integer lines
            return "VOID"
        over_won = total > line
        return "WON" if (label == "OVER") is over_won else "LOST"
    best = max(scores.values())
    winners = [team for team, score in scores.items() if score == best]
    if len(winners) != 1:  # tie — postponement/void rules differ per venue, leave pending
        return None
    return "WON" if winners[0] == label else "LOST"


def _open_ev_bets(store: Store) -> list[sqlite3.Row]:
    with store.connect() as con:
        return list(
            con.execute(
                """
                SELECT * FROM flat_bets b
                 WHERE b.kind = 'EV' AND b.status = 'OPEN'
                   AND NOT EXISTS (SELECT 1 FROM flat_bets s
                                    WHERE s.status = 'SETTLED' AND s.reappearance_of = b.id)
                 ORDER BY b.id
                """
            )
        )


def _append_settled_row(con: sqlite3.Connection, bet: sqlite3.Row, result: str, note: str) -> None:
    confirmed = con.execute(
        """SELECT confirmed_cost, validation_rule, validation_tag
             FROM flat_bets
            WHERE status = 'CONFIRMED' AND reappearance_of = ?
            ORDER BY id LIMIT 1""",
        (bet["id"],),
    ).fetchone()
    pnl_optimistic = _flat_pnl(bet["optimistic_cost"], result)
    pnl_confirmed = _flat_pnl(confirmed["confirmed_cost"], result) if confirmed else None
    pnl_kelly_optimistic = _sized_pnl(bet["optimistic_cost"], result, bet["kelly_stake_usd"])
    pnl_kelly_confirmed = _sized_pnl(confirmed["confirmed_cost"], result, bet["kelly_stake_usd"]) if confirmed else None
    validation_rule = confirmed["validation_rule"] if confirmed and confirmed["validation_rule"] is not None else bet["validation_rule"]
    validation_tag = confirmed["validation_tag"] if confirmed and confirmed["validation_tag"] is not None else bet["validation_tag"]
    con.execute(
        """
        INSERT INTO flat_bets(fingerprint, reappearance_of, opened_poll_id, category, sport, kind, trusted,
                              live, edge, stake_usd, legs_json, optimistic_cost, confirmed_cost,
                              pnl_optimistic, pnl_confirmed, pnl_kelly_optimistic, pnl_kelly_confirmed,
                              status, placed_at, settled_at, result, notes,
                              quality_flag, kelly_f_star, kelly_stake_usd,
                              validation_rule, validation_tag)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'SETTLED', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            bet["fingerprint"],
            bet["id"],
            bet["opened_poll_id"],
            bet["category"],
            bet["sport"],
            bet["kind"],
            bet["trusted"],
            bet["live"],
            bet["edge"],
            bet["stake_usd"],
            bet["legs_json"],
            bet["optimistic_cost"],
            None if not confirmed else confirmed["confirmed_cost"],
            None if pnl_optimistic is None else str(pnl_optimistic),
            None if pnl_confirmed is None else str(pnl_confirmed),
            None if pnl_kelly_optimistic is None else str(pnl_kelly_optimistic),
            None if pnl_kelly_confirmed is None else str(pnl_kelly_confirmed),
            # P&L attributes to the day the bet was placed (SPEC §9); report restates that day.
            bet["placed_at"],
            datetime.now(timezone.utc).isoformat(),
            result,
            note,
            bet["quality_flag"],
            bet["kelly_f_star"],
            bet["kelly_stake_usd"],
            validation_rule,
            validation_tag,
        ),
    )


def _flat_pnl(effective_cost: str | None, result: str) -> Decimal | None:
    """$1 flat stake at effective cost c: win pays (1-c)/c, loss pays -1, void pays 0."""
    return _sized_pnl(effective_cost, result, Decimal("1.00"))


def _sized_pnl(effective_cost: str | None, result: str, stake_usd: str | Decimal | None) -> Decimal | None:
    """Stake-sized P&L at effective cost c; $1 stake is the flat-bet special case."""
    if effective_cost is None or stake_usd in (None, ""):
        return None
    stake = Decimal(str(stake_usd))
    if stake <= 0:
        return Decimal("0.00")
    if result == "VOID":
        return Decimal("0.00")
    if result == "LOST":
        return money(-stake)
    cost = Decimal(effective_cost)
    if cost <= 0:
        return None
    return money(stake * (Decimal("1") - cost) / cost)
