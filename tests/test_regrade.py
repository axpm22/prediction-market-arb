"""MODEL_FOUNDATIONS Phase A — regrade helpers + EV-subset test (fixtures-free)."""
from decimal import Decimal

from arb.experimental.analysis.regrade import _mid, _wilson_ci, ev_subset_test
from arb.store import Store


def test_mid_is_bid_ask_average_with_ask_fallback():
    assert _mid("0.60", "0.50") == Decimal("0.55")          # (bid+ask)/2
    assert _mid("0.60", None) == Decimal("0.60")            # one-sided book -> ask
    assert _mid("0.60", "") == Decimal("0.60")
    assert _mid("0.60", "0.70") == Decimal("0.60")          # crossed/absurd bid -> ask only


def test_wilson_ci_bounds():
    # 6/6 wins: point estimate 1.0, lower bound well below 1 (small N honesty).
    lo, hi = _wilson_ci(6, 6)
    assert hi == 1.0 and 0.5 < lo < 1.0
    assert _wilson_ci(0, 0) == (0.0, 0.0)


def _insert_ev(con, *, result, annotated_ann=False, fp="f"):
    cur = con.execute(
        """INSERT INTO flat_bets(fingerprint, opened_poll_id, category, sport, kind, trusted, live,
               edge, stake_usd, legs_json, status, placed_at, settled_at, result, notes)
           VALUES (?, 1, 'sports', 'baseball_mlb', 'EV', 0, 0, '0.05', '1.00', '[]', 'SETTLED',
                   '2026-07-02T14:00:00+00:00', '2026-07-02T20:00:00+00:00', ?, '')""",
        (fp, result))
    bet_id = int(cur.lastrowid)
    if annotated_ann:
        con.execute("INSERT INTO annotations(bet_id, tag, reason, created_at) VALUES (?,?,?,?)",
                    (bet_id, "bug:settled-rebook-loop", "loop", "2026-07-02T21:00:00+00:00"))
    return bet_id


def test_ev_subset_excludes_annotated_loop_rows(tmp_path):
    store = Store(tmp_path / "arb.sqlite")
    with store.connect() as con:
        _insert_ev(con, result="WON")                        # clean venue win
        _insert_ev(con, result="LOST")                       # clean venue loss
        _insert_ev(con, result="WON", annotated_ann=True)    # loop row -> excluded
        _insert_ev(con, result="WON", annotated_ann=True)    # loop row -> excluded
    result = ev_subset_test(store)
    assert result["n"] == 2                                  # only the two clean rows
    assert result["venue_wins"] == 1
    assert result["venue_win_rate"] == 0.5
