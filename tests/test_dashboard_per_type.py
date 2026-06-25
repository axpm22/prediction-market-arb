"""Dashboard control-center: per-bet-type performance section (DASHBOARD_PROMPT requirement b/c).

Each ledger kind is its own line with $ and %, confirmed/clean by default, plus version columns
(re-grades of the same bets). Arbs lock at entry (pnl_optimistic) with NULL confirmed, so the
mirage_gap surfaces honestly. No secret ever appears in the served payload.
"""
import json
from dataclasses import replace
from datetime import datetime, timezone

from arb.config import load_config
from arb.experimental.dashboard import CANONICAL_KINDS, build_dashboard_data
from arb.store import Store


def _seed(store, kind, *, result, pnl_optimistic, pnl_confirmed, stake="1.00", quality_flag=None):
    with store.connect() as con:
        con.execute(
            """INSERT INTO flat_bets(fingerprint, opened_poll_id, category, sport, kind, trusted, live,
                   edge, stake_usd, legs_json, status, placed_at, result, pnl_optimistic, pnl_confirmed,
                   quality_flag, notes)
               VALUES (?, 1, 'sports', 'baseball_mlb', ?, 1, 0, '0.03', ?, '[]', 'SETTLED', ?, ?, ?, ?, ?, '')""",
            (f"{kind}-{result}-{pnl_optimistic}-{quality_flag}", kind, stake,
             datetime.now(timezone.utc).isoformat(), result, pnl_optimistic, pnl_confirmed, quality_flag),
        )


def _row(data, kind):
    return next(r for r in data["per_type"] if r["kind"] == kind)


def test_per_type_separates_kinds_with_money_and_percent(tmp_path) -> None:
    config = replace(load_config(), database_path=tmp_path / "arb.sqlite")
    store = Store(config.database_path)
    # EV: one win (+0.50) and one loss (-1.00), both confirmed -> roi = -0.50/2.00 = -0.25.
    _seed(store, "EV", result="WON", pnl_optimistic="0.50", pnl_confirmed="0.50")
    _seed(store, "EV", result="LOST", pnl_optimistic="-1.00", pnl_confirmed="-1.00")
    # ARB: a held lock (+0.03), RESOLVED, confirmed NULL (locks aren't "confirmed" the EV way).
    _seed(store, "ARB", result="RESOLVED", pnl_optimistic="0.03", pnl_confirmed=None)

    data = build_dashboard_data(store, config)

    # Every canonical kind is its own line, even 0-row ones — never blended.
    kinds = [r["kind"] for r in data["per_type"]]
    for k in CANONICAL_KINDS:
        assert k in kinds

    ev = _row(data, "EV")
    assert ev["n"] == 2 and ev["win_rate"] == 0.5
    assert ev["pnl"] == -0.5 and ev["staked"] == 2.0 and ev["roi"] == -0.25   # confirmed/clean $/%

    arb = _row(data, "ARB")
    assert arb["win_rate"] is None                       # RESOLVED locks have no WON/LOST win rate
    assert arb["pnl"] == 0.0                             # confirmed NULL -> default confirmed view is 0
    assert arb["versions"]["optimistic_clean_flat"]["pnl"] == 0.03   # the lock shows in optimistic
    assert arb["mirage_gap"] == 0.03                     # optimistic minus confirmed
    assert arb["versions_are_regrades"] is True          # not independent bets
    assert arb["status"] == "measurement-only"


def test_headline_is_confirmed_clean_and_roi_matches_sql(tmp_path) -> None:
    config = replace(load_config(), database_path=tmp_path / "arb.sqlite")
    store = Store(config.database_path)
    _seed(store, "EV", result="WON", pnl_optimistic="0.50", pnl_confirmed="0.50")
    _seed(store, "EV", result="LOST", pnl_optimistic="-1.00", pnl_confirmed="-1.00")
    _seed(store, "EV", result="WON", pnl_optimistic="2.00", pnl_confirmed=None, quality_flag="suspect-edge")  # raw-only

    data = build_dashboard_data(store, config)
    assert data["headline"]["basis"] == "confirmed / clean / flat-$1"
    assert data["headline"]["total_profit"] == -0.5     # only the two clean confirmed bets

    # Spot-check: the UI ROI for a kind must match a direct SQL aggregate over flat_bets.
    with store.connect() as con:
        row = con.execute(
            """SELECT COALESCE(SUM(CAST(pnl_confirmed AS REAL)),0) p, COALESCE(SUM(CAST(stake_usd AS REAL)),0) s
                 FROM flat_bets WHERE kind='EV' AND quality_flag IS NULL AND pnl_confirmed IS NOT NULL"""
        ).fetchone()
    assert _row(data, "EV")["roi"] == round(row[0] / row[1], 4)


def test_served_payload_has_no_secret(tmp_path) -> None:
    config = replace(load_config(), database_path=tmp_path / "arb.sqlite",
                     odds_api_key="SECRET-TEST-KEY-123")
    store = Store(config.database_path)
    _seed(store, "EV", result="WON", pnl_optimistic="0.50", pnl_confirmed="0.50")
    payload = json.dumps(build_dashboard_data(store, config))
    assert "SECRET-TEST-KEY-123" not in payload
    assert "ODDS_API_KEY" not in payload and "apiKey" not in payload
