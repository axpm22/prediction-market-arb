"""04 — Phase A verdict wiring: EV-freeze gate, read-only Store, daily-report premise monitor.

The regrade RUN itself is exercised against the archive by hand (read-only); these tests lock the
code the verdict changed so the freeze can't silently regress.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from arb.cli import bookable_opportunities
from arb.store import Store
from arb.tracker import _premise_monitor_lines


def _opp(kind: str):
    # Only .kind is read by the gate; a light stand-in keeps the test independent of Opportunity's
    # full constructor.
    return SimpleNamespace(kind=kind)


def test_ev_freeze_gate_drops_only_ev_when_disabled() -> None:
    kept = [_opp("EV"), _opp("ARB"), _opp("LIVE_BASEBALL_EV"), _opp("DIVERGENCE"), _opp("EV")]
    # enabled → nothing dropped
    assert bookable_opportunities(kept, ev_enabled=True) == kept
    # frozen → both EV flags gone, every other kind (incl. the in-play lane) kept
    bookable = bookable_opportunities(kept, ev_enabled=False)
    assert [o.kind for o in bookable] == ["ARB", "LIVE_BASEBALL_EV", "DIVERGENCE"]


def test_premise_monitor_line_flags_inverted_and_omits_when_empty() -> None:
    # venue Brier lower than consensus → the premise is inverted (the Phase A finding).
    inverted = _premise_monitor_lines({"consensus_brier": 0.2379, "venue_brier": 0.2209, "n_consensus": 68})
    assert any("EV premise inverted" in ln for ln in inverted)
    assert any("consensus 0.2379 vs venue 0.2209" in ln for ln in inverted)
    # consensus sharper → no inversion warning.
    sharper = _premise_monitor_lines({"consensus_brier": 0.20, "venue_brier": 0.24, "n_consensus": 10})
    assert any("consensus sharper" in ln for ln in sharper)
    # no data → the line is omitted entirely (never breaks the report).
    assert _premise_monitor_lines(None) == []
    assert _premise_monitor_lines({"consensus_brier": None, "venue_brier": None}) == []


def test_store_read_only_reads_but_cannot_write(tmp_path) -> None:
    path = tmp_path / "arb.sqlite"
    live = Store(path)
    with live.connect() as con:
        con.execute("INSERT INTO polls(started_at, kind) VALUES (?, ?)",
                    (datetime.now(timezone.utc).isoformat(), "watch"))

    ro = Store(path, read_only=True)
    with ro.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM polls").fetchone()[0] == 1  # reads work
        with pytest.raises(Exception):  # mode=ro rejects any write
            con.execute("INSERT INTO polls(started_at, kind) VALUES ('x', 'y')")


def test_store_read_only_never_creates_a_missing_db(tmp_path) -> None:
    missing = tmp_path / "nope.sqlite"
    Store(missing, read_only=True)  # must not create the file or run migrations
    assert not missing.exists()
