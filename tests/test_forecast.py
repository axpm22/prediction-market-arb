from decimal import Decimal

from arb.forecast.ledger import log_forecast, report_data, settle_forecast
from arb.forecast.scoring import brier, calibration_bins, log_loss, skill_score
from arb.store import Store


def test_forecast_scoring_vectors() -> None:
    assert brier(Decimal("0.7"), 1) == 0.09
    assert brier(Decimal("0.7"), 0) == 0.49
    assert log_loss(Decimal("0.7"), 1) == 0.356675
    assert skill_score(0.18, 0.20) == 0.10


def test_calibration_bin_vector() -> None:
    rows = [{"p_you": "0.6", "outcome": 1 if i < 58 else 0} for i in range(100)]
    bins = calibration_bins(rows, 10)
    bucket = next(row for row in bins if row["bin"] == "0.6-0.7")
    assert bucket["n"] == 100
    assert bucket["predicted"] == 0.6
    assert bucket["realized"] == 0.58
    assert bucket["abs_error"] == 0.02


def test_forecast_log_settle_report_and_edge_gate(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    low_edge = log_forecast(
        store,
        vertical="weather",
        category="weather",
        market="nyc-high-temp",
        p_you=Decimal("0.53"),
        p_market=Decimal("0.50"),
        p_base=Decimal("0.48"),
        executable_price=Decimal("0.50"),
        edge_min=Decimal("0.05"),
        record_paper_bet=True,
    )
    high_edge = log_forecast(
        store,
        vertical="weather",
        category="weather",
        market="den-high-temp",
        p_you=Decimal("0.62"),
        p_market=Decimal("0.50"),
        executable_price=Decimal("0.51"),
        edge_min=Decimal("0.05"),
        record_paper_bet=True,
    )

    settle_forecast(store, low_edge, 1, closing_price=Decimal("0.55"))
    settle_forecast(store, high_edge, 0, closing_price=Decimal("0.49"))
    data = report_data(store)

    assert data["summary"].n == 2
    assert data["mean_clv"] == 0.015
    with store.connect() as con:
        # Only high_edge (disagreement 0.12 >= 0.05) records a paper bet; low_edge does not.
        [open_bet] = list(con.execute("SELECT * FROM flat_bets WHERE kind='FORECAST' AND status='OPEN'"))
        [settled] = list(con.execute("SELECT * FROM flat_bets WHERE kind='FORECAST' AND status='SETTLED'"))
    assert "forecast-id=" in open_bet["notes"]
    # #10 edge is the comparable return-per-dollar at the executable price: (0.62-0.51)/0.51.
    assert open_bet["edge"] == "0.215686"
    assert open_bet["optimistic_cost"] == "0.51"
    # #4 settlement realized the bet: YES side, outcome 0 -> lost -$1.
    assert settled["result"] == "LOST"
    assert settled["pnl_optimistic"] == "-1.00"
