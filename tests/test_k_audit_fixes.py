"""Regression checks for the K-series audit fixes not already pinned elsewhere:
K3 (outage close-storms), K4 (adj_edge column + haircut-aware sizing), K7 (shrink knobs),
K9 (no invented start), K15 (ARB weight renorm).
"""
import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from arb.arbitrage import build_event, detect_true_arbs
from arb.config import _shrink_keys
from arb.pricing import shrink_probability
from arb.store import Store
from arb.tracker import record_tracker_poll

from tests.test_tracker import NOW, arb_opp, ev_opp, quote


def test_k3_failed_venue_exempts_fingerprint_from_absent_closing(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    poll1 = store.start_poll("watch")
    record_tracker_poll(store, poll1, [arb_opp("0.48", "0.48")], now=NOW)
    # Next poll the arb is absent — but its venue (kalshi) failed, so NO close row.
    poll2 = store.start_poll("watch")
    record_tracker_poll(store, poll2, [], now=NOW, failed_venues={"kalshi"})
    with store.connect() as con:
        closes = con.execute("SELECT COUNT(*) FROM flat_bets WHERE kind='CLOSE'").fetchone()[0]
    assert closes == 0
    # Venue healthy and still absent -> the close proceeds as before.
    poll3 = store.start_poll("watch")
    record_tracker_poll(store, poll3, [], now=NOW)
    with store.connect() as con:
        closes = con.execute("SELECT COUNT(*) FROM flat_bets WHERE kind='CLOSE'").fetchone()[0]
    assert closes == 1


def test_k4_arb_rows_store_adj_edge_and_edge_points(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    opps = detect_true_arbs(
        arb_opp("0.48", "0.48").legs, bankroll=Decimal("1000"), max_stake_pct=Decimal("0.01"),
        trusted_pairs={"kalshi+kalshi"}, stale_warn=Decimal("0.10"),
        line_velocity={"default": Decimal("0.001")},
    )
    assert opps and opps[0].adj_edge is not None
    poll = store.start_poll("watch")
    record_tracker_poll(store, poll, opps, now=NOW)
    with store.connect() as con:
        row = con.execute("SELECT edge_points, adj_edge FROM flat_bets WHERE kind='ARB'").fetchone()
    assert row["edge_points"] is not None
    assert row["adj_edge"] is not None
    assert Decimal(row["adj_edge"]) <= Decimal(row["edge_points"])


def test_k7_shrink_knobs_are_explicit_and_mutually_exclusive() -> None:
    # sd regime
    shrunk = shrink_probability(Decimal("0.60"), 0, Decimal("0.05"), Decimal("0.5"), prior_sd=0.05)
    assert Decimal("0.5") < shrunk < Decimal("0.60")
    # pseudo-count regime
    shrunk = shrink_probability(Decimal("0.60"), 3, Decimal("0"), Decimal("0.5"), prior_pseudocount=3)
    assert shrunk == Decimal("0.55")
    # both off = identity
    assert shrink_probability(Decimal("0.60"), 3) == Decimal("0.60")
    with pytest.raises(ValueError):
        _shrink_keys({"shrink_prior_sd": 0.05, "shrink_prior_pseudocount": 3})
    with pytest.raises(ValueError):
        _shrink_keys({"shrink_prior_strength": 0.05})


def test_k9_build_event_never_invents_a_start() -> None:
    from dataclasses import replace

    untimed = replace(quote("los angeles dodgers", "0.5"), start_utc=None)
    event = build_event(untimed.event_key, [untimed])
    assert event.start_utc is None


def test_k9_missing_start_ev_is_skipped_not_stamped(tmp_path) -> None:
    from dataclasses import replace

    store = Store(tmp_path / "arb.sqlite")
    opp = ev_opp("0.50")
    opp = replace(opp, event=replace(opp.event, start_utc=None),
                  legs=[replace(opp.legs[0], start_utc=None)], fingerprint="")
    poll = store.start_poll("watch")
    record_tracker_poll(store, poll, [opp], now=NOW)
    with store.connect() as con:
        row = con.execute("SELECT quality_flag FROM flat_bets").fetchone()
    assert row["quality_flag"] == "missing-start-skipped"


def test_k15_arb_segment_not_penalized_for_lacking_win_rate(tmp_path) -> None:
    from arb.scoring import score_segments

    store = Store(tmp_path / "arb.sqlite")
    legs = json.dumps([{"venue": "kalshi", "book": "b", "outcome_label": "x",
                        "cost": "0.48", "fee_cost": "0"}])
    with store.connect() as con:
        for i in range(30):
            con.execute(
                """INSERT INTO flat_bets(fingerprint, opened_poll_id, category, sport, kind, trusted,
                       live, edge, stake_usd, legs_json, pnl_optimistic, status, placed_at, result, notes)
                   VALUES (?, 1, 'sports', 'baseball_mlb', 'ARB', 1, 0, '0.04', '1.00', ?, '0.04',
                           'SETTLED', ?, 'RESOLVED', '')""",
                (f"fp{i}", legs, datetime.now(timezone.utc).isoformat()),
            )
    scores = score_segments(store, {"score_min_n": 30})
    arb = next(s for s in scores if s.segment == "baseball_mlb:ARB")
    # Profitable, always-resolved arbs: with w_wr renormalized away the score reflects
    # ROI/trend/sample only — clearly above the 30-point block line (was dragged toward it).
    assert arb.tier != "BLOCKED"
    assert arb.score > 50
