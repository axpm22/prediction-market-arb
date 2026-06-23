"""Phase 2 / audit N1: the CLV go/no-go gate must default to the UNBIASED no-vig metric.

Loading fees on both ends biases mean CLV toward/below zero, so the fee-loaded gate can read
"NOT proven" on an edge the no-vig metric shows is real — a false negative that keeps EV
disabled. These tests pin a fixture where the two metrics disagree and assert the default
(no-vig) verdict flips to "edge demonstrated", and that quarantined flags are excluded.
"""
import json

from decimal import Decimal

from arb.experimental.ev.clv import _ev_flags, clv_result, grade_clv, grade_clv_novig, main
from arb.store import Store

T0 = "2026-06-10T18:00:00+00:00"      # entry time
CLOSE = "2026-06-10T20:00:00+00:00"   # later = the close
EVK = "baseball_mlb:2026-06-10:team a:team b:1"


def _ev_flag(con, *, quality_flag=None, cost="0.48", fee="0.06") -> None:
    leg = {"event_key": EVK, "venue": "polymarket", "book": "B",
           "outcome_label": "team a", "cost": cost, "fee_cost": fee}
    con.execute(
        """INSERT INTO flat_bets(fingerprint, opened_poll_id, category, sport, kind, trusted, live,
               edge, stake_usd, legs_json, status, placed_at, notes, quality_flag)
           VALUES ('fp', 1, 'sports', 'baseball_mlb', 'EV', 0, 0, '0.02', '1.00', ?, 'OPEN', ?, '', ?)""",
        (json.dumps([leg]), T0, quality_flag),
    )


def _quote(con, outcome, raw, fetched_at, *, depth="100") -> None:
    con.execute(
        """INSERT INTO quotes(fetched_at, event_key, sport, venue, book, market_type, line,
               outcome, raw_price, cost, fee_cost, depth_usd, live)
           VALUES (?, ?, 'baseball_mlb', 'polymarket', 'B', 'WINNER', NULL, ?, ?, ?, '0.00', ?, 0)""",
        (fetched_at, EVK, outcome, raw, raw, depth),
    )


def _seed(store: Store) -> None:
    with store.connect() as con:
        _ev_flag(con)
        # Two-sided book. At entry: A 0.48 / B 0.55 (mid_A = 0.48/1.03 = 0.4660).
        # At close:    A 0.52 / B 0.52 (mid_A = 0.52/1.04 = 0.5000).  no-vig CLV = +0.034.
        _quote(con, "team a", "0.48", T0)
        _quote(con, "team b", "0.55", T0)
        _quote(con, "team a", "0.52", CLOSE)
        _quote(con, "team b", "0.52", CLOSE)


def test_fee_loaded_and_novig_verdicts_disagree(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    _seed(store)
    # Fee-loaded close (latest A effective = 0.52) minus fee-loaded entry_ask (0.54) = -0.02.
    fee_loaded = grade_clv(store, Decimal("50"))
    assert fee_loaded["n"] == 1
    assert fee_loaded["ci95"][0] <= 0          # the biased gate: "NOT proven"
    # No-vig mid moved +0.034 toward us.
    novig = grade_clv_novig(store)
    assert novig["n"] == 1
    assert novig["ci95"][0] > 0                # the unbiased gate: edge demonstrated


def test_default_verdict_is_novig_and_flips(tmp_path, capsys) -> None:
    store = Store(tmp_path / "arb.sqlite")
    _seed(store)
    # clv_result default (fee_loaded=False) == the no-vig metric.
    assert clv_result(store, fee_loaded=False, min_depth=Decimal("50")) == grade_clv_novig(store)

    import arb.experimental.ev.clv as clv_mod
    orig = clv_mod.load_config
    clv_mod.load_config = lambda: type("C", (), {"database_path": tmp_path / "arb.sqlite"})()
    try:
        main([])                                # default = no-vig gate
        default_out = capsys.readouterr().out
        main(["--fee-loaded"])                  # explicit biased metric
        fee_out = capsys.readouterr().out
    finally:
        clv_mod.load_config = orig
    assert "edge demonstrated" in default_out           # default flips to proven
    assert "no-vig mid (default gate)" in default_out
    assert "NOT proven" in fee_out                       # biased metric still says no


def test_quarantined_flags_excluded_from_sample(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    _seed(store)
    with store.connect() as con:
        _ev_flag(con, quality_flag="uneconomic-horizon")   # quarantined — must not count
        _ev_flag(con, quality_flag="below-min-profit")
    flags = _ev_flags(store)
    assert len(flags) == 1                                  # only the clean flag
    assert all(f["event_key"] == EVK for f in flags)
