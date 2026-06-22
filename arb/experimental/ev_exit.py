"""EV take-profit watcher.

This is not order execution. It appends an EXITED/SOLD paper row when an open EV bet
could be closed at the current executable bid after sell-side friction. The original
bet then stops being treated as open, and a later cheaper ask can re-open the same
fingerprint through the normal tracker path.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable

import requests

from arb.pricing import all_in_fee_cost, consensus_outcome_key, money, q6
from arb.store import Store
from arb.venues.kalshi import BASE_URL as KALSHI_URL
from arb.venues.polymarket import CLOB_URL

HEADERS = {"User-Agent": "arb-paper-scanner/0.1 (public read-only)"}
ExitFetcher = Callable[[dict[str, Any]], tuple[Decimal, Decimal | None] | None]


def open_ev_exit_candidates(store: Store, now: datetime, cfg: dict[str, Any]) -> list[sqlite3.Row]:
    cutoff = (now - timedelta(hours=int(cfg.get("max_bet_age_hours", 72)))).isoformat()
    include_micro = bool(cfg.get("include_micro", True))
    quality_clause = "(b.quality_flag IS NULL OR b.quality_flag = 'micro')" if include_micro else "b.quality_flag IS NULL"
    with store.connect() as con:
        return list(
            con.execute(
                f"""
                SELECT b.*,
                       (SELECT confirmed_cost FROM flat_bets c
                         WHERE c.reappearance_of = b.id AND c.status = 'CONFIRMED'
                         ORDER BY c.id LIMIT 1) confirmed_entry_cost
                  FROM flat_bets b
                 WHERE b.kind = 'EV'
                   AND b.status = 'OPEN'
                   AND b.placed_at >= ?
                   AND {quality_clause}
                   AND b.notes NOT LIKE '%closing-line%'
                   AND NOT EXISTS (
                       SELECT 1 FROM flat_bets x
                        WHERE x.reappearance_of = b.id
                          AND x.status IN ('SETTLED','CLOSED','EXITED')
                   )
                 ORDER BY b.id
                """,
                (cutoff,),
            )
        )


def run_ev_exit_once(
    store: Store,
    config,
    cfg: dict[str, Any],
    fetchers: dict[str, ExitFetcher] | None = None,
    now: datetime | None = None,
) -> int:
    if not bool(cfg.get("enabled", True)):
        return 0
    now = now or datetime.now(timezone.utc)
    fetchers = fetchers if fetchers is not None else live_fetchers(config)
    consensus = store.load_consensus()
    max_age = timedelta(hours=int(config.consensus_max_age_hours))
    exited = 0
    with store.connect() as con:
        for bet in open_ev_exit_candidates(store, now, cfg):
            decision = exit_decision(bet, consensus, max_age, fetchers, cfg, now)
            if decision is None:
                continue
            _append_exit_row(con, bet, decision, now)
            exited += 1
    return exited


def exit_decision(
    bet: sqlite3.Row,
    consensus: dict[tuple[str, str], tuple[Decimal, datetime]],
    max_consensus_age: timedelta,
    fetchers: dict[str, ExitFetcher],
    cfg: dict[str, Any],
    now: datetime,
) -> dict[str, Decimal | str] | None:
    legs = json.loads(bet["legs_json"] or "[]")
    if len(legs) != 1:
        return None
    leg = legs[0]
    start = _parse_start(leg)
    if start is not None and start <= now:
        return None
    fetcher = fetchers.get(str(leg.get("venue")))
    if fetcher is None:
        return None
    current = fetcher(leg)
    if current is None:
        return None
    exit_bid, exit_depth = current
    entry_cost = Decimal(str(bet["confirmed_entry_cost"] or bet["optimistic_cost"]))
    if entry_cost <= 0 or exit_bid <= 0:
        return None
    stake = Decimal(str(bet["stake_usd"]))
    min_depth = Decimal(str(cfg.get("min_exit_depth_usd", "5")))
    if exit_depth is not None and exit_depth < min(min_depth, stake):
        return None
    profit = money(stake * (exit_bid / entry_cost - Decimal("1")))
    roi = q6(profit / stake) if stake > 0 else Decimal("0")
    if profit < Decimal(str(cfg.get("min_profit_usd", "0.05"))):
        return None
    if roi < Decimal(str(cfg.get("min_profit_roi", "0.03"))):
        return None

    outcome = consensus_outcome_key(str(leg.get("market_type")), _decimal_or_none(leg.get("line")), str(leg.get("outcome_label")))
    entry = consensus.get((str(leg.get("event_key")), outcome))
    if entry is None:
        if bool(cfg.get("require_fair_value", True)):
            return None
        remaining_edge = Decimal("0")
    else:
        p_hat, computed_at = entry
        if now - computed_at.astimezone(timezone.utc) > max_consensus_age:
            return None
        remaining_edge = q6(p_hat - exit_bid)
    if remaining_edge > Decimal(str(cfg.get("exit_edge_ceiling", "0.01"))):
        return None

    return {
        "entry_cost": q6(entry_cost),
        "exit_bid": q6(exit_bid),
        "exit_depth": "" if exit_depth is None else str(q6(exit_depth)),
        "profit": profit,
        "roi": roi,
        "remaining_edge": remaining_edge,
    }


def _append_exit_row(con: sqlite3.Connection, bet: sqlite3.Row, decision: dict[str, Decimal | str], now: datetime) -> None:
    note = (
        "ev-exit: take-profit; "
        f"entry={decision['entry_cost']} exit_bid={decision['exit_bid']} "
        f"roi={decision['roi']} remaining_edge={decision['remaining_edge']} "
        f"exit_depth={decision['exit_depth']}"
    )
    con.execute(
        """
        INSERT INTO flat_bets(fingerprint, reappearance_of, opened_poll_id, category, sport, kind, trusted,
                              live, edge, stake_usd, legs_json, optimistic_cost, confirmed_cost,
                              pnl_optimistic, pnl_confirmed, status, placed_at, settled_at, result, notes,
                              quality_flag, kelly_f_star, kelly_stake_usd,
                              validation_rule, validation_tag)
        VALUES (?, ?, ?, ?, ?, 'EV', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'EXITED', ?, ?, 'SOLD', ?,
                ?, ?, ?, ?, ?)
        """,
        (
            bet["fingerprint"], bet["id"], bet["opened_poll_id"], bet["category"], bet["sport"],
            bet["trusted"], bet["live"], bet["edge"], bet["stake_usd"], bet["legs_json"],
            str(decision["entry_cost"]), str(decision["exit_bid"]),
            str(decision["profit"]), str(decision["profit"]),
            bet["placed_at"], now.isoformat(), note, bet["quality_flag"],
            bet["kelly_f_star"], bet["kelly_stake_usd"], bet["validation_rule"], bet["validation_tag"],
        ),
    )


def live_fetchers(config) -> dict[str, ExitFetcher]:
    return {
        "kalshi": lambda leg: _kalshi_current_bid(leg, config.kalshi_fee_rate, config.taker_price_buffer),
        "polymarket": lambda leg: _polymarket_current_bid(leg, config.polymarket_sports_fee_rate, config.taker_price_buffer),
    }


def _kalshi_current_bid(leg: dict[str, Any], fee_rate: Decimal, taker_buffer: Decimal) -> tuple[Decimal, Decimal | None] | None:
    response = requests.get(f"{KALSHI_URL}/markets/{leg['book']}/orderbook", timeout=30, headers=HEADERS)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    orderbook = response.json()
    book = orderbook.get("orderbook_fp") or orderbook.get("orderbook") or {}
    side = "no" if leg.get("market_type") == "TOTAL" and leg.get("outcome_label") == "UNDER" else "yes"
    levels = book.get(f"{side}_dollars") or []
    parsed = [(Decimal(str(level[0])), Decimal(str(level[1]))) for level in levels]
    parsed = [(price, size) for price, size in parsed if price > 0 and size > 0]
    if not parsed:
        return None
    bid, size = max(parsed, key=lambda level: level[0])
    net = q6(bid - all_in_fee_cost(bid, fee_rate, taker_buffer, basis="quadratic"))
    return (net, q6(bid * size)) if net > 0 else None


def _polymarket_current_bid(leg: dict[str, Any], fee_rate: Decimal, taker_buffer: Decimal) -> tuple[Decimal, Decimal | None] | None:
    response = requests.get(f"{CLOB_URL}/book", params={"token_id": leg["book"]}, timeout=30, headers=HEADERS)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    best = _best_bid(response.json().get("bids", []))
    if best is None:
        return None
    bid, depth = best
    net = q6(bid - all_in_fee_cost(bid, fee_rate, taker_buffer, basis="linear"))
    return (net, depth) if net > 0 else None


def _best_bid(levels: list[dict[str, Any]]) -> tuple[Decimal, Decimal] | None:
    parsed = [(Decimal(str(level["price"])), Decimal(str(level["size"]))) for level in levels]
    parsed = [(price, size) for price, size in parsed if price > 0 and size > 0]
    if not parsed:
        return None
    price, size = max(parsed, key=lambda level: level[0])
    return price, q6(price * size)


def _parse_start(leg: dict[str, Any]) -> datetime | None:
    raw = leg.get("start_utc")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _decimal_or_none(value: Any) -> Decimal | None:
    return None if value in (None, "") else Decimal(str(value))
