"""D1 (ARB_LADDER_AND_FRICTIONS_SPEC §4): an unparseable Kalshi ticker time must NOT fall back
to close_time (days after the game) — the quote is left start-unknown and non-actionable."""
import json
from decimal import Decimal
from pathlib import Path

from arb.arbitrage import detect_true_arbs
from arb.venues.kalshi import parse_kalshi_payload

FX = Path("tests/fixtures/kalshi_baseball_mlb.json")


def _break_ticker_times(payload):
    markets = payload["markets"] if isinstance(payload, dict) else payload
    for market in markets:
        parts = str(market["ticker"]).split("-")
        if len(parts) >= 2:
            parts[1] = "BADTIME"  # no longer matches TICKER_TIME → _start_from_ticker None
            market["ticker"] = "-".join(parts)
    return payload


def test_unparseable_ticker_time_leaves_start_unknown_not_close_time():
    quotes = parse_kalshi_payload(_break_ticker_times(json.loads(FX.read_text())),
                                  "baseball_mlb", fee_rate=Decimal("0"))
    assert quotes, "fixture should still yield quotes"
    assert all(q.start_utc is None for q in quotes)
    assert all("start-unknown" in q.notes for q in quotes)


def test_start_unknown_kalshi_legs_are_not_actionable_arbs():
    quotes = parse_kalshi_payload(_break_ticker_times(json.loads(FX.read_text())),
                                  "baseball_mlb", fee_rate=Decimal("0"))
    # Even if two start-unknown legs would otherwise lock, clustering drops untimed legs.
    arbs = detect_true_arbs(quotes, bankroll=Decimal("1000"), max_stake_pct=Decimal("0.10"),
                            trusted_pairs=set(), stale_warn=Decimal("1"),
                            doubleheader_cluster_tol_secs=7200)
    assert arbs == []
