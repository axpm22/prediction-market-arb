from argparse import Namespace
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import json
from pathlib import Path

from arb.cli import (
    apply_opportunity_filters,
    apply_segment_gates,
    compute_consensus,
    detect_ev_from_consensus,
    run_scan,
    split_pregame,
)
from arb.config import load_config
from arb.models import Quote
from arb.pricing import kalshi_fee_cost
from arb.store import Store
from arb.venues.oddsapi import parse_odds_events

EVENT_KEY = "baseball_mlb:2026-06-10:kansas city royals:texas rangers:3"


def kalshi_quote(cost: str, depth: str = "1200", start_days_out: int = 1) -> Quote:
    price = Decimal(cost)
    return Quote(
        event_key=EVENT_KEY,
        venue="kalshi",
        book="KXMLBGAME-26JUN101940TEXKC-TEX",
        market_type="WINNER",
        line=None,
        outcome_label="kansas city royals",
        raw_price=price,
        cost=price,
        fee_cost=kalshi_fee_cost(price, Decimal("0.07")),
        depth_usd=Decimal(depth),
        live=False,
        fetched_at=datetime.now(timezone.utc),
        start_utc=datetime.now(timezone.utc) + timedelta(days=start_days_out),
        home="kansas city royals",
        away="texas rangers",
    )


def test_ev_vector_and_sub_threshold_ev_is_still_recorded() -> None:
    quote = kalshi_quote("0.50")
    fresh = datetime.now(timezone.utc)
    consensus = {
        (EVENT_KEY, "kansas city royals"): (Decimal("0.55"), fresh),
    }
    opportunities = detect_ev_from_consensus([quote], consensus, max_age_hours=8, trusted_pairs=set())
    assert len(opportunities) == 1
    assert opportunities[0].edge == Decimal("0.062802")

    # Edge below ev_threshold (0.02) must still produce a recorded opportunity.
    small = {(EVENT_KEY, "kansas city royals"): (Decimal("0.52"), fresh)}
    opportunities = detect_ev_from_consensus([quote], small, max_age_hours=8, trusted_pairs=set())
    assert len(opportunities) == 1
    assert Decimal("0") < opportunities[0].edge < Decimal("0.02")


def test_old_consensus_is_annotated_stale() -> None:
    quote = kalshi_quote("0.50")
    old = datetime.now(timezone.utc) - timedelta(hours=9)
    consensus = {(EVENT_KEY, "kansas city royals"): (Decimal("0.55"), old)}
    opportunities = detect_ev_from_consensus([quote], consensus, max_age_hours=8, trusted_pairs=set())
    assert len(opportunities) == 1
    assert "stale-consensus" in opportunities[0].notes


def test_filters_count_left_on_table() -> None:
    config = load_config()
    quote = kalshi_quote("0.50")
    consensus = {(EVENT_KEY, "kansas city royals"): (Decimal("0.55"), datetime.now(timezone.utc))}
    deep = detect_ev_from_consensus([quote], consensus, max_age_hours=8, trusted_pairs=set())
    shallow = detect_ev_from_consensus(
        [kalshi_quote("0.50", depth="10")], consensus, max_age_hours=8, trusted_pairs=set()
    )
    far_out = detect_ev_from_consensus(
        [kalshi_quote("0.50", start_days_out=30)], consensus, max_age_hours=8, trusted_pairs=set()
    )
    kept, left = apply_opportunity_filters(deep + shallow + far_out, config)
    assert len(kept) == 1
    assert len(left) == 2


def seed_settled_segment(store: Store, sport: str, kind: str, n: int, wins: int) -> None:
    with store.connect() as con:
        for i in range(n):
            won = i < wins
            con.execute(
                """INSERT INTO flat_bets(fingerprint, opened_poll_id, category, sport, kind, trusted, live,
                       edge, stake_usd, legs_json, status, placed_at, result, pnl_optimistic, quality_flag, notes)
                   VALUES (?, 1, 'sports', ?, ?, 1, 0, '0.05', '1.00', '[]', 'SETTLED', ?, ?, ?, NULL, '')""",
                (
                    f"{sport}:{kind}:{i}",
                    sport,
                    kind,
                    datetime.now(timezone.utc).isoformat(),
                    "WON" if won else "LOST",
                    "0.90" if won else "-1.00",
                ),
            )


def ev_for_segment_gate(config) -> object:
    quote = kalshi_quote("0.50")
    consensus = {(EVENT_KEY, "kansas city royals"): (Decimal("0.56"), datetime.now(timezone.utc))}
    [opp] = detect_ev_from_consensus([quote], consensus, max_age_hours=8, trusted_pairs=set(), config=config)
    assert opp.kelly_stake is not None and opp.kelly_stake > 0
    return opp


def test_segment_gate_zeroes_kelly_when_data_is_insufficient(tmp_path) -> None:
    config = replace(load_config(), database_path=tmp_path / "arb.sqlite")
    store = Store(config.database_path)
    [gated] = apply_segment_gates([ev_for_segment_gate(config)], store, config)

    assert gated.kelly_stake == Decimal("0.00")
    assert "segment-insufficient-data" in gated.notes
    assert gated.quality_flag is None


def test_segment_gate_blocks_proven_bad_segment(tmp_path) -> None:
    config = replace(load_config(), database_path=tmp_path / "arb.sqlite")
    store = Store(config.database_path)
    seed_settled_segment(store, "baseball_mlb", "EV", n=40, wins=0)
    [gated] = apply_segment_gates([ev_for_segment_gate(config)], store, config)

    assert gated.kelly_stake == Decimal("0.00")
    assert gated.status == "REVIEW"
    assert gated.quality_flag == "segment-blocked"
    assert "segment-blocked" in gated.notes


def test_started_events_are_marked_live_and_split_out() -> None:
    now = datetime.now(timezone.utc)
    upcoming = kalshi_quote("0.50", start_days_out=1)
    started = kalshi_quote("0.0017", start_days_out=-1)
    pregame, in_play = split_pregame([upcoming, started], now)
    assert pregame == [upcoming]
    assert len(in_play) == 1
    assert in_play[0].live is True
    assert in_play[0].cost == Decimal("0.0017")


def test_scan_is_noop_when_consensus_disabled(tmp_path) -> None:
    config = replace(load_config(), consensus_enabled=False, database_path=tmp_path / "arb.sqlite")
    store = Store(config.database_path)
    args = Namespace(dry_run=False, fixtures=False, sport=None)
    assert run_scan(args, config, store) == 0
    assert store.summary()["quotes"] == 0


def test_watch_records_no_ev_when_consensus_disabled(tmp_path) -> None:
    from arb.cli import run_watch_once

    config = replace(load_config(), consensus_enabled=False, database_path=tmp_path / "arb.sqlite")
    store = Store(config.database_path)
    # Stored consensus exists, but the EV class must stay off while disabled (SPEC §1).
    store.save_consensus({(EVENT_KEY, "kansas city royals"): Decimal("0.55")}, datetime.now(timezone.utc))
    args = Namespace(once=True, dry_run=False, fixtures=True, sport=["baseball_mlb"])
    assert run_watch_once(args, config, store) == 0
    with store.connect() as con:
        kinds = {row["kind"] for row in con.execute("SELECT kind FROM opportunities")}
    assert "EV" not in kinds


def test_fixture_scan_persists_quotes_and_consensus_roundtrips(tmp_path) -> None:
    config = replace(load_config(), consensus_enabled=True, database_path=tmp_path / "arb.sqlite")
    store = Store(config.database_path)
    args = Namespace(dry_run=False, fixtures=True, sport=["baseball_mlb"])
    assert run_scan(args, config, store) == 0
    # Every observed quote is persisted regardless of pregame/in-play status.
    assert store.summary()["quotes"] == 380
    for (key, _outcome), (p_hat, computed_at) in store.load_consensus().items():
        assert Decimal("0") < p_hat < Decimal("1")
        assert computed_at.tzinfo is not None


def test_recorded_fixture_consensus_includes_common_game() -> None:
    # Clock-independent: consensus math on the raw recorded fixture (no pregame split).
    events = json.loads(Path("tests/fixtures/oddsapi_baseball_mlb.json").read_text())
    quotes = parse_odds_events(events, "baseball_mlb")
    consensus = compute_consensus(quotes, load_config().min_books_for_consensus)
    p_hat = consensus[(EVENT_KEY, "kansas city royals")]
    assert Decimal("0") < p_hat < Decimal("1")
