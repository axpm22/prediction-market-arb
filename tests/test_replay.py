"""Offline policy replay (PROMPT_REPLAY): re-run detect() over stored quotes.
Read-only by construction — no test here opens the live DB."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from arb.config import load
from arb.cycle import _quote_row
from arb.replay import open_ro, quote_from_row, replay
from arb.store import Store

ROOT = Path(__file__).resolve().parent.parent
CFG = load(ROOT / "config.yaml")
NOW = datetime(2026, 7, 25, 18, 0, tzinfo=timezone.utc)
START = NOW + timedelta(hours=2)
EK = "baseball_mlb:2026-07-25:mets:royals"

DETECT_FIELDS = ("event_key", "venue", "book", "market_type", "line", "outcome_label",
                 "side", "raw_price", "cost", "fee_cost", "depth_usd", "ladder_json",
                 "live", "fetched_at", "category", "sport", "bid", "volume_24h",
                 "start_utc", "start_grade", "venue_market_id")


def _q(**kw):
    base = dict(event_key=EK, venue="kalshi", book="kalshi", market_type="WINNER",
                line=None, outcome_label="mets", side="YES",
                raw_price=Decimal("0.47"), cost=Decimal("0.47"),
                fee_cost=Decimal("0.01"), depth_usd=Decimal("100"),
                ladder_json=None, live=False, fetched_at=NOW, category="sports",
                sport="baseball_mlb", bid=Decimal("0.45"),
                volume_24h=Decimal("1000"), home="royals", away="mets",
                start_utc=START, start_grade="verified", venue_market_id="MKT-1")
    base.update(kw)
    from arb.models import Quote
    return Quote(**base)


ROUND_TRIP_CASES = [
    _q(),                                                   # plain YES WINNER
    _q(side="NO", outcome_label="royals"),                  # NO side
    _q(line=None, market_type="WINNER", start_grade="absent", start_utc=None),
    _q(market_type="TOTAL", line="8.5", outcome_label="OVER"),
    _q(depth_usd=None, bid=None, volume_24h=None, ladder_json='[["0.47","100"]]'),
    _q(live=True, venue="polymarket", book="polymarket"),
]


def test_quote_from_row_is_the_inverse_of_quote_row():
    for q in ROUND_TRIP_CASES:
        back = quote_from_row(_quote_row(q, poll_id=7))
        for f in DETECT_FIELDS:
            assert getattr(back, f) == getattr(q, f), f
        assert isinstance(back.cost, Decimal) and isinstance(back.fee_cost, Decimal)
        assert isinstance(back.raw_price, Decimal)
        assert back.depth_usd is None or isinstance(back.depth_usd, Decimal)
        assert back.effective_cost == q.effective_cost


def test_quote_from_row_parses_home_away_from_event_key():
    # quotes never store venue-raw home/away (_quote_row, arb/cycle.py:22)
    back = quote_from_row(_quote_row(_q(), poll_id=1))
    assert (back.away, back.home) == ("mets", "royals")
    dh = quote_from_row(_quote_row(_q(event_key=EK + ":dh2"), poll_id=1))
    assert (dh.away, dh.home) == ("mets", "royals")


def test_quote_from_row_leaves_venue_side_empty():
    # reconcile-only field, absent from the quotes table and unread by detect()
    assert quote_from_row(_quote_row(_q(), poll_id=1)).venue_side == ""


# ---------------------------------------------------------------- R2: the loop

# 2026-07-25 12:00Z = 08:00 America/New_York; the day boundary is NY (§8.2 keys)
P1 = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)   # pregame: start is later
P2 = datetime(2026, 7, 25, 23, 0, tzinfo=timezone.utc)   # after start: in-play
GAME = datetime(2026, 7, 25, 20, 0, tzinfo=timezone.utc)


def _seed(path):
    """A watch poll that yields one same-venue ARB, a watch poll whose event has
    already started (in-play reject), and a live poll that `--kind watch` skips."""
    store = Store(path)
    for started, kind in ((P1, "watch"), (P2, "watch"), (P1, "live")):
        poll_id = store.insert("polls", {"started_at": started.isoformat(),
                                         "kind": kind, "n_quotes": 2, "n_opps": 0})
        legs = [_q(outcome_label="mets", cost=Decimal("0.47"), raw_price=Decimal("0.47"),
                   fee_cost=Decimal("0"), bid=Decimal("0.46"), fetched_at=started,
                   start_utc=GAME),
                _q(outcome_label="royals", cost=Decimal("0.51"), raw_price=Decimal("0.51"),
                   fee_cost=Decimal("0"), bid=Decimal("0.50"), fetched_at=started,
                   start_utc=GAME)]
        store.insert_many("quotes", [_quote_row(q, poll_id) for q in legs])
    store.close()
    return path


def test_replay_rebuilds_a_known_arb_and_a_known_reject(tmp_path):
    con = open_ro(_seed(tmp_path / "t.sqlite"))
    s = replay(CFG, con, "2026-07-25", kind="watch")
    assert s.polls == 2 and s.quotes == 4          # the live poll is not in scope
    assert s.opps[("ARB", "OK")] == 1              # 1 - (0.47 + 0.51)
    assert s.edge_total == Decimal("0.02")
    assert s.rejects["in-play"] == 1               # the 23:00Z poll, game at 20:00Z
    assert s.days == {"2026-07-25": 2}


def test_replay_kind_selects_the_mode(tmp_path):
    con = open_ro(_seed(tmp_path / "t.sqlite"))
    s = replay(CFG, con, "2026-07-25", kind="live")
    assert s.polls == 1
    # live mode inverts the guard: a pregame event in a live cycle is the reject
    assert s.rejects["pregame-in-live-cycle"] == 1 and not s.opps


def test_replay_day_filter_is_a_new_york_day(tmp_path):
    con = open_ro(_seed(tmp_path / "t.sqlite"))
    assert replay(CFG, con, "2026-07-24", kind="watch").polls == 0
    assert replay(CFG, con, "2026-07-24", "2026-07-26", kind="watch").polls == 2


def test_replay_writes_nothing(tmp_path):
    path = _seed(tmp_path / "t.sqlite")
    before = path.read_bytes()
    replay(CFG, open_ro(path), "2026-07-25", kind="watch")
    assert path.read_bytes() == before


def test_cmd_replay_runs_read_only(tmp_path, capsys):
    from types import SimpleNamespace

    from arb.commands import cmd_replay
    path = _seed(tmp_path / "t.sqlite")
    args = SimpleNamespace(from_date="2026-07-25", to_date=None, kind="watch")
    assert cmd_replay(CFG, Store(path), args) == 0
    out = capsys.readouterr().out
    assert "read-only" in out and "negRisk" in out   # scope stated in the header
