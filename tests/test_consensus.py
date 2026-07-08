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
    h2h = [c for c in cons if c["market_type"] == "WINNER"]
    assert len(h2h) == 2  # two outcomes, 3 books each
    mets = [c for c in h2h if c["outcome"] == "New York Mets"][0]
    assert mets["n_books"] == 3
    assert Decimal("0.55") < mets["p_hat"] < Decimal("0.65")
    # totals: only 1 book quotes it -> below the totals floor (2) -> no consensus row
    assert not [c for c in cons if c["market_type"] == "TOTAL"]
    # per-book de-vig rows persist for the bake-off re-run (v3.3)
    assert len([b for b in books if b["market_type"] == "WINNER"]) == 6
