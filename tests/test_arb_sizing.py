import json
from datetime import datetime, timezone
from decimal import Decimal

from arb.arb_sizing import build_arb_scale_projection, build_arb_sizing_model, recommend_arb_stake
from arb.store import Store


def _insert_arb(con, *, status: str, stake: str, pnl: str | None, depth: str,
                basket: str = "0.96", pair: tuple[str, str] = ("kalshi", "polymarket")) -> None:
    cost = Decimal(basket) / Decimal("2")
    legs = [
        {"venue": pair[0], "outcome_label": "a", "cost": str(cost), "fee_cost": "0", "depth_usd": depth},
        {"venue": pair[1], "outcome_label": "b", "cost": str(cost), "fee_cost": "0", "depth_usd": depth},
    ]
    con.execute(
        """INSERT INTO flat_bets(fingerprint, opened_poll_id, category, sport, kind, trusted, live,
               edge, edge_points, stake_usd, legs_json, optimistic_cost, pnl_optimistic,
               status, placed_at, settled_at, result, notes)
           VALUES (?, 1, 'sports', 'baseball_mlb', 'ARB', 1, 0,
               '0.041667', '0.040000', ?, ?, ?, ?, ?, ?, ?, 'RESOLVED', '')""",
        (
            f"{status}-{stake}-{depth}-{pnl}-{pair}",
            stake,
            json.dumps(legs),
            basket,
            pnl,
            status,
            datetime.now(timezone.utc).isoformat(),
            datetime.now(timezone.utc).isoformat() if status == "SETTLED" else None,
        ),
    )


def test_arb_sizing_trains_only_on_settled_staked_arbs(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    with store.connect() as con:
        _insert_arb(con, status="SETTLED", stake="1.00", pnl="0.04", depth="1000")
        _insert_arb(con, status="SETTLED", stake="2.00", pnl="0.08", depth="2000")
        _insert_arb(con, status="VERIFIED", stake="0", pnl=None, depth="9000")
        _insert_arb(con, status="VANISHED", stake="0", pnl=None, depth="9000")
        _insert_arb(con, status="SETTLED", stake="0", pnl="99.00", depth="9000")
        model = build_arb_sizing_model(con, Decimal("1000"), Decimal("0.10"), min_sample=1)
    assert model["training"]["n"] == 2
    assert model["training"]["stake"] == 3.0
    assert model["training"]["pnl"] == 0.12


def test_arb_sizing_scales_with_depth_when_empirical_buckets_pass(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    with store.connect() as con:
        for _ in range(4):
            _insert_arb(con, status="SETTLED", stake="1.00", pnl="0.04", depth="500")
            _insert_arb(con, status="SETTLED", stake="1.00", pnl="0.04", depth="5000")
        model = build_arb_sizing_model(con, Decimal("1000"), Decimal("0.10"), min_sample=1)
    small = [
        {"venue": "kalshi", "cost": "0.48", "fee_cost": "0", "depth_usd": "500"},
        {"venue": "polymarket", "cost": "0.48", "fee_cost": "0", "depth_usd": "500"},
    ]
    large = [
        {"venue": "kalshi", "cost": "0.48", "fee_cost": "0", "depth_usd": "5000"},
        {"venue": "polymarket", "cost": "0.48", "fee_cost": "0", "depth_usd": "5000"},
    ]
    small_rec = recommend_arb_stake(small, Decimal("0.04"), model)
    large_rec = recommend_arb_stake(large, Decimal("0.04"), model)
    assert small_rec["scalable"] is True
    assert large_rec["stake"] > small_rec["stake"]
    assert large_rec["stake"] <= model["max_stake"]


def test_g2_config_overrides_buckets_and_caps(tmp_path) -> None:
    # G2: the depth fraction + edge caps are config-driven (were hardcoded). A model built with
    # a higher fraction and cap must size a deep arb larger than the default model, and the model
    # must carry the resolved knobs so recommend_arb_stake bins/caps consistently.
    store = Store(tmp_path / "arb.sqlite")
    with store.connect() as con:
        for _ in range(4):
            _insert_arb(con, status="SETTLED", stake="1.00", pnl="0.04", depth="5000")
        default_model = build_arb_sizing_model(con, Decimal("1000"), Decimal("0.10"), min_sample=1)
        boosted_model = build_arb_sizing_model(
            con, Decimal("1000"), Decimal("0.10"), min_sample=1,
            cfg={"market_depth_fraction": "0.05",
                 "edge_caps": {"0.5-1%": 5, "1-2%": 15, "2-5%": 200, "5%+": 100}})
    deep = [
        {"venue": "kalshi", "cost": "0.48", "fee_cost": "0", "depth_usd": "5000"},
        {"venue": "polymarket", "cost": "0.48", "fee_cost": "0", "depth_usd": "5000"},
    ]
    default_rec = recommend_arb_stake(deep, Decimal("0.04"), default_model)
    boosted_rec = recommend_arb_stake(deep, Decimal("0.04"), boosted_model)
    assert default_model["market_depth_fraction"] == 0.01      # default preserved
    assert boosted_model["market_depth_fraction"] == 0.05      # config override honored
    assert boosted_rec["stake"] > default_rec["stake"]         # bigger fraction + cap -> bigger stake


def test_arb_sizing_refuses_unproven_venue_pair(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    with store.connect() as con:
        for _ in range(4):
            _insert_arb(con, status="SETTLED", stake="1.00", pnl="0.04", depth="1000")
        model = build_arb_sizing_model(con, Decimal("1000"), Decimal("0.10"), min_sample=1)
    sx_pair = [
        {"venue": "polymarket", "cost": "0.48", "fee_cost": "0", "depth_usd": "5000"},
        {"venue": "sx", "cost": "0.48", "fee_cost": "0", "depth_usd": "5000"},
    ]
    rec = recommend_arb_stake(sx_pair, Decimal("0.04"), model)
    assert rec["scalable"] is False
    assert rec["stake"] == 1.0
    assert "venue pair" in rec["reason"]


def test_arb_scale_projection_shows_larger_stakes_when_depth_allowed(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    with store.connect() as con:
        _insert_arb(con, status="SETTLED", stake="1.00", pnl="0.04", depth="500")
        _insert_arb(con, status="SETTLED", stake="1.00", pnl="0.04", depth="2000")
        projection = build_arb_scale_projection(
            con,
            Decimal("1000"),
            Decimal("0.10"),
            target_stake=Decimal("10"),
            market_depth_fraction=Decimal("0.01"),
        )

    total = projection["total"]
    assert total["n"] == 2
    assert total["actual_stake"] == 2.0
    assert total["actual_pnl"] == 0.08
    assert total["flat10_stake"] == 15.0
    assert total["flat10_pnl"] == 0.6
    assert total["depth_stake"] == 25.0
    assert total["depth_pnl"] == 1.0
    assert total["scaled_n"] == 2
    assert projection["examples"][0]["depth_stake"] == 20.0
