"""Maker-fill simulator. Pure — no sockets, no DB, no clock.

The whole point is the BRACKET: we cannot observe our queue position, so a
single fill number is a claim we cannot support. Conservative assumes we are
last in queue; optimistic assumes first.
"""
from decimal import Decimal

from arb.maker import MakerSim


def _sim(fee="0"):
    return MakerSim(fee_rate=Decimal(fee))


def test_no_fill_before_anything_trades_at_our_price():
    s = _sim()
    s.post("tokA", Decimal("0.40"), queue_ahead=Decimal("100"), posted_at=0.0)
    assert s.observe("tokA", Decimal("0.40"), Decimal("0"), at=1.0) == []


def test_optimistic_fills_on_the_first_size_at_our_price():
    """Front of queue: the first contract traded at our price is ours."""
    s = _sim()
    s.post("tokA", Decimal("0.40"), queue_ahead=Decimal("100"), posted_at=0.0)
    fills = s.observe("tokA", Decimal("0.40"), Decimal("10"), at=1.0)
    assert [f["basis"] for f in fills] == ["optimistic"]
    assert fills[0]["price"] == Decimal("0.40")


def test_conservative_waits_until_the_whole_queue_ahead_is_consumed():
    s = _sim()
    s.post("tokA", Decimal("0.40"), queue_ahead=Decimal("100"), posted_at=0.0)
    s.observe("tokA", Decimal("0.40"), Decimal("60"), at=1.0)
    assert s.observe("tokA", Decimal("0.40"), Decimal("30"), at=2.0) == []
    fills = s.observe("tokA", Decimal("0.40"), Decimal("20"), at=3.0)
    assert [f["basis"] for f in fills] == ["conservative"]
    assert fills[0]["at"] == 3.0


def test_each_basis_fills_at_most_once():
    s = _sim()
    s.post("tokA", Decimal("0.40"), queue_ahead=Decimal("1"), posted_at=0.0)
    first = s.observe("tokA", Decimal("0.40"), Decimal("500"), at=1.0)
    assert sorted(f["basis"] for f in first) == ["conservative", "optimistic"]
    assert s.observe("tokA", Decimal("0.40"), Decimal("500"), at=2.0) == []


def test_size_at_a_worse_price_never_fills_our_bid():
    """We bid 0.40. Trades at 0.41 are someone else's; only 0.40-or-better
    reaches our resting buy."""
    s = _sim()
    s.post("tokA", Decimal("0.40"), queue_ahead=Decimal("0"), posted_at=0.0)
    assert s.observe("tokA", Decimal("0.41"), Decimal("999"), at=1.0) == []
    assert s.observe("tokA", Decimal("0.40"), Decimal("1"), at=2.0) != []


def test_another_token_never_fills_our_order():
    s = _sim()
    s.post("tokA", Decimal("0.40"), queue_ahead=Decimal("0"), posted_at=0.0)
    assert s.observe("tokB", Decimal("0.40"), Decimal("999"), at=1.0) == []


def test_markout_marks_against_an_executable_exit_never_a_mid():
    """Measurement rule 1: mids are banned. A maker BUY exits by selling into
    the bid, so the bid is the honest mark."""
    from arb.maker import markout
    fill = {"token": "tokA", "price": Decimal("0.40"), "at": 0.0,
            "basis": "conservative"}
    got = markout(fill, exit_bid=Decimal("0.43"), fee=Decimal("0.005"))
    assert got == Decimal("0.025")           # 0.43 - 0.40 - 0.005


def test_markout_is_negative_when_the_book_moves_against_the_fill():
    from arb.maker import markout
    fill = {"token": "tokA", "price": Decimal("0.40"), "at": 0.0,
            "basis": "conservative"}
    assert markout(fill, exit_bid=Decimal("0.37"),
                   fee=Decimal("0.005")) == Decimal("-0.035")


def test_fee_is_charged_on_every_fill():
    """A maker rebate is not free money — the fee still has to clear."""
    from arb.maker import markout
    fill = {"token": "tokA", "price": Decimal("0.40"), "at": 0.0,
            "basis": "optimistic"}
    assert markout(fill, exit_bid=Decimal("0.40"),
                   fee=Decimal("0.01")) == Decimal("-0.01")


def test_bracket_reports_both_bounds_and_names_the_primary():
    from arb.maker import bracket
    fills = [{"basis": "conservative", "markout": Decimal("-0.01")},
             {"basis": "conservative", "markout": Decimal("0.03")},
             {"basis": "optimistic", "markout": Decimal("0.05")}]
    b = bracket(fills)
    assert b["conservative"]["n"] == 2
    assert b["conservative"]["mean"] == Decimal("0.01")
    assert b["optimistic"]["n"] == 1
    assert b["primary"] == "conservative"


def test_bracket_on_no_fills_is_empty_not_zero():
    """Zero mean from zero fills reads as 'measured, no edge'. It is not
    measured at all."""
    from arb.maker import bracket
    b = bracket([])
    assert b["conservative"]["n"] == 0
    assert b["conservative"]["mean"] is None


# --- replay (World A) -------------------------------------------------------
# DEVIATION from the plan, stated deliberately: the plan wrote replay for
# World B (fills inferred from book deltas) because trade prints were assumed
# absent. Task 1 verified they exist. Real prints remove the limitation the
# plan recorded as unfixable — a book delta cannot distinguish a TRADE from a
# CANCELLATION, and counting cancellations biases every maker number upward.

def _cfg():
    from pathlib import Path
    from arb.config import load
    return load(Path(__file__).resolve().parent.parent / "config.yaml")


def _book(tok, price, size, ts):
    return {"event_type": "book", "asset_id": tok, "timestamp": str(ts),
            "bids": [{"price": str(price), "size": str(size)}],
            "asks": [{"price": "0.99", "size": "50"}]}


def _trade(tok, price, size, ts):
    return {"event_type": "last_trade_price", "asset_id": tok,
            "price": str(price), "size": str(size), "side": "SELL",
            "fee_rate_bps": "0", "timestamp": str(ts), "transaction_hash": "0x1"}


def _pc(tok, best_bid, ts):
    return {"event_type": "price_change", "timestamp": str(ts),
            "price_changes": [{"asset_id": tok, "price": str(best_bid),
                               "size": "1", "side": "BUY",
                               "best_bid": str(best_bid), "best_ask": "0.99"}]}


def test_replay_fills_from_real_trade_prints(tmp_path):
    """A print is unambiguous: it says a trade happened at a price for a size."""
    from arb import bench_maker, pm_tape
    con = pm_tape.open_tape(tmp_path / "t.sqlite")
    frames = [_book("tokA", "0.40", "10", 1),          # post at 0.40, 10 ahead
              _trade("tokA", "0.40", "50", 2),         # 50 traded -> both bases
              _pc("tokA", "0.45", 3)]                  # later executable exit
    pm_tape.persist_frames(con, frames, [1.0, 2.0, 100.0])
    res = bench_maker.replay(_cfg(), con)
    assert res["fills"] == 2                            # optimistic + conservative
    assert res["prints"] == 1


def test_replay_ignores_book_size_shrinking_because_it_may_be_a_cancellation(tmp_path):
    """The whole reason Task 1 mattered. Size leaving a level is NOT evidence
    of a trade — it is equally a cancellation. Only a print counts."""
    from arb import bench_maker, pm_tape
    con = pm_tape.open_tape(tmp_path / "t.sqlite")
    frames = [_book("tokA", "0.40", "100", 1),
              _book("tokA", "0.40", "3", 2),           # 97 vanished, no print
              _pc("tokA", "0.45", 3)]
    pm_tape.persist_frames(con, frames, [1.0, 2.0, 100.0])
    res = bench_maker.replay(_cfg(), con)
    assert res["prints"] == 0
    assert res["fills"] == 0


def test_replay_marks_out_against_a_later_executable_bid(tmp_path):
    from arb import bench_maker, pm_tape
    con = pm_tape.open_tape(tmp_path / "t.sqlite")
    frames = [_book("tokA", "0.40", "0", 1),
              _trade("tokA", "0.40", "5", 2),
              _pc("tokA", "0.45", 3)]
    pm_tape.persist_frames(con, frames, [1.0, 2.0, 200.0])
    res = bench_maker.replay(_cfg(), con)
    assert res["marked"] >= 1
    # 0.45 exit - 0.40 fill - maker fee > 0 on a 5c favourable move
    assert res["bracket"]["conservative"]["mean"] > Decimal("0")


def test_replay_leaves_unmarked_fills_censored_rather_than_scoring_them_zero(tmp_path):
    """No later bid means no mark. Scoring it 0 would invent a flat trade."""
    from arb import bench_maker, pm_tape
    con = pm_tape.open_tape(tmp_path / "t.sqlite")
    frames = [_book("tokA", "0.40", "0", 1), _trade("tokA", "0.40", "5", 2)]
    pm_tape.persist_frames(con, frames, [1.0, 2.0])
    res = bench_maker.replay(_cfg(), con)
    assert res["fills"] == 2 and res["marked"] == 0
    assert res["bracket"]["conservative"]["mean"] is None


def test_replay_on_an_empty_tape_reports_nothing_measured(tmp_path):
    from arb import bench_maker, pm_tape
    con = pm_tape.open_tape(tmp_path / "t.sqlite")
    res = bench_maker.replay(_cfg(), con)
    assert res["fills"] == 0
    assert res["bracket"]["conservative"]["mean"] is None


# --- re-posting -------------------------------------------------------------
# The 2026-07-31 run produced 35 fills from 817 prints because replay posted
# ONE order per token and never re-posted, capping fills at 2 x tokens however
# long the tape ran. A market maker re-posts continuously — that is what making
# a market IS.

def test_replay_reposts_after_a_fill_so_one_token_yields_many_observations():
    from arb import bench_maker, pm_tape
    import tempfile, os
    with tempfile.TemporaryDirectory() as d:
        con = pm_tape.open_tape(os.path.join(d, "t.sqlite"))
        frames, times, t = [], [], 1.0
        for i in range(4):                    # four post -> fill -> re-post cycles
            frames.append(_book("tokA", "0.40", "0", i))
            times.append(t); t += 1
            frames.append(_trade("tokA", "0.40", "5", i))
            times.append(t); t += 1
        frames.append(_pc("tokA", "0.45", 99))
        times.append(t + 1000)
        pm_tape.persist_frames(con, frames, times)
        res = bench_maker.replay(_cfg(), con)
        # without re-posting this is capped at 2; with it, one per cycle per basis
        assert res["fills"] > 2, f"expected re-posting, got {res['fills']}"
        assert res["prints"] == 4


def test_bracket_ci_clusters_by_token_not_by_fill():
    """Re-posting makes many fills per token, and fills within a token are
    correlated. Dividing by sqrt(n_fills) would manufacture precision the data
    does not have — the CI must widen to sqrt(n_tokens), as l7_maker does."""
    from arb.maker import bracket
    many = [{"basis": "conservative", "markout": Decimal("0.01"), "token": "tokA"},
            {"basis": "conservative", "markout": Decimal("0.03"), "token": "tokA"},
            {"basis": "conservative", "markout": Decimal("-0.01"), "token": "tokA"},
            {"basis": "conservative", "markout": Decimal("0.02"), "token": "tokA"}]
    one_cluster = bracket(many)["conservative"]
    spread_out = [{**f, "token": f"tok{i}"} for i, f in enumerate(many)]
    four_clusters = bracket(spread_out)["conservative"]
    assert one_cluster["n"] == four_clusters["n"] == 4
    assert one_cluster["mean"] == four_clusters["mean"]
    width_1 = one_cluster["hi"] - one_cluster["lo"]
    width_4 = four_clusters["hi"] - four_clusters["lo"]
    assert width_1 > width_4, "one cluster must give a WIDER interval than four"


def test_bracket_reports_the_cluster_count_so_power_is_readable():
    from arb.maker import bracket
    fills = [{"basis": "conservative", "markout": Decimal("0.01"), "token": "a"},
             {"basis": "conservative", "markout": Decimal("0.02"), "token": "a"},
             {"basis": "conservative", "markout": Decimal("0.03"), "token": "b"}]
    s = bracket(fills)["conservative"]
    assert s["n"] == 3 and s["clusters"] == 2
