from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from arb.config import load_config
from arb.models import Quote
from arb.experimental.nonsports import detect_nonsports, load_map, suggest_candidates, write_candidates

EVENT_KEY = "nonsports:fed-cut-2026-06"


def nonsports_quote(venue: str, label: str, cost: str) -> Quote:
    price = Decimal(cost)
    return Quote(
        event_key=EVENT_KEY, venue=venue, book=f"{venue}-{label}", market_type="WINNER",
        line=None, outcome_label=label, raw_price=price, cost=price, fee_cost=Decimal("0"),
        depth_usd=Decimal("500"), live=False, fetched_at=datetime.now(timezone.utc),
        start_utc=datetime(2026, 6, 18, tzinfo=timezone.utc), notes=("nonsports",),
    )


def test_map_filters_disabled_past_and_far_out(tmp_path) -> None:
    map_path = tmp_path / "map.yaml"
    map_path.write_text(
        """
- {id: live-one, enabled: true, close_by: 2026-06-15}
- {id: disabled, enabled: false, close_by: 2026-06-15}
- {id: too-far, enabled: true, close_by: 2026-07-15}
- {id: past, enabled: true, close_by: 2026-06-01}
"""
    )
    entries = load_map(map_path, today=date(2026, 6, 11), max_days=7)
    assert [entry["id"] for entry in entries] == ["live-one"]


def test_nonsports_flags_are_always_review_and_categorized() -> None:
    config = load_config()
    quotes = [
        nonsports_quote("kalshi", "YES", "0.40"),
        nonsports_quote("polymarket", "NO", "0.55"),  # YES+NO = 0.95 < 1 → arb
    ]
    # single venue per outcome → match gates are satisfied trivially
    opportunities = detect_nonsports(quotes, config)
    arbs = [o for o in opportunities if o.kind == "ARB"]
    assert arbs
    for opp in opportunities:
        assert opp.category == "nonsports"
        assert opp.status == "REVIEW"
        assert "nonsports" in opp.notes


def test_candidate_suggestions_respect_threshold_and_dedupe(tmp_path) -> None:
    kalshi = [("KXFED", "Fed decision June 2026"), ("KXMARS", "Elon Mars landing")]
    polymarket = [("9001", "Fed decision June 2026?"), ("9002", "NYC mayor race winner")]
    matches = suggest_candidates(kalshi, polymarket, threshold=85)
    assert [(m[1], m[3]) for m in matches] == [("KXFED", "9001")]

    log = tmp_path / "cands.log"
    assert write_candidates(matches, log) == 1
    assert write_candidates(matches, log) == 0  # identical lines are not re-appended
    assert "kalshi:KXFED" in log.read_text()
