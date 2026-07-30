"""Cover-survival funnel (read-only). A cover pays only if it clears every
gate at once; each gate here has killed the lane at some point in this project,
so the test pins one synthetic cover per failure mode."""
import json
import sqlite3
from pathlib import Path

from arb.config import load
from arb import covers

ROOT = Path(__file__).resolve().parent.parent
CFG = load(ROOT / "config.yaml")


def _con():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE polls (id INTEGER PRIMARY KEY, started_at TEXT)")
    c.execute("""CREATE TABLE opportunities (
                   id INTEGER PRIMARY KEY, poll_id INTEGER, fingerprint TEXT,
                   kind TEXT, event_key TEXT, edge TEXT, status TEXT,
                   legs_json TEXT)""")
    return c


def _leg(cost, bid, depth):
    return {"venue": "kalshi", "side": "YES", "cost": cost, "bid": bid,
            "depth_usd": depth}


def _add(c, poll, at, fp, edge, legs):
    c.execute("INSERT OR IGNORE INTO polls VALUES (?,?)", (poll, at))
    c.execute("INSERT INTO opportunities (poll_id,fingerprint,kind,event_key,"
              "edge,status,legs_json) VALUES (?,?,?,?,?,?,?)",
              (poll, fp, "XSPREAD", f"ev:{fp}", edge, "REVIEW",
               json.dumps(legs)))


FLOOR = float(CFG["min_executable_usd"])


def test_funnel_drops_each_failure_mode_at_its_own_gate():
    c = _con()
    wide = [_leg("0.50", "0.20", FLOOR * 4), _leg("0.49", "0.19", FLOOR * 4)]
    thin = [_leg("0.50", "0.499", 1.0), _leg("0.49", "0.489", 1.0)]
    good = [_leg("0.50", "0.499", FLOOR * 4), _leg("0.49", "0.489", FLOOR * 4)]
    _add(c, 1, "2026-07-30T00:00:00", "neg", "-0.01", good)   # no edge
    _add(c, 1, "2026-07-30T00:00:00", "wide", "0.01", wide)   # spread > edge
    _add(c, 1, "2026-07-30T00:00:00", "thin", "0.01", thin)   # under depth floor
    _add(c, 1, "2026-07-30T00:00:00", "once", "0.01", good)   # one poll only
    _add(c, 1, "2026-07-30T00:00:00", "live", "0.01", good)   # clears everything
    _add(c, 2, "2026-07-30T00:01:00", "live", "0.01", good)
    r = covers.funnel(CFG, c, "2026-07-30T00:00:00")
    s = r["stages"]
    assert s["detected"] == 5
    assert s["edge_net>0"] == 4                      # 'neg' gone
    assert s["spread<=edge"] == 3                    # 'wide' gone
    assert s[f"depth>=${FLOOR:.0f}"] == 2            # 'thin' gone
    assert s["survived>=2 polls"] == 1               # 'once' gone
    assert [x["event_key"] for x in r["survivors"]] == ["ev:live"]


def test_a_leg_with_no_bid_is_disqualified_not_treated_as_zero_spread():
    """SX carries 554,910 quotes and zero bids. Reading a missing bid as 0
    would make every one of them look like a free cover — it is the opposite,
    a book you cannot exit."""
    c = _con()
    nobid = [_leg("0.50", None, FLOOR * 4), _leg("0.49", None, FLOOR * 4)]
    _add(c, 1, "2026-07-30T00:00:00", "sx", "0.01", nobid)
    _add(c, 2, "2026-07-30T00:01:00", "sx", "0.01", nobid)
    r = covers.funnel(CFG, c, "2026-07-30T00:00:00")
    assert r["stages"]["edge_net>0"] == 1
    assert r["stages"]["spread<=edge"] == 0
    assert r["survivors"] == []


def test_window_excludes_polls_before_since():
    c = _con()
    good = [_leg("0.50", "0.499", FLOOR * 4), _leg("0.49", "0.489", FLOOR * 4)]
    _add(c, 1, "2026-07-29T00:00:00", "old", "0.01", good)
    _add(c, 2, "2026-07-30T12:00:00", "new", "0.01", good)
    r = covers.funnel(CFG, c, "2026-07-30T00:00:00")
    assert r["observations"] == 1 and r["stages"]["detected"] == 1


def test_render_states_the_censoring_when_nothing_survives():
    c = _con()
    r = covers.funnel(CFG, c, "2026-07-30T00:00:00")
    text = covers.render(r)
    assert "NOTHING survived" in text and "censored" in text
