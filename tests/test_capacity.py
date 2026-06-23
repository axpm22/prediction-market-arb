import json
from datetime import datetime, timezone
from decimal import Decimal

from arb.capacity import arb_capacity, ladder_scaled_stake, max_economic_arb_size
from arb.experimental.analysis.arb_capacity import run_arb_capacity
from arb.store import Store


def test_two_level_breakeven_and_profitmax() -> None:
    # Top of book: 0.48 + 0.49 = 0.97 (3.1% lock), $480/$490 deep. Deeper levels are worse:
    # leg A second level 0.53, leg B 0.52 -> filling past the top eventually pushes Σ over 1.
    a = [(Decimal("0.48"), Decimal("48")), (Decimal("0.53"), Decimal("530"))]   # 100 then ~1000 contracts
    b = [(Decimal("0.49"), Decimal("49")), (Decimal("0.52"), Decimal("520"))]
    cap = arb_capacity([a, b])
    assert cap.feasible
    # top-level fills 100 contracts each at 0.48/0.49 -> profit 100*(1-0.97)=~3 there.
    assert cap.max_profit_usd > 0
    assert cap.limited_by in ("edge", "depth")
    # break-even must be >= profit-max size (you can stake more before going negative than the
    # point that maximizes profit).
    assert cap.breakeven_stake_usd >= cap.profitmax_stake_usd


def test_single_level_is_depth_limited() -> None:
    # One level each, sum 0.97 < 1: the whole book is profitable -> depth-limited, break-even
    # is the fully-filled stake.
    cap = arb_capacity([[(Decimal("0.48"), Decimal("480"))], [(Decimal("0.49"), Decimal("490"))]])
    assert cap.feasible and cap.limited_by == "depth"
    assert cap.profitmax_stake_usd > 0


def test_no_arb_when_sum_exceeds_one() -> None:
    cap = arb_capacity([[(Decimal("0.55"), Decimal("100"))], [(Decimal("0.50"), Decimal("100"))]])
    assert not cap.feasible


def test_max_economic_arb_size_from_legs_uses_ladder() -> None:
    legs = [
        {"cost": "0.48", "fee_cost": "0", "depth_usd": "48",
         "ladder": [["0.48", "48"], ["0.53", "530"]]},
        {"cost": "0.49", "fee_cost": "0", "depth_usd": "49",
         "ladder": [["0.49", "49"], ["0.52", "520"]]},
    ]
    cap = max_economic_arb_size(legs)
    assert cap.feasible and cap.profitmax_stake_usd > 0
    stake = ladder_scaled_stake(legs, bankroll=Decimal("1000"), max_stake_pct=Decimal("0.10"))
    assert stake > 0 and stake <= Decimal("100")   # capped by bankroll*max_stake_pct


def test_arb_capacity_report(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    legs = [
        {"venue": "kalshi", "outcome_label": "a", "cost": "0.48", "fee_cost": "0",
         "depth_usd": "48", "ladder": [["0.48", "48"], ["0.53", "530"]]},
        {"venue": "polymarket", "outcome_label": "b", "cost": "0.49", "fee_cost": "0",
         "depth_usd": "49", "ladder": [["0.49", "49"], ["0.52", "520"]]},
    ]
    with store.connect() as con:
        con.execute(
            """INSERT INTO flat_bets(fingerprint, opened_poll_id, category, sport, kind, trusted, live,
                   edge, stake_usd, legs_json, status, placed_at, notes)
               VALUES ('a1', 1, 'sports', 'baseball_mlb', 'ARB', 1, 0, '0.0309', '1.00', ?, 'OPEN', ?, '')""",
            (json.dumps(legs), datetime.now(timezone.utc).isoformat()),
        )
    r = run_arb_capacity(store)
    assert r.n_arbs == 1 and r.n_feasible == 1 and r.n_with_ladder == 1
    assert r.median_breakeven_usd is not None and r.median_breakeven_usd > 0


def test_arb_capacity_dedups_fingerprints_and_drops_review(tmp_path) -> None:
    # B2: the same arb re-observed across polls is ONE opportunity, and untrusted REVIEW
    # arbs (hard rule #6) are not actionable. Both must be excluded from the headline sum.
    store = Store(tmp_path / "arb.sqlite")
    legs = [
        {"venue": "kalshi", "outcome_label": "a", "cost": "0.48", "fee_cost": "0",
         "depth_usd": "48", "ladder": [["0.48", "48"], ["0.53", "530"]]},
        {"venue": "polymarket", "outcome_label": "b", "cost": "0.49", "fee_cost": "0",
         "depth_usd": "49", "ladder": [["0.49", "49"], ["0.52", "520"]]},
    ]
    legs_json = json.dumps(legs)
    now = datetime.now(timezone.utc).isoformat()
    with store.connect() as con:
        # Same fingerprint 'a1' seen three times (re-observation inflation) + a REVIEW row.
        for status in ("OPEN", "SETTLED", "SETTLED"):
            con.execute(
                """INSERT INTO flat_bets(fingerprint, opened_poll_id, category, sport, kind, trusted, live,
                       edge, stake_usd, legs_json, status, placed_at, notes)
                   VALUES ('a1', 1, 'sports', 'baseball_mlb', 'ARB', 1, 0, '0.0309', '1.00', ?, ?, ?, '')""",
                (legs_json, status, now),
            )
        con.execute(
            """INSERT INTO flat_bets(fingerprint, opened_poll_id, category, sport, kind, trusted, live,
                   edge, stake_usd, legs_json, status, placed_at, notes)
               VALUES ('review1', 1, 'sports', 'baseball_mlb', 'ARB', 0, 0, '0.0309', '1.00', ?, 'OPEN', ?, '')""",
            (legs_json, now),
        )
    r = run_arb_capacity(store)
    # 3 sightings of 'a1' collapse to 1; 'review1' (trusted=0) is dropped entirely.
    assert r.n_arbs == 1 and r.n_feasible == 1
    # Opting back into REVIEW rows still dedups: 'a1' + 'review1' = 2 distinct fingerprints.
    r_all = run_arb_capacity(store, trusted_only=False)
    assert r_all.n_arbs == 2
