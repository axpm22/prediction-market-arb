from datetime import datetime, timezone

from arb.bias import calibration
from arb.store import Store

BANDS = [0.0, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 1.0]


def seed(store: Store, cost: float, won: bool, n: int) -> None:
    with store.connect() as con:
        for i in range(n):
            con.execute(
                """INSERT INTO flat_bets(fingerprint, opened_poll_id, category, sport, kind, trusted, live,
                       edge, stake_usd, legs_json, status, placed_at, result, optimistic_cost, quality_flag, notes)
                   VALUES (?,1,'sports','x','EV',1,0,'0','1.00','[]','SETTLED',?,?,?,NULL,'')""",
                (f"{cost}{won}{i}", datetime.now(timezone.utc).isoformat(),
                 "WON" if won else "LOST", str(cost)),
            )


def test_longshot_band_overpriced_flagged_avoid() -> None:
    import tempfile, pathlib
    store = Store(pathlib.Path(tempfile.mkdtemp()) / "a.sqlite")
    # 0.05-0.10 band (implied mid 0.075): only 2% actually win -> realized << implied -> avoid.
    seed(store, 0.07, True, 4)
    seed(store, 0.07, False, 196)
    band = next(s for s in calibration(store, BANDS, 200, 0.02) if s.lo == 0.05)
    assert band.n == 200
    assert band.signal == "avoid"
    assert band.edge < 0


def test_favorite_band_underpriced_flagged_back() -> None:
    import tempfile, pathlib
    store = Store(pathlib.Path(tempfile.mkdtemp()) / "a.sqlite")
    # 0.80-0.90 band (implied mid 0.85): 95% win -> realized > implied -> back.
    seed(store, 0.85, True, 190)
    seed(store, 0.85, False, 10)
    band = next(s for s in calibration(store, BANDS, 200, 0.02) if s.lo == 0.8)
    assert band.signal == "back"
    assert band.edge > 0


def test_small_band_is_insufficient() -> None:
    import tempfile, pathlib
    store = Store(pathlib.Path(tempfile.mkdtemp()) / "a.sqlite")
    seed(store, 0.5, True, 10)
    band = next(s for s in calibration(store, BANDS, 200, 0.02) if s.lo == 0.5)
    assert band.signal == "INSUFFICIENT_DATA"
