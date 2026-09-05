"""tools/backtest_whale_follow.py — the four things that can silently lie.

The P&L formula (it must be `realized_edge`'s own accounting, per dollar,
minus the venue fee), the consensus rule (a SECOND distinct wallet within
24 h), the exit rule (the sell that takes cumulative sells over 50%), and the
no-look-ahead guard (a market that resolves after `as_of` must not move a
score). Every tape here is synthetic and lives in `tmp_path` — nothing in this
file opens `data/`.
"""
import copy
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from arb.config import load
from arb.venues.fees import leg_fee_cost
from arb.whale_mat import materialize
from arb.whale_score import Position, realized_edge
from arb.whale_tape import open_tape
from tests.test_whale_lane import ROOT

sys.path.insert(0, str(ROOT))
from tools import backtest_whale_follow as bt  # noqa: E402

T0 = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)
SPORT, MTYPE = "baseball_mlb", "WINNER"


@pytest.fixture
def cfg():
    return copy.deepcopy(load(ROOT / "config.yaml"))


@pytest.fixture
def con(tmp_path):
    return open_tape(tmp_path / "tape.sqlite")


def _market(con, cid, start):
    con.execute("INSERT INTO markets (condition_id, sport, market_type, start_utc,"
                " tokens_json, first_seen, last_seen) VALUES (?,?,?,?,'[]',?,?)",
                (cid, SPORT, MTYPE, start.isoformat(),
                 T0.isoformat(), T0.isoformat()))
    con.commit()


def _resolution(con, cid, winner, at):
    con.execute("INSERT INTO resolutions (condition_id, resolved_at,"
                " winner_outcome_index, status, source, raw_json, fetched_at)"
                " VALUES (?,?,?,'resolved','gamma','{}',?)",
                (cid, at.isoformat(), winner, at.isoformat()))
    con.commit()


def _print(con, wallet, cid, when, side="BUY", price="0.40", size="1000", idx=0):
    con.execute(
        "INSERT INTO prints (observed_at, ts, tx_hash, proxy_wallet, condition_id,"
        " asset, outcome_index, side, price, size, notional_usd, event_slug, source,"
        " raw_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'tape','{}')",
        (T0.isoformat(), int(when.timestamp()),
         f"0x{wallet}{cid}{int(when.timestamp())}{side}{price}{size}{idx}",
         wallet, cid, f"tok{idx}", idx, side, price, size,
         str(Decimal(price) * Decimal(size)), f"evt-{cid}"))
    con.commit()


def _rows(con, cfg, now=None):
    """The tool's candidate rows over whatever tape the test just built."""
    materialize(con, cfg, now or T0 + timedelta(days=30), cap=0)
    positions, res, mk = bt.load_tape(con)
    wf = bt.walk_forward(positions, res, cfg)
    return bt.build_rows(positions, res, mk, bt.entry_exit(con, cfg), wf, cfg), wf


# ------------------------------------------------------------------ the P&L

def test_pnl_is_realized_edge_per_dollar_minus_the_venue_fee(cfg):
    """payout/entry − 1, per `realized_edge`'s own accounting, minus one
    Polymarket taker fee at the entry price."""
    pos = Position(wallet="0xw", condition_id="0xc", outcome_index=0,
                   avg_price=Decimal("0.40"), size=Decimal(1000),
                   notional_usd=Decimal(400), entry_ts=0, last_ts=0, pregame=True,
                   sport=SPORT, market_type=MTYPE, root="sports", source="tape")
    entry = Decimal("0.40")
    fee = leg_fee_cost(cfg, "polymarket", "sports", entry)
    assert fee == cfg["polymarket"]["sports_fee_rate"] * entry * (1 - entry)

    # a win: realized_edge is the per-SHARE edge; the tool's per-DOLLAR ROI is
    # that over the entry price, and the fee is charged the same way.
    gross = realized_edge(pos, Decimal(1)) / entry
    assert float(gross) == pytest.approx(1.5)               # (1 − 0.40) / 0.40
    got = bt.pnl(entry, Decimal(1), "sports", cfg, Decimal(0))
    assert got == pytest.approx(float(gross - fee / entry))
    assert got < float(gross)                                # the fee is real

    # a loss is −1 minus the fee, never worse than −1 by more than the fee
    lost = bt.pnl(entry, Decimal(0), "sports", cfg, Decimal(0))
    assert lost == pytest.approx(float(-1 - fee / entry))

    # slippage moves the ENTRY, so it moves the fee too
    slipped = Decimal("0.41")
    fee2 = leg_fee_cost(cfg, "polymarket", "sports", slipped)
    assert bt.pnl(entry, Decimal(1), "sports", cfg, Decimal("0.01")) == pytest.approx(
        float((Decimal(1) - slipped - fee2) / slipped))

    # the fee category is the position's ROOT, not always sports
    assert bt.pnl(entry, Decimal(1), "crypto", cfg, Decimal(0)) < \
        bt.pnl(entry, Decimal(1), "sports", cfg, Decimal(0))


# ------------------------------------------------------------- consensus K

def test_consensus_needs_a_second_distinct_wallet_within_24h(cfg, con):
    _market(con, "0xc1", T0 + timedelta(days=3))
    _resolution(con, "0xc1", 0, T0 + timedelta(days=4))
    _print(con, "0xA", "0xc1", T0)                                    # first
    _print(con, "0xB", "0xc1", T0 + timedelta(hours=1))               # 2nd, < 24 h
    _print(con, "0xC", "0xc1", T0 + timedelta(hours=40))              # too late
    _print(con, "0xD", "0xc1", T0 + timedelta(minutes=30), idx=1)     # other outcome

    rows, _ = _rows(con, cfg)
    k2 = {(r["wallet"], r["idx"]): r["k2"]["all"] for r in rows}
    assert k2 == {("0xA", 0): False, ("0xB", 0): True,
                  ("0xC", 0): False, ("0xD", 1): False}
    # K=1 follows all four, K=2 only the second wallet
    assert len(bt.select(rows, ("all", 1, False))) == 4
    assert [r["wallet"] for r in bt.select(rows, ("all", 2, False))] == ["0xB"]


def test_consensus_ignores_the_same_wallet_buying_twice(cfg, con):
    _market(con, "0xc1", T0 + timedelta(days=3))
    _resolution(con, "0xc1", 0, T0 + timedelta(days=4))
    _print(con, "0xA", "0xc1", T0)
    _print(con, "0xA", "0xc1", T0 + timedelta(hours=2))               # same wallet
    rows, _ = _rows(con, cfg)
    assert len(rows) == 2 and not any(r["k2"]["all"] for r in rows)


# ------------------------------------------------------------------ exit E

def test_exit_fires_at_the_sell_that_crosses_50_percent(cfg, con):
    _market(con, "0xc1", T0 + timedelta(days=3))
    _resolution(con, "0xc1", 1, T0 + timedelta(days=4))               # the whale LOST
    _print(con, "0xA", "0xc1", T0, price="0.40", size="1000")
    _print(con, "0xA", "0xc1", T0 + timedelta(hours=1), side="SELL",
           price="0.60", size="400")                                  # 40%, not yet
    _print(con, "0xA", "0xc1", T0 + timedelta(hours=2), side="SELL",
           price="0.80", size="400")                                  # 80% cumulative

    rows, _ = _rows(con, cfg)
    assert len(rows) == 1
    row = rows[0]
    assert row["exit"][0] == Decimal("0.80")                           # the CROSSING sell
    slip = Decimal("0.01")
    assert bt.roi(row, cfg, slip, True) == pytest.approx(
        bt.pnl(Decimal("0.40"), Decimal("0.80") - slip, "sports", cfg, slip))
    # holding the same position loses everything
    assert bt.roi(row, cfg, slip, False) == pytest.approx(
        bt.pnl(Decimal("0.40"), Decimal(0), "sports", cfg, slip))


def test_selling_less_than_half_holds_to_resolution(cfg, con):
    _market(con, "0xc1", T0 + timedelta(days=3))
    _resolution(con, "0xc1", 0, T0 + timedelta(days=4))
    _print(con, "0xA", "0xc1", T0, price="0.40", size="1000")
    _print(con, "0xA", "0xc1", T0 + timedelta(hours=1), side="SELL",
           price="0.90", size="300")                                   # 30%, and
    #                                                                  the 0.90 x 300 = $270
    #                                                                  print clears the dust
    #                                                                  floor, so it is a real
    #                                                                  sell that simply is not
    #                                                                  half the position
    rows, _ = _rows(con, cfg)
    assert rows[0]["exit"] is None
    assert bt.roi(rows[0], cfg, Decimal("0.01"), True) == \
        bt.roi(rows[0], cfg, Decimal("0.01"), False)


# --------------------------------------------------------- no look-ahead

def test_a_market_resolved_after_as_of_cannot_move_a_score(cfg, con):
    """0xHIST wins big, resolved 2026-03-05. As of 2026-03-01 its score must
    equal a wallet with no history at all; as of 2026-04-01 it must be above."""
    hist_at = datetime(2026, 3, 5, tzinfo=timezone.utc)
    _market(con, "0xold", hist_at - timedelta(days=1))
    _resolution(con, "0xold", 0, hist_at)
    _print(con, "0xHIST", "0xold", hist_at - timedelta(days=2),
           price="0.10", size="5000")            # $500: above whale.min_print_usd
    # a live candidate for each wallet, entered in March, so both are scored
    _market(con, "0xc1", T0 + timedelta(days=3))
    _resolution(con, "0xc1", 0, T0 + timedelta(days=4))
    _print(con, "0xHIST", "0xc1", T0)
    _print(con, "0xNONE", "0xc1", T0)

    _, wf = _rows(con, cfg)
    before = wf[(2026, 3)]                       # as_of 2026-03-01 < 2026-03-05
    after = wf[(2026, 4)]                        # as_of 2026-04-01 > 2026-03-05
    lb = lambda month, w: bt.score_lb(month, w, SPORT, MTYPE, cfg)  # noqa: E731
    assert lb(before, "0xHIST") == lb(before, "0xNONE")
    assert lb(after, "0xHIST") > lb(after, "0xNONE")
    # and the March candidate was judged by the March score, not April's
    assert before["pop"].get("sports", 0) == 0


def test_the_tool_never_writes(cfg, con, tmp_path):
    """Every read goes through a `mode=ro` URI in `main`; prove the query path
    itself only reads by running it against a connection opened read-only."""
    _market(con, "0xc1", T0 + timedelta(days=3))
    _resolution(con, "0xc1", 0, T0 + timedelta(days=4))
    _print(con, "0xA", "0xc1", T0)
    materialize(con, cfg, T0 + timedelta(days=30), cap=0)
    con.close()
    ro = sqlite3.connect(f"file:{tmp_path / 'tape.sqlite'}?mode=ro", uri=True)
    positions, res, mk = bt.load_tape(ro)
    wf = bt.walk_forward(positions, res, cfg)
    rows = bt.build_rows(positions, res, mk, bt.entry_exit(ro, cfg), wf, cfg)
    assert len(rows) == 1 and rows[0]["price"] == Decimal("0.40")
