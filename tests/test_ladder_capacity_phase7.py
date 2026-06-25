"""Phase 7 (B1/B3/B4/G1) — real ladder capture, the shared depth_usd invariant, and the
INSUFFICIENT_DEPTH_DATA / empty-population report states. Follows ARB_LADDER_AND_FRICTIONS_SPEC §0.
"""
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from arb.capacity import arb_capacity, max_economic_arb_size
from arb.experimental.analysis.arb_capacity import format_report, run_arb_capacity
from arb.store import Store
from arb.venues.kalshi import _ladder_at_ask
from arb.venues.polymarket import polymarket_ladder
from arb.venues.sx import _taker_ladder

FIX = Path("tests/fixtures")


def _ascending(ladder):
    costs = [c for c, _ in ladder]
    return costs == sorted(costs) and len(set(costs)) == len(costs)


# ---- B3: the shared taker-$-notional invariant, calibrated per venue against a recorded book ----

def test_kalshi_ladder_notional_invariant() -> None:
    book = json.loads((FIX / "kalshi_orderbook_ladder.json").read_text())
    ladder = _ladder_at_ask(book, "yes")
    assert ladder is not None and _ascending(ladder)
    # NO bids [(0.52,100),(0.47,200)] -> YES costs (0.48,0.53); contracts = notional/cost = size.
    assert [(c, (d / c).quantize(Decimal("1"))) for c, d in ladder] == [
        (Decimal("0.48"), Decimal("100")), (Decimal("0.53"), Decimal("200"))]


def test_polymarket_ladder_notional_invariant() -> None:
    book = json.loads((FIX / "polymarket_book_ladder.json").read_text())
    ladder = polymarket_ladder(book["asks"])
    assert ladder is not None and _ascending(ladder)
    # asks price*shares = notional; contracts = notional/cost = shares (100, 200).
    assert ladder[0] == (Decimal("0.48"), Decimal("48.000000"))
    assert [(d / c).quantize(Decimal("1")) for c, d in ladder] == [Decimal("100"), Decimal("200")]


def test_sx_ladder_notional_invariant() -> None:
    data = json.loads((FIX / "sx_orders_ladder.json").read_text())
    ladder = _taker_ladder(data["orders"], maker_betting_outcome_one=False)
    assert ladder is not None and _ascending(ladder)
    # maker stake M at cost c backs taker notional T = M*c/(1-c); contracts = T/c = M/(1-c).
    # M=48 @ c=0.48 -> contracts 48/0.52 = 92.31 ; M=200 @ c=0.53 -> 200/0.47 = 425.53.
    contracts = [(d / c) for c, d in ladder]
    assert contracts[0].quantize(Decimal("0.01")) == Decimal("92.31")
    assert contracts[1].quantize(Decimal("0.01")) == Decimal("425.53")


# ---- a synthetic 2-leg arb with sloped ladders -> break-even, profit-max, limited_by='edge' ----

def test_sloped_arb_is_edge_limited() -> None:
    # Deep books whose deeper levels push Σcost over 1, so the lock crosses break-even BEFORE
    # the book runs out -> the binding constraint is the edge, not depth.
    a = [(Decimal("0.48"), Decimal("48")), (Decimal("0.55"), Decimal("5500"))]
    b = [(Decimal("0.49"), Decimal("49")), (Decimal("0.54"), Decimal("5400"))]
    cap = arb_capacity([a, b])
    assert cap.feasible
    assert cap.limited_by == "edge"
    assert cap.breakeven_stake_usd >= cap.profitmax_stake_usd > 0


# ---- B3: fillable-fraction haircut shrinks sizing only, leaves the stored book honest ----

def test_fillable_fraction_haircut_shrinks_capacity() -> None:
    legs = [
        {"cost": "0.48", "fee_cost": "0", "depth_usd": "48", "ladder": [["0.48", "480"], ["0.53", "5300"]]},
        {"cost": "0.49", "fee_cost": "0", "depth_usd": "49", "ladder": [["0.49", "490"], ["0.52", "5200"]]},
    ]
    full = max_economic_arb_size(legs, fillable_fraction=Decimal("1"))
    half = max_economic_arb_size(legs, fillable_fraction=Decimal("0.5"))
    assert half.fillable_stake_usd < full.fillable_stake_usd        # less takeable depth
    assert half.profitmax_stake_usd <= full.profitmax_stake_usd


# ---- B4 / report: INSUFFICIENT_DEPTH_DATA until enough real ladders accumulate ----

def _arb_row(con, fp, legs, status="OPEN", trusted=1):
    con.execute(
        """INSERT INTO flat_bets(fingerprint, opened_poll_id, category, sport, kind, trusted, live,
               edge, stake_usd, legs_json, status, placed_at, notes)
           VALUES (?, 1, 'sports', 'baseball_mlb', 'ARB', ?, 0, '0.0309', '1.00', ?, ?, ?, '')""",
        (fp, trusted, json.dumps(legs), status, datetime.now(timezone.utc).isoformat()),
    )


def test_report_unknown_when_ladder_coverage_below_threshold(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    laddered = [
        {"cost": "0.48", "fee_cost": "0", "depth_usd": "48", "ladder": [["0.48", "48"], ["0.53", "530"]]},
        {"cost": "0.49", "fee_cost": "0", "depth_usd": "49", "ladder": [["0.49", "49"], ["0.52", "520"]]},
    ]
    top_only = [
        {"cost": "0.48", "fee_cost": "0", "depth_usd": "48"},
        {"cost": "0.49", "fee_cost": "0", "depth_usd": "49"},
    ]
    with store.connect() as con:
        _arb_row(con, "lad", laddered)
        _arb_row(con, "t1", top_only)
        _arb_row(con, "t2", top_only)              # 1 of 3 has a ladder -> 33% < 50%
    r = run_arb_capacity(store, min_ladder_fraction=0.5)
    assert r.insufficient_depth_data and r.captured_fraction < 0.5
    out = format_report(r)
    assert "INSUFFICIENT_DEPTH_DATA" in out and "UNKNOWN" in out


def test_report_full_when_coverage_sufficient(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    laddered = [
        {"cost": "0.48", "fee_cost": "0", "depth_usd": "48", "ladder": [["0.48", "48"], ["0.53", "530"]]},
        {"cost": "0.49", "fee_cost": "0", "depth_usd": "49", "ladder": [["0.49", "49"], ["0.52", "520"]]},
    ]
    with store.connect() as con:
        _arb_row(con, "lad", laddered)
    r = run_arb_capacity(store, min_ladder_fraction=0.5)
    assert not r.insufficient_depth_data and r.captured_fraction == 1.0
    assert "INSUFFICIENT_DEPTH_DATA" not in format_report(r)


# ---- G1: empty trusted+settled population is explicit (flat $1), not a fake computed number ----

def test_g1_empty_population_is_explicit(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    top_only = [{"cost": "0.48", "fee_cost": "0", "depth_usd": "48"},
                {"cost": "0.49", "fee_cost": "0", "depth_usd": "49"}]
    with store.connect() as con:
        _arb_row(con, "review", top_only, trusted=0)   # untrusted REVIEW -> excluded -> 0 feasible
    r = run_arb_capacity(store)
    assert r.n_feasible == 0 and r.insufficient_depth_data
    out = format_report(r)
    assert "0 feasible" in out and "flat $1" in out
