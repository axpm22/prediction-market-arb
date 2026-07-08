"""Consensus from the (synthetic-shape) Odds API fixture."""
import json
from decimal import Decimal
from pathlib import Path

from arb.config import load
from arb.consensus import compute_consensus
from arb.venues.oddsapi import parse_events

ROOT = Path(__file__).resolve().parent.parent
CFG = load(ROOT / "config.yaml")


def test_consensus_median_and_book_rows():
    events = json.loads((ROOT / "tests/fixtures/oddsapi_h2h.json").read_text())["events"]
    rows = parse_events(events, "baseball_mlb")
    cons, books = compute_consensus(CFG, rows, "2026-07-08T18:00:00Z")
    # canonical event key so consensus JOINS venue quotes (bluejays/giants sorted)
    ev0 = [c for c in cons if c["event_key"] == "baseball_mlb:2026-07-08:bluejays:giants"]
    h2h = [c for c in ev0 if c["market_type"] == "WINNER"]
    assert len(h2h) == 2  # two outcomes, 5 h2h books >= floor 3
    tor = [c for c in h2h if c["outcome"] == "Toronto Blue Jays"][0]
    assert tor["n_books"] >= 3
    assert Decimal("0.85") < tor["p_hat"] < Decimal("1")  # TOR huge in-game favourite
    # totals floor is 2: the 11.5 line (5 books) makes consensus; the lone 11 doesn't
    tot_lines = {c["line"] for c in ev0 if c["market_type"] == "TOTAL"}
    assert tot_lines == {"11.5"}
    # per-book de-vig rows persist for the bake-off re-run (v3.3)
    assert len([b for b in books if b["market_type"] == "WINNER"
                and b["event_key"] == "baseball_mlb:2026-07-08:bluejays:giants"]) == 10
