"""ATTIC — tests for code parked in attic/unused_2026_07_28.py.

Not collected: pytest is pointed at tests/. Kept so that restoring a function
also restores the check that proved it worked. Imports below will need
repointing at whatever module the code is restored into.
"""
# from tests/test_metrics.py — covered arb.metrics.exit_policy_pnls (SPEC 10.8)
def test_exit_engine_vector_10_8():
    # EV bet: ask 0.50, Kalshi fee 0.0175 -> c 0.5175, p_hat 0.55, stake $1
    # later poll bid 0.58: exit fee 0.07*0.58*0.42 = 0.017052 -> exit 0.562948
    # P1 (capture 0.8): 0.045448 >= 0.8*0.0325 = 0.026 -> exits, pnl $0.087822
    bet = {"kind": "EV", "stake_usd": "1", "edge_points": "0.0325",
           "legs_json": json.dumps([{"venue": "kalshi", "cost": "0.5175",
                                     "outcome": "mets", "side": "YES"}])}
    quote_series = [{"bid": Decimal("0.53"), "at": NOW + timedelta(minutes=5)},
                    {"bid": Decimal("0.58"), "at": NOW + timedelta(minutes=10)}]
    pnls = exit_policy_pnls(CFG, bet, quote_series, category="sports")
    assert pnls["take_profit"] == Decimal("0.087822")
    assert "hold" not in pnls  # H10: dropped — hold-to-resolution is P0, computed elsewhere
