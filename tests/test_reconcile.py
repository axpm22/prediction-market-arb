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
    assert {k: counts[k] for k in ("checked", "agree", "corrected", "skipped")} == {
        "checked": 1, "agree": 0, "corrected": 1, "skipped": 0}
    assert counts["provisional"] == 0   # the one settled arb now has a CORRECTED cross-check
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


# ---- Phase 1 / audit A2: per-venue resolvers (recorded settled fixtures) ----

import json as _json
from pathlib import Path as _Path

from arb.reconcile import provisional_arb_count
from arb.venues.polymarket import polymarket_leg_result, resolve_polymarket_market
from arb.venues.sx import resolve_sx_market, sx_leg_result

FIX = _Path("tests/fixtures")


def test_polymarket_resolver_won_lost_void_none() -> None:
    market = _json.loads((FIX / "polymarket_settled.json").read_text())
    assert resolve_polymarket_market(market, "tokWin") == "WON"
    assert resolve_polymarket_market(market, "tokLose") == "LOST"
    void = _json.loads((FIX / "polymarket_settled_void.json").read_text())
    assert resolve_polymarket_market(void, "tokA") == "VOID"
    # An unknown token, or a market that has not closed, returns None (skip, never guess).
    assert resolve_polymarket_market(market, "nope") is None
    assert resolve_polymarket_market({"closed": False, "clobTokenIds": "[\"x\"]", "outcomePrices": "[\"1\"]"}, "x") is None


def test_polymarket_leg_result_dispatch_and_fetch_injection() -> None:
    market = _json.loads((FIX / "polymarket_settled.json").read_text())
    leg = {"venue": "polymarket", "book": "tokWin"}
    assert polymarket_leg_result(leg, fetch_market=lambda _t: market) == "WON"
    # Non-poly leg ignored; empty fetch -> None.
    assert polymarket_leg_result({"venue": "kalshi", "book": "K"}, fetch_market=lambda _t: market) is None
    assert polymarket_leg_result(leg, fetch_market=lambda _t: None) is None


def test_sx_resolver_uses_ref_side() -> None:
    market = _json.loads((FIX / "sx_settled.json").read_text())  # outcome=1
    assert resolve_sx_market(market, "1") == "WON"   # leg backed outcome one, which won
    assert resolve_sx_market(market, "2") == "LOST"  # leg backed outcome two, which lost
    assert resolve_sx_market(market, None) is None   # legs without the side hint stay unverifiable
    assert resolve_sx_market({"status": "VOID", "outcome": 0}, "1") == "VOID"
    assert resolve_sx_market({"status": "ACTIVE", "outcome": 0}, "1") is None  # not reported yet


def test_sx_leg_result_dispatch() -> None:
    market = _json.loads((FIX / "sx_settled.json").read_text())
    leg = {"venue": "sx", "book": "0xSETTLED", "ref": "2"}
    assert sx_leg_result(leg, fetch_market=lambda _h: market) == "LOST"
    assert sx_leg_result({"venue": "polymarket", "book": "x"}, fetch_market=lambda _h: market) is None


def test_venue_dispatch_resolves_what_kalshi_only_skipped(tmp_path) -> None:
    """The bug: a kalshi-only resolver returns None on the poly leg and skips the whole
    bet. With the venue map, each leg resolves with its own venue's resolver."""
    store = Store(tmp_path / "arb.sqlite")
    legs = [{"venue": "kalshi", "book": "K", "outcome_label": "a", "cost": "0.48", "fee_cost": "0"},
            {"venue": "polymarket", "book": "tokWin", "outcome_label": "b", "cost": "0.49", "fee_cost": "0"}]
    _settled_arb(store, legs, "0.03")
    market = _json.loads((FIX / "polymarket_settled.json").read_text())

    kalshi_only = lambda leg: "LOST" if leg["venue"] == "kalshi" else None
    # Before: single kalshi-only resolver skips because the poly leg is unresolved.
    assert reconcile_settlements(store, kalshi_only)["skipped"] == 1
    # After: venue map resolves the poly leg via its own resolver -> the arb is checked.
    resolvers = {
        "kalshi": lambda leg: "LOST",                                  # kalshi leg lost
        "polymarket": lambda leg: polymarket_leg_result(leg, lambda _t: market),  # poly leg WON
    }
    counts = reconcile_settlements(store, resolvers)
    assert counts["checked"] == 1
    # kalshi LOST + poly WON == clean one-winner lock -> RECONCILED, agrees.
    assert counts["agree"] == 1 and counts["corrected"] == 0
    assert counts["provisional"] == 0


def test_poly_sx_arb_venue_disagreement_is_corrected(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    legs = [{"venue": "polymarket", "book": "tokWin", "outcome_label": "a", "cost": "0.48", "fee_cost": "0"},
            {"venue": "sx", "book": "0xSETTLED", "ref": "1", "outcome_label": "b", "cost": "0.49", "fee_cost": "0"}]
    _settled_arb(store, legs, "0.03")
    market = _json.loads((FIX / "polymarket_settled.json").read_text())  # poly tokWin -> WON
    sx_market = _json.loads((FIX / "sx_settled.json").read_text())       # sx outcome=1, ref=1 -> WON
    resolvers = {
        "polymarket": lambda leg: polymarket_leg_result(leg, lambda _t: market),
        "sx": lambda leg: sx_leg_result(leg, lambda _h: sx_market),
    }
    counts = reconcile_settlements(store, resolvers)
    # Both legs WON -> venues resolved inconsistently -> CORRECTED settler-error.
    assert counts["corrected"] == 1
    with store.connect() as con:
        [row] = list(con.execute("SELECT * FROM flat_bets WHERE status='CORRECTED'"))
    assert row["result"] == "SETTLER-ERROR"
    assert "2 legs won" in row["notes"]


def test_provisional_count_tracks_unreconciled(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    legs = [{"venue": "kalshi", "book": "K", "outcome_label": "a", "cost": "0.48", "fee_cost": "0"},
            {"venue": "polymarket", "book": "P", "outcome_label": "b", "cost": "0.49", "fee_cost": "0"}]
    _settled_arb(store, legs, "0.03")
    with store.connect() as con:
        assert provisional_arb_count(con) == 1   # settled, no cross-check yet -> provisional
    reconcile_settlements(store, {"kalshi": lambda l: "WON", "polymarket": lambda l: "LOST"})
    with store.connect() as con:
        assert provisional_arb_count(con) == 0   # now reconciled -> no longer provisional
