"""L5 vectors (PROMPT_LIVE_LEADLAG B1–B5): a clean lead and a clean null."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from arb.config import load
from arb.leadlag import cross_correlation, run_study
from arb.store import Store

ROOT = Path(__file__).resolve().parent.parent
CFG = load(ROOT / "config.yaml")
BASE = datetime(2026, 7, 9, 0, 0, tzinfo=timezone.utc)


def _seed(store, n_events, lag_secs_b, alternate=False):
    """Each event: both venues quote mid 0.50, then move +0.02 — venue A at
    +100s, venue B at +100s+lag (or alternating who leads when alternate)."""
    rows = []
    for i in range(n_events):
        ek = f"baseball_mlb:2026-07-08:team{i}a:team{i}b"
        lead_is_a = (not alternate) or (i % 2 == 0)
        lag_a = 0 if lead_is_a else lag_secs_b
        lag_b = lag_secs_b if lead_is_a else 0
        for venue, lag in (("kalshi", lag_a), ("polymarket", lag_b)):
            for t_off, mid in ((0, "0.50"), (100 + lag, "0.52")):
                cost = Decimal(mid) + Decimal("0.01")
                bid = Decimal(mid) - Decimal("0.01")
                rows.append({
                    "poll_id": t_off // 20, "fetched_at":
                        (BASE + timedelta(seconds=t_off)).isoformat(),
                    "event_key": ek, "category": "sports", "sport": "baseball_mlb",
                    "venue": venue, "book": venue, "market_type": "WINNER",
                    "line": None, "outcome": f"team{i}a", "side": "YES",
                    "raw_price": str(cost), "cost": str(cost), "fee_cost": "0",
                    "depth_usd": "100", "ladder_json": None, "live": 1,
                    "bid": str(bid), "volume_24h": "5",
                    "start_utc": (BASE - timedelta(hours=1)).isoformat(),
                    "start_grade": "nominal", "venue_market_id": f"M{i}-{venue}"})
    store.insert_many("quotes", rows)


def test_lead_vector(tmp_path):
    # venue A (+0.02 at t), venue B follows at t+40s, 60 events
    store = Store(tmp_path / "t.sqlite")
    _seed(store, 60, lag_secs_b=40)
    res = run_study(CFG, store, seed=1729)
    pair = res["pairs"]["kalshi:polymarket"]
    assert pair["n_matched"] == 60
    assert pair["p_lead_first"] == 1.0          # kalshi (first venue) won every match
    assert pair["ci_low"] > 0.5                 # CI excludes 0.5 -> kalshi LEADS
    assert pair["leader"] == "kalshi"
    assert pair["median_lead_secs"] == 40.0     # the tradability bound
    assert pair["n_events"] == 60
    # a run row persisted (append-only, SC5)
    assert store.query("SELECT COUNT(*) c FROM leadlag_runs")[0]["c"] == 1


def test_null_vector(tmp_path):
    # alternating leader -> p_lead = 0.5, CI contains 0.5 -> "no lead"
    store = Store(tmp_path / "t.sqlite")
    _seed(store, 60, lag_secs_b=40, alternate=True)
    res = run_study(CFG, store, seed=1729)
    pair = res["pairs"]["kalshi:polymarket"]
    assert pair["p_lead_first"] == 0.5
    assert pair["ci_low"] <= 0.5 <= pair["ci_high"]
    assert pair["leader"] is None               # study reports "no lead"


def test_cross_correlation_argmax_lag():
    # A's change lands 2 grid steps before B's identical change
    a = [0, 0, 0, 0, 0, Decimal("0.02"), 0, 0, 0, 0]
    b = [0, 0, 0, 0, 0, 0, 0, Decimal("0.02"), 0, 0]
    lag, corr = cross_correlation(a, b, max_lag=5)
    assert lag == 2
    assert corr > Decimal("0.9")


def test_velocity_estimates_emitted(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    _seed(store, 10, lag_secs_b=40)
    res = run_study(CFG, store, seed=1)
    v = res["velocities"]["baseball_mlb"]["kalshi"]
    # |Δmid|/Δt = 0.02/100s = 0.0002 pts/sec (in-play observations)
    assert Decimal(v) == Decimal("0.0002")
