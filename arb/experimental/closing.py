"""Closing-line EV recorder — runs as its own process, never touches the ARB daemon.

User methodology: compare venue prices against sportsbook consensus ONLY in the final
minutes before a game starts (the closing line is the books' sharpest number), and use
each sportsbook snapshot exactly once per event. The consensus table row is the
used-once guard: once an event has consensus stored, it is never snapshotted again.

Venue prices come from the quotes the main daemon already persists every sweep; quotes
older than `max_quote_age_secs` are rejected rather than compared against a fresh line.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from arb.cli import compute_consensus
from arb.models import Quote, opportunity_fingerprint
from arb.pricing import consensus_outcome_key, expected_value, q6
from arb.store import Store
from arb.venues.oddsapi import OddsApiAdapter
from arb.venues.sx import SxAdapter


def fetch_sx_schedule(config, sports: list[str]) -> dict[str, tuple[str, datetime]]:
    """event_key -> (sport, start_utc) from SX's free active-markets feed."""
    adapter = SxAdapter(config)
    schedule: dict[str, tuple[str, datetime]] = {}
    for sport in sports:
        for quote in adapter.fetch_quotes(sport):
            if quote.start_utc is not None:
                schedule[quote.event_key] = (sport, quote.start_utc)
    return schedule


def events_in_window(
    schedule: dict[str, tuple[str, datetime]],
    now: datetime,
    window_open_mins: int,
    window_close_mins: int,
) -> dict[str, tuple[str, datetime]]:
    opens = now + timedelta(minutes=window_close_mins)
    closes = now + timedelta(minutes=window_open_mins)
    return {key: (sport, start) for key, (sport, start) in schedule.items() if opens <= start <= closes}


def unused_events(store: Store, targets: dict[str, tuple[str, datetime]]) -> dict[str, tuple[str, datetime]]:
    """Drop events whose sportsbook snapshot was already taken (use-once rule)."""
    if not targets:
        return {}
    with store.connect() as con:
        seen = {
            row["event_key"]
            for row in con.execute(
                f"SELECT DISTINCT event_key FROM consensus WHERE event_key IN ({','.join('?' * len(targets))})",
                list(targets),
            )
        }
    return {key: value for key, value in targets.items() if key not in seen}


def latest_quotes(store: Store, event_keys: list[str], now: datetime, max_age_secs: int) -> list[Quote]:
    """Rebuild Quote objects from the freshest stored venue rows per (venue, book, outcome)."""
    if not event_keys:
        return []
    cutoff = (now - timedelta(seconds=max_age_secs)).isoformat()
    with store.connect() as con:
        rows = list(
            con.execute(
                f"""
                SELECT * FROM quotes
                 WHERE event_key IN ({','.join('?' * len(event_keys))})
                   AND live = 0 AND venue != 'oddsapi' AND fetched_at >= ?
                 ORDER BY id
                """,
                [*event_keys, cutoff],
            )
        )
    freshest: dict[tuple[str, str, str], Any] = {}
    for row in rows:  # later rows overwrite earlier ones — keep the newest per book/outcome
        freshest[(row["venue"], row["book"], row["outcome"])] = row
    return [_row_to_quote(row) for row in freshest.values()]


def record_closing_bets(
    store: Store,
    config,
    closing_cfg: dict[str, Any],
    now: datetime | None = None,
    schedule: dict[str, tuple[str, datetime]] | None = None,
) -> int:
    """One pass: snapshot consensus for window events and record EV > 0 closing bets."""
    now = now or datetime.now(timezone.utc)
    if schedule is None:
        schedule = fetch_sx_schedule(config, list(closing_cfg["sports"]))
    targets = unused_events(
        store,
        events_in_window(schedule, now, int(closing_cfg["window_open_mins"]), int(closing_cfg["window_close_mins"])),
    )
    if not targets:
        return 0

    adapter = OddsApiAdapter(config)
    consensus: dict[tuple[str, str], Decimal] = {}
    for sport in sorted({sport for sport, _ in targets.values()}):
        odds_quotes = adapter.fetch_quotes(sport)  # one snapshot per start-wave (2 credits)
        sport_keys = [key for key, (s, _) in targets.items() if s == sport]
        consensus.update(
            (pair, p_hat)
            for pair, p_hat in compute_consensus(odds_quotes, config.min_books_for_consensus, config.min_books_for_consensus_totals).items()
            if pair[0] in sport_keys
        )
    if not consensus:
        return 0
    store.save_consensus(consensus, now)  # marks every snapshotted event as used

    venue_quotes = latest_quotes(store, list(targets), now, int(closing_cfg["max_quote_age_secs"]))
    # One bet per consensus number: across venues quoting the same outcome, only the
    # single cheapest effective price is bet — never several stakes on one opinion.
    best_per_outcome: dict[tuple[str, str], Quote] = {}
    for quote in venue_quotes:
        key = (quote.event_key, consensus_outcome_key(quote.market_type, quote.line, quote.outcome_label))
        current = best_per_outcome.get(key)
        if current is None or quote.effective_cost < current.effective_cost:
            best_per_outcome[key] = quote
    recorded = 0
    poll_id: int | None = None
    with store.connect() as con:
        for key, quote in best_per_outcome.items():
            p_hat = consensus.get(key)
            if p_hat is None:
                continue
            ev = expected_value(p_hat, quote.effective_cost)
            if ev <= 0:
                continue
            if poll_id is None:
                poll_id = store.start_poll("closing")
            _insert_closing_bet(con, poll_id, quote, ev, now)
            recorded += 1
    if poll_id is not None:
        store.finish_poll(poll_id, adapter.credits_remaining, len(venue_quotes), recorded)
    return recorded


def _insert_closing_bet(con, poll_id: int, quote: Quote, edge: Decimal, now: datetime) -> None:
    # SPEC §3.3: SX settlement rules differ — closing bets on SX stay untrusted too.
    trusted = int(quote.venue != "sx")
    con.execute(
        """
        INSERT INTO flat_bets(fingerprint, reappearance_of, opened_poll_id, category, sport, kind, trusted,
                              live, edge, stake_usd, legs_json, optimistic_cost, pnl_optimistic,
                              status, placed_at, notes)
        VALUES (?, NULL, ?, 'sports', ?, 'EV', ?, 0, ?, '1.00', ?, ?, NULL, 'OPEN', ?, 'closing-line')
        """,
        (
            opportunity_fingerprint("EV", quote.event_key, [quote]),
            poll_id,
            quote.sport,
            trusted,
            str(edge),
            json.dumps([_quote_json(quote)]),
            str(q6(quote.effective_cost)),
            now.isoformat(),
        ),
    )


def _row_to_quote(row) -> Quote:
    return Quote(
        event_key=row["event_key"],
        venue=row["venue"],
        book=row["book"],
        market_type=row["market_type"],
        line=None if row["line"] is None else Decimal(row["line"]),
        outcome_label=row["outcome"],
        raw_price=Decimal(row["raw_price"]),
        cost=Decimal(row["cost"]),
        fee_cost=Decimal(row["fee_cost"]),
        depth_usd=None if row["depth_usd"] is None else Decimal(row["depth_usd"]),
        live=bool(row["live"]),
        fetched_at=datetime.fromisoformat(row["fetched_at"]),
    )


def _quote_json(quote: Quote) -> dict[str, str | None]:
    from arb.store import quote_to_json

    return quote_to_json(quote)
