"""Consensus from the (synthetic-shape) Odds API fixture."""
import json
from decimal import Decimal
from pathlib import Path

from arb.config import load
from arb.consensus import compute_anchor_consensus, compute_consensus
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


def test_doubleheader_consensus_does_not_blend():
    # H3: two same-pair games same day must NOT collapse to one bare-key row
    # (which would blend both games' books). They get :dh1/:dh2 tags instead.
    def _books(commence, fav):
        comp = 1 / (1 - 1 / fav)  # vig-free complement
        return [{"sport_key": "baseball_mlb", "home": "New York Mets",
                 "away": "Kansas City Royals", "commence_time": commence,
                 "book": bk, "market": "h2h", "line": None, "outcome": o, "odds": od}
                for bk in ("pin", "dk", "fd")
                for o, od in (("New York Mets", fav),
                              ("Kansas City Royals", comp))]
    rows = (_books("2026-07-08T17:00:00Z", 1.5)     # game 1, 1pm ET
            + _books("2026-07-08T20:00:00Z", 3.0))  # game 2, 4pm ET
    cons, _ = compute_consensus(CFG, rows, "2026-07-08T12:00:00Z")
    keys = {c["event_key"] for c in cons}
    assert "baseball_mlb:2026-07-08:mets:royals:dh1" in keys
    assert "baseball_mlb:2026-07-08:mets:royals:dh2" in keys
    assert "baseball_mlb:2026-07-08:mets:royals" not in keys  # never blended


def test_consensus_skips_book_with_malformed_odds():
    # one bad book price (odds <= 1) must drop THAT book, not kill the scan
    def _rows(book, odds_a, odds_b):
        return [{"sport_key": "baseball_mlb", "home": "New York Mets",
                 "away": "Kansas City Royals", "commence_time": "2026-07-08T20:00:00Z",
                 "book": book, "market": "h2h", "line": None, "outcome": o, "odds": od}
                for o, od in (("New York Mets", Decimal(odds_a)),
                              ("Kansas City Royals", Decimal(odds_b)))]
    rows = (_rows("pin", "1.91", "1.91") + _rows("dk", "1.90", "1.92")
            + _rows("fd", "1.89", "1.93") + _rows("bad", "0.50", "1.91"))
    cons, books = compute_consensus(CFG, rows, "2026-07-08T12:00:00Z")
    assert cons, "good books must still produce consensus"
    assert all(c["n_books"] == 3 for c in cons)          # bad book dropped
    assert not [b for b in books if b["book"] == "bad"]  # and not persisted


def test_anchor_consensus_keeps_only_configured_book():
    # Part A: eu fixture has pinnacle (the anchor) + onexbet (a soft book) —
    # only pinnacle may survive the filter.
    events = json.loads((ROOT / "tests/fixtures/oddsapi_h2h_anchor.json").read_text())["events"]
    rows = parse_events(events, "baseball_mlb")
    cons, books = compute_anchor_consensus(CFG, rows, "2026-07-08T20:10:00Z")
    assert {b["book"] for b in books} == {"pinnacle"}
    assert all(c["method"] == "anchor:power" for c in cons)
    assert all(b["method"] == "anchor:power" for b in books)
    ev0 = [c for c in cons if c["event_key"] == "baseball_mlb:2026-07-08:bluejays:giants"]
    assert len(ev0) == 2  # both h2h outcomes, single-book de-vig
    assert all(c["n_books"] == 1 for c in ev0)
    tor = [c for c in ev0 if c["outcome"] == "Toronto Blue Jays"][0]
    assert Decimal("0.9") < tor["p_hat"] < Decimal("1")  # TOR huge favourite, vig-free


def test_anchor_consensus_doubleheader_does_not_blend():
    def _books(commence, fav):
        comp = 1 / (1 - 1 / fav)
        return [{"sport_key": "baseball_mlb", "home": "New York Mets",
                 "away": "Kansas City Royals", "commence_time": commence,
                 "book": bk, "market": "h2h", "line": None, "outcome": o, "odds": od}
                for bk, o, od in (("pinnacle", "New York Mets", fav),
                                  ("pinnacle", "Kansas City Royals", comp))]
    rows = (_books("2026-07-08T17:00:00Z", Decimal("1.5"))
            + _books("2026-07-08T20:00:00Z", Decimal("3.0")))
    cons, _ = compute_anchor_consensus(CFG, rows, "2026-07-08T12:00:00Z")
    keys = {c["event_key"] for c in cons}
    assert "baseball_mlb:2026-07-08:mets:royals:dh1" in keys
    assert "baseball_mlb:2026-07-08:mets:royals:dh2" in keys
    assert "baseball_mlb:2026-07-08:mets:royals" not in keys
