"""STRIKE_AWARE paper lane + settlement-mismatch (owner-approved 2026-07-02): AI-matched
flags are booked in a segregated $1 flat paper slice when paper_fill is on, and a pair whose
two venues resolve inconsistently is flagged settlement_mismatch, banned, and never re-bet.
No network — crafted opportunities and a direct settled ledger row."""
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from arb.experimental.nonsports import detect_nonsports, paper_booked
from arb.nonsports_ai import cached_verdict, verdict_matches
from arb.reconcile import reconcile_settlements
from arb.store import Store
from arb.tracker import record_tracker_poll

from tests.test_tracker import quote

NOW = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc)
KEY = "nonsports:ai:KXHIGHNY-26JUL03-T105:0xAA"
PAIRKEY = "kalshi:KXHIGHNY-26JUL03-T105|polymarket:0xAA"
CONFIG = SimpleNamespace(bankroll_usd=Decimal("1000"), max_stake_pct=Decimal("0.01"),
                         arb_stale_warn=Decimal("0.10"), divergence_threshold=Decimal("0.05"))


def _ai_arb_opps(cross=True):
    # Two legs whose fee-adjusted costs sum < $1 => a booked ARB (cross); == cost keeps it a flag.
    a, b = ("0.44", "0.44") if cross else ("0.60", "0.60")
    start = NOW + timedelta(days=2)   # future start -> actionable (real nonsports quotes carry close_by)
    legs = [replace(quote("YES", a), event_key=KEY, venue="kalshi", book="KXHIGHNY-26JUL03-T105",
                    start_utc=start, notes=("nonsports", "ai-matched")),
            replace(quote("NO", b), event_key=KEY, venue="polymarket", book="0xAA",
                    start_utc=start, notes=("nonsports", "ai-matched"))]
    return detect_nonsports(legs, CONFIG, ai_confidence={KEY: "0.93"}, ai_pairkeys={KEY: PAIRKEY})


def test_every_ai_flag_is_review_and_tagged_regardless_of_confidence() -> None:
    opps = _ai_arb_opps()
    assert opps and all(o.status == "REVIEW" and o.event.ai_matched for o in opps)
    assert any(n == f"pair={PAIRKEY}" for n in opps[0].notes)


def test_paper_fill_books_one_row_off_is_display_only(tmp_path) -> None:
    opps = _ai_arb_opps()
    # paper_fill OFF -> nothing booked (display-only).
    store = Store(tmp_path / "off.sqlite")
    record_tracker_poll(store, store.start_poll("nonsports"), paper_booked(opps, False), now=NOW)
    with store.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM flat_bets WHERE kind='ARB'").fetchone()[0] == 0
    # paper_fill ON -> booked; a persisting opp across two polls does NOT open a second position.
    store = Store(tmp_path / "on.sqlite")
    record_tracker_poll(store, store.start_poll("nonsports"), paper_booked(opps, True), now=NOW)
    record_tracker_poll(store, store.start_poll("nonsports"), paper_booked(opps, True), now=NOW)
    with store.connect() as con:
        rows = con.execute("SELECT ai_matched, trusted, status FROM flat_bets WHERE kind='ARB' AND status='OPEN'").fetchall()
    assert len(rows) == 1 and rows[0]["ai_matched"] == 1 and rows[0]["trusted"] == 0


def _insert_settled_ai_arb(store) -> None:
    legs = [{"venue": "kalshi", "book": "KXHIGHNY-26JUL03-T105", "cost": "0.44", "fee_cost": "0",
             "outcome_label": "YES", "market_type": "WINNER", "event_key": KEY, "line": None},
            {"venue": "polymarket", "book": "0xAA", "cost": "0.44", "fee_cost": "0",
             "outcome_label": "NO", "market_type": "WINNER", "event_key": KEY, "line": None}]
    with store.connect() as con:
        con.execute(
            """INSERT INTO flat_bets(fingerprint, opened_poll_id, category, sport, kind, trusted,
                   live, edge, stake_usd, legs_json, optimistic_cost, pnl_optimistic, status,
                   result, placed_at, settled_at, notes, ai_matched)
               VALUES('fp1', 1, 'nonsports', 'nonsports', 'ARB', 0, 0, '0.13', '1.00', ?,
                      '0.88', '0.136', 'SETTLED', 'RESOLVED', ?, ?, ?, 1)""",
            (json.dumps(legs), NOW.isoformat(), NOW.isoformat(),
             f"nonsports,ai-matched conf=0.93,pair={PAIRKEY}"),
        )


def test_settlement_mismatch_flags_bans_and_never_rebets(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    _insert_settled_ai_arb(store)
    # Venues DISAGREE: Kalshi says YES happened, Polymarket also pays its NO leg -> both legs
    # "won" -> the fake-arb the rules feared.
    resolvers = {"kalshi": lambda leg: "WON", "polymarket": lambda leg: "WON", "sx": lambda leg: None}
    counts = reconcile_settlements(store, resolvers)
    assert counts["mismatch"] == 1 and counts["banned"] == 1
    with store.connect() as con:
        row = con.execute("SELECT settlement_mismatch, ai_matched, result FROM flat_bets "
                          "WHERE status='CORRECTED'").fetchone()
    assert row["settlement_mismatch"] == 1 and row["ai_matched"] == 1 and row["result"] == "SETTLER-ERROR"
    # The pair is now a negative verdict -> never paper-bet again.
    verdict = cached_verdict(store, PAIRKEY)
    assert verdict is not None and verdict["equivalent"] == 0
    assert not verdict_matches(verdict, 0.85)
