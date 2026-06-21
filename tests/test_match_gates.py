from datetime import datetime, timedelta, timezone
from decimal import Decimal

from arb.arbitrage import detect_divergences, detect_true_arbs, same_outcome_spread, start_drift_secs
from arb.models import Quote

EVENT = "baseball_mlb:2026-06-12:kansas city royals:texas rangers:3"
NOW = datetime(2026, 6, 12, 20, 0, tzinfo=timezone.utc)


def q(venue: str, outcome: str, cost: str, start_offset_mins: int = 0) -> Quote:
    price = Decimal(cost)
    return Quote(
        event_key=EVENT, venue=venue, book=f"{venue}-{outcome}", market_type="WINNER",
        line=None, outcome_label=outcome, raw_price=price, cost=price, fee_cost=Decimal("0"),
        depth_usd=Decimal("500"), live=False, fetched_at=NOW,
        start_utc=NOW + timedelta(hours=3, minutes=start_offset_mins),
    )


def detect(quotes, **gates):
    return detect_true_arbs(
        quotes, bankroll=Decimal("1000"), max_stake_pct=Decimal("0.10"),
        trusted_pairs=set(), stale_warn=Decimal("0.04"), **gates,
    )


def test_start_drift_suspends_the_arb() -> None:
    quotes = [q("kalshi", "kansas city royals", "0.48"),
              q("polymarket", "texas rangers", "0.48", start_offset_mins=120)]
    assert start_drift_secs(quotes) == 7200
    assert detect(quotes, max_start_drift_mins=30) == []          # suspended, not flagged
    assert len(detect(quotes)) == 1                                # gate off → old behavior


def test_same_outcome_gap_suspends_the_arb() -> None:
    quotes = [
        q("kalshi", "kansas city royals", "0.30"),
        q("polymarket", "kansas city royals", "0.55"),   # 25-point gap on one outcome
        q("polymarket", "texas rangers", "0.40"),
    ]
    assert same_outcome_spread(quotes) == Decimal("0.25")
    assert detect(quotes, match_suspect_prob_diff=Decimal("0.15")) == []
    assert len(detect(quotes)) == 1


def test_suspect_divergences_stay_recorded_but_tagged() -> None:
    quotes = [q("kalshi", "kansas city royals", "0.30"),
              q("polymarket", "kansas city royals", "0.55")]
    flagged = detect_divergences(quotes, Decimal("0.03"), set(),
                                 match_suspect_prob_diff=Decimal("0.15"))
    assert len(flagged) == 1
    assert flagged[0].status == "REVIEW"
    assert "match-suspect" in flagged[0].notes
