from dataclasses import replace
from datetime import datetime, timezone

from arb.config import load_config
from arb.experimental.dashboard import build_dashboard_data, write_dashboard
from arb.experimental.webserver import PAGE, build_state
from arb.store import Store


def seed_settled(
    store: Store,
    fingerprint: str,
    *,
    edge: str,
    pnl_optimistic: str,
    pnl_confirmed: str | None,
    pnl_kelly_optimistic: str | None,
    pnl_kelly_confirmed: str | None,
    quality_flag: str | None,
) -> None:
    with store.connect() as con:
        con.execute(
            """INSERT INTO flat_bets(fingerprint, opened_poll_id, category, sport, kind, trusted, live,
                   edge, stake_usd, legs_json, status, placed_at, result, pnl_optimistic, pnl_confirmed,
                   pnl_kelly_optimistic, pnl_kelly_confirmed, quality_flag, notes)
               VALUES (?, 1, 'sports', 'baseball_mlb', 'EV', 1, 0, ?, '1.00', '[]', 'SETTLED', ?,
                       'WON', ?, ?, ?, ?, ?, '')""",
            (
                fingerprint,
                edge,
                datetime.now(timezone.utc).isoformat(),
                pnl_optimistic,
                pnl_confirmed,
                pnl_kelly_optimistic,
                pnl_kelly_confirmed,
                quality_flag,
            ),
        )


def test_dashboard_data_includes_clean_raw_buckets_drawdowns_and_kelly(tmp_path) -> None:
    config = replace(load_config(), database_path=tmp_path / "arb.sqlite")
    store = Store(config.database_path)
    seed_settled(
        store,
        "clean",
        edge="0.030000",
        pnl_optimistic="1.00",
        pnl_confirmed="0.50",
        pnl_kelly_optimistic="5.00",
        pnl_kelly_confirmed="2.50",
        quality_flag=None,
    )
    seed_settled(
        store,
        "flagged",
        edge="0.080000",
        pnl_optimistic="-1.00",
        pnl_confirmed=None,
        pnl_kelly_optimistic="-3.00",
        pnl_kelly_confirmed=None,
        quality_flag="suspect-edge",
    )

    data = build_dashboard_data(store, config)

    assert len(data["equity"]["optimistic_raw"]) == 2
    assert len(data["equity"]["optimistic_clean"]) == 1
    assert data["drawdowns"]["optimistic_raw"]["max_drawdown"] == -1.0
    assert len(data["kelly_equity"]["optimistic_raw"]) == 2
    assert data["kelly_drawdowns"]["optimistic_raw"]["max_drawdown"] == -3.0
    assert data["optimistic_confirmed_gap"]["raw"] == -0.5
    assert data["ev_edge_buckets"]["2-5%"]["raw"]["n"] == 1
    assert data["ev_edge_buckets"]["2-5%"]["clean"]["n"] == 1
    assert data["ev_edge_buckets"][">5%"]["raw"]["n"] == 1
    assert data["ev_edge_buckets"][">5%"]["clean"]["n"] == 0
    assert data["left_on_table"]["persisted"] is False

    json_path = write_dashboard(store, config, tmp_path)
    assert json_path.exists()
    html = (tmp_path / "dashboard.html").read_text()
    assert "chartjs-adapter-date-fns" in html
    assert "Kelly optimistic raw" in html


def test_live_dashboard_exposes_scanner_controls(tmp_path) -> None:
    config = replace(load_config(), database_path=tmp_path / "arb.sqlite")
    store = Store(config.database_path)

    state = build_state(store, config)

    assert "scanners" in state
    scanner_ids = {scanner["id"] for scanner in state["scanners"]}
    assert scanner_ids >= {"daemon", "micro_ev", "ev_exit", "political", "nonsports"}
    assert not (scanner_ids & {"focus", "verify", "hot"})
    micro = next(scanner for scanner in state["scanners"] if scanner["id"] == "micro_ev")
    assert micro["control"] == "parent"
    ev_exit = next(scanner for scanner in state["scanners"] if scanner["id"] == "ev_exit")
    assert ev_exit["control"] == "direct"
    assert "Micro EV" in PAGE
    assert len([process["name"] for process in state["processes"]]) == len({process["name"] for process in state["processes"]})
    assert "micro_ev" in state
    assert all("returns" in scanner and "next" in scanner for scanner in state["scanners"])
    assert "Scanning Controls" in PAGE
    assert "/api/scanner" in PAGE


def test_dashboard_surfaces_exposure_and_alerts(tmp_path) -> None:
    import json as _json
    from datetime import datetime as _dt, timezone as _tz
    config = replace(load_config(), database_path=tmp_path / "arb.sqlite", bankroll_usd=__import__("decimal").Decimal("100"))
    store = Store(config.database_path)
    legs = _json.dumps([{"venue": "polymarket", "book": "b", "outcome_label": "x", "cost": "0.5", "fee_cost": "0"}])
    with store.connect() as con:
        for i in range(3):  # 3 x $60 = $180 committed > $100 bankroll
            con.execute(
                """INSERT INTO flat_bets(fingerprint, opened_poll_id, category, sport, kind, trusted, live,
                       edge, stake_usd, legs_json, status, placed_at, notes)
                   VALUES (?, 1, 'sports', 'baseball_mlb', 'EV', 0, 0, '0.03', '60', ?, 'OPEN', ?, '')""",
                (f"fp{i}", legs, _dt.now(_tz.utc).isoformat()),
            )
    from arb.experimental.webserver import build_state
    st = build_state(store, config)
    assert st["exposure"]["over_bankroll"] is True
    assert any("exceeds bankroll" in a["text"] and a["level"] == "bad" for a in st["alerts"])
