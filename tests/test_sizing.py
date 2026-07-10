"""§13.4 sizing/capacity vectors — projections only, NEVER headline.

Exact Decimal vectors for VWAP, q*, Kelly; the INSUFFICIENT_DEPTH_DATA gate;
and a frozen test that projections cannot move the headline."""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from arb.capacity import INSUFFICIENT, capacity_frame
from arb.config import load
from arb.report import daily_frame, headline, write_daily
from arb.sizing import kelly_stake, parse_ladder, q_star, vwap_cost
from arb.store import Store
from arb.tracker import process_poll

from tests.test_tracker import VENUE_OK, arb_opp

NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parent.parent
LADDER = [(Decimal("0.50"), Decimal("100")), (Decimal("0.52"), Decimal("100"))]


@pytest.fixture
def cfg():
    return load(ROOT / "config.yaml")


# ---------- pure math vectors ----------

def test_parse_ladder_json_and_repr():
    assert parse_ladder('[["0.50", "100"], ["0.52", "100"]]') == LADDER
    assert parse_ladder("[['0.50', '100'], ['0.52', '100']]") == LADDER  # sx repr form
    assert parse_ladder(None) == []
    assert parse_ladder("not a ladder") == []


def test_vwap_vector():
    # 150 contracts: 100@0.50 + 50@0.52 -> 76/150 = 0.506667
    v, filled = vwap_cost(LADDER, Decimal(150))
    assert (v, filled) == (Decimal("0.506667"), Decimal(150))
    # ladder exhausts: partial fill reported, never invented depth
    v, filled = vwap_cost(LADDER, Decimal(300))
    assert (v, filled) == (Decimal("0.510000"), Decimal(200))
    assert vwap_cost([], Decimal(10)) == (None, Decimal(0))


def test_q_star_vector():
    # net edge 0.03, slippage budget 0.20 -> bound 0.006 over top 0.50;
    # level 2: vwap(n)-0.50 <= 0.006 -> n <= 2/0.014 = 142.857143
    assert q_star(LADDER, Decimal("0.03"), Decimal("0.20")) == Decimal("142.857143")
    # generous bound -> whole visible ladder, never more
    assert q_star(LADDER, Decimal("0.5"), Decimal("0.5")) == Decimal(200)
    assert q_star([], Decimal("0.03"), Decimal("0.20")) == Decimal(0)
    assert q_star(LADDER, Decimal("0"), Decimal("0.20")) == Decimal(0)


def test_kelly_vector():
    # §10 EV vector: p=0.55, c=0.5175 -> f* = 0.0325/0.4825; quarter-Kelly x $1000
    s = kelly_stake(Decimal("0.55"), Decimal("0.5175"), Decimal(1000),
                    Decimal("0.25"), Decimal("0.05"))
    assert s == Decimal("16.839378")
    # 5% hard cap binds before fractional Kelly
    s = kelly_stake(Decimal("0.9"), Decimal("0.5"), Decimal(1000),
                    Decimal("0.25"), Decimal("0.05"))
    assert s == Decimal("50.000000")
    assert kelly_stake(Decimal("0.5"), Decimal("0.5175"), Decimal(1000),
                       Decimal("0.25"), Decimal("0.05")) == Decimal(0)


# ---------- capacity gate ----------

def _db_with_arb(cfg, tmp_path, ladder_json=None):
    """One clean ARB (S=0.98, below stale-warn) booked at poll 1; optionally
    matching quotes rows carrying ladders."""
    store = Store(tmp_path / "t.sqlite")
    p1 = store.insert("polls", {"started_at": NOW.isoformat(), "kind": "watch",
                                "n_quotes": 2, "n_opps": 1})
    process_poll(cfg, store, p1, "watch", [arb_opp(cfg, "0.49", "0.49")],
                 VENUE_OK, now=NOW)
    if ladder_json is not None:
        for outcome in ("mets", "royals"):
            store.insert("quotes", {
                "poll_id": p1, "fetched_at": NOW.isoformat(),
                "event_key": "baseball_mlb:2026-07-08:mets:royals",
                "category": "sports", "sport": "baseball_mlb", "venue": "kalshi",
                "book": "kalshi", "market_type": "WINNER", "line": None,
                "outcome": outcome, "side": "YES", "raw_price": "0.49",
                "cost": "0.49", "fee_cost": "0", "depth_usd": "100",
                "ladder_json": ladder_json, "live": 0})
    return store


def test_capacity_insufficient_without_ladders(cfg, tmp_path):
    store = _db_with_arb(cfg, tmp_path)  # no quotes rows at all
    f = capacity_frame(cfg, store, "2026-07-08")
    assert f["status"] == INSUFFICIENT
    assert f["n_feasible"] == 1 and f["n_with_ladders"] == 0
    assert f["positions"] == [] and f["projected_pnl"] == Decimal(0)
    # top-of-book-only quotes are fiction too (V1 bug B1)
    store2 = _db_with_arb(cfg, tmp_path.joinpath("b"),
                          ladder_json='[["0.49", "100"]]')
    assert capacity_frame(cfg, store2, "2026-07-08")["status"] == INSUFFICIENT


def test_capacity_projection_vector(cfg, tmp_path):
    store = _db_with_arb(cfg, tmp_path,
                         ladder_json='[["0.49", "100"], ["0.51", "100"]]')
    f = capacity_frame(cfg, store, "2026-07-08")
    assert f["status"] == "OK" and f["ladder_fraction"] == 1.0
    assert len(f["positions"]) == 1
    p = f["positions"][0]
    # caps: kelly 50/0.98=51.020408; depth (100/5)/0.49=40.816327; q*=125 each leg
    assert p["contracts"] == Decimal("40.816327")
    # all fills inside level 1 -> vwap 0.49/leg; pnl = contracts x 0.02, kalshi fixed 0
    assert p["pnl_usd"] == Decimal("0.816327")


def test_capacity_dedup_and_review_exclusion(cfg, tmp_path):
    """Reappearances dedup by position_key; REVIEW/untrusted rows never project
    (V1 bug B2: 4.5x duplication + 99% REVIEW rows = '$72,209')."""
    store = _db_with_arb(cfg, tmp_path,
                         ladder_json='[["0.49", "100"], ["0.51", "100"]]')
    t2 = NOW + timedelta(seconds=60)
    p2 = store.insert("polls", {"started_at": t2.isoformat(), "kind": "watch",
                                "n_quotes": 2, "n_opps": 1})
    process_poll(cfg, store, p2, "watch", [arb_opp(cfg, "0.485", "0.49", fetched=t2)],
                 VENUE_OK, now=t2)
    f = capacity_frame(cfg, store, "2026-07-08")
    assert f["n_feasible"] == 1  # same position_key counted once
    # flip the bet to review via a hand-built row: excluded from projections
    row = dict(store.query("SELECT * FROM flat_bets LIMIT 1")[0])
    row.pop("id")
    row.update({"lane": "review", "position_key": "other-pos", "fingerprint": "fp2"})
    store.insert("flat_bets", row)
    f2 = capacity_frame(cfg, store, "2026-07-08")
    assert f2["n_feasible"] == 2 and len(f2["positions"]) == 1


def test_projections_never_touch_headline(cfg, tmp_path):
    """Frozen: the projection section changes no headline cell and the daily md
    labels it as a projection."""
    store = _db_with_arb(cfg, tmp_path,
                         ladder_json='[["0.49", "100"], ["0.51", "100"]]')
    hl_before = headline(cfg, store)
    frame = daily_frame(cfg, store, "2026-07-08")
    assert frame["capacity"]["status"] == "OK"
    assert headline(cfg, store) == hl_before
    write_daily(cfg, store, "2026-07-08", outdir=tmp_path / "out")
    md = (tmp_path / "out" / "2026-07-08.md").read_text()
    assert "never headline" in md and "PROJECTION" in md.upper()


def test_parse_ladder_bad_decimal_token_returns_empty():
    # InvalidOperation escaped the except tuple: one malformed ladder_json row
    # in the DB crashed capacity_frame and hence `arb report --daily`
    assert parse_ladder('[["abc", "1"]]') == []
    assert parse_ladder('[["0.5", "NaN%"]]') == []


def test_capacity_finds_complement_mapped_leg_quote(cfg, tmp_path):
    # quotes persist PRE-map (cycle.py stores the raw fetch); a NO(mets) quote
    # complement-maps to a YES(royals) leg at detection. The old join on the
    # post-map (outcome, side) missed every such leg — the position was
    # silently excluded and dragged ladder_fraction under the gate.
    store = Store(tmp_path / "cm.sqlite")
    p1 = store.insert("polls", {"started_at": NOW.isoformat(), "kind": "watch",
                                "n_quotes": 2, "n_opps": 1})
    process_poll(cfg, store, p1, "watch", [arb_opp(cfg, "0.49", "0.49")],
                 VENUE_OK, now=NOW)
    ladder = '[["0.49", "100"], ["0.51", "100"]]'
    common = {"poll_id": p1, "fetched_at": NOW.isoformat(),
              "event_key": "baseball_mlb:2026-07-08:mets:royals",
              "category": "sports", "sport": "baseball_mlb", "venue": "kalshi",
              "book": "kalshi", "market_type": "WINNER", "line": None,
              "raw_price": "0.49", "cost": "0.49", "fee_cost": "0",
              "depth_usd": "100", "ladder_json": ladder, "live": 0}
    store.insert("quotes", {**common, "outcome": "mets", "side": "YES"})
    # the royals YES leg exists in the DB only as its pre-map NO(mets) source
    store.insert("quotes", {**common, "outcome": "mets", "side": "NO"})
    f = capacity_frame(cfg, store, "2026-07-08")
    assert f["n_with_ladders"] == 1
    assert f["status"] == "OK"
