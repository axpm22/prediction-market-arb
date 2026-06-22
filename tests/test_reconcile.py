import json
from datetime import datetime, timezone
from decimal import Decimal

from arb.reconcile import reconcile_arb, reconcile_settlements
from arb.store import Store


# ---- pure reconciliation vectors ----

def test_clean_arb_one_winner_agrees() -> None:
    # legs cost 0.48 + 0.49 = 0.97 -> provisional 1/0.97-1 = 0.030928 -> $0.03 in $1 units.
    r = reconcile_arb([(Decimal("0.48"), "WON"), (Decimal("0.49"), "LOST")], Decimal("0.03"))
    assert r.agrees
    assert r.realized_pnl == Decimal("0.03")


def test_both_legs_lose_is_settler_error() -> None:
    # Settlement mismatch: neither leg paid (e.g. a void on the winning side at one venue).
    r = reconcile_arb([(Decimal("0.48"), "LOST"), (Decimal("0.49"), "LOST")], Decimal("0.03"))
    assert not r.agrees
    assert r.realized_pnl == Decimal("-1.00")   # lost the whole basket
    assert "no leg won" in r.detail


def test_both_legs_win_is_settler_error() -> None:
    r = reconcile_arb([(Decimal("0.48"), "WON"), (Decimal("0.49"), "WON")], Decimal("0.03"))
    assert not r.agrees
    assert "2 legs won" in r.detail
    # payout 2 / cost 0.97 - 1
    assert r.realized_pnl == (Decimal("2") / Decimal("0.97") - Decimal("1")).quantize(Decimal("0.01"))


def test_void_leg_refunds_partial() -> None:
    r = reconcile_arb([(Decimal("0.48"), "WON"), (Decimal("0.49"), "VOID")], Decimal("0.03"))
    assert not r.agrees   # a void means the lock didn't hold cleanly
    assert "void" in r.detail


# ---- end-to-end append against the ledger ----

def _settled_arb(store: Store, legs: list[dict], provisional: str) -> int:
    with store.connect() as con:
        cur = con.execute(
            """INSERT INTO flat_bets(fingerprint, opened_poll_id, category, sport, kind, trusted, live,
                   edge, stake_usd, legs_json, optimistic_cost, pnl_optimistic, status, placed_at,
                   result, notes)
               VALUES ('fp', 1, 'sports', 'baseball_mlb', 'ARB', 0, 0, '0.03', '1.00', ?, '0.97', ?,
                       'SETTLED', ?, 'RESOLVED', '')""",
            (json.dumps(legs), provisional, datetime.now(timezone.utc).isoformat()),
        )
        return int(cur.lastrowid)


def test_reconcile_settlements_appends_corrected_row_on_mismatch(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    legs = [{"venue": "kalshi", "book": "K", "outcome_label": "a", "cost": "0.48", "fee_cost": "0"},
            {"venue": "polymarket", "book": "P", "outcome_label": "b", "cost": "0.49", "fee_cost": "0"}]
    bet_id = _settled_arb(store, legs, "0.03")

    # Resolver says BOTH legs lost -> settlement mismatch.
    counts = reconcile_settlements(store, lambda leg: "LOST")
    assert counts == {"checked": 1, "agree": 0, "corrected": 1, "skipped": 0}
    with store.connect() as con:
        [row] = list(con.execute("SELECT * FROM flat_bets WHERE status='CORRECTED'"))
    assert row["reappearance_of"] == bet_id
    assert row["result"] == "SETTLER-ERROR"
    assert row["pnl_optimistic"] == "-1.00"   # realized differs from the +0.03 lock


def test_reconcile_settlements_agree_and_idempotent(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    legs = [{"venue": "kalshi", "book": "K", "outcome_label": "a", "cost": "0.48", "fee_cost": "0"},
            {"venue": "polymarket", "book": "P", "outcome_label": "b", "cost": "0.49", "fee_cost": "0"}]
    _settled_arb(store, legs, "0.03")
    results = iter(["WON", "LOST"])
    counts = reconcile_settlements(store, lambda leg: next(results))
    assert counts["agree"] == 1 and counts["corrected"] == 0
    # second run: already reconciled -> nothing checked
    assert reconcile_settlements(store, lambda leg: "WON")["checked"] == 0


def test_unresolved_leg_skipped(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    legs = [{"venue": "kalshi", "book": "K", "outcome_label": "a", "cost": "0.48", "fee_cost": "0"}]
    _settled_arb(store, legs, "0.03")
    assert reconcile_settlements(store, lambda leg: None)["skipped"] == 1
