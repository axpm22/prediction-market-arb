"""PARLAY bet class (SPEC §11): detection, own-ledger recording, AND-settlement, pregame-only.

A parlay pays $1 iff every leg wins — no static replication, so it can never be a risk-free
lock. Every signal is REVIEW, never auto-filled, recorded in its own ledger lane (kind=PARLAY),
and settled as the conjunction of its component legs.
"""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from arb.matching import event_key, load_aliases
from arb.models import Event, Opportunity, Quote
from arb.parlay import ParlayLeg, ParlayMarket, detect_parlays, load_parlay_markets
from arb.settlement import settle_parlays
from arb.store import Store
from arb.tracker import record_tracker_poll
from arb.validation import ValidationRule

FIXTURES = Path("tests/fixtures")
ALIASES = load_aliases()
NOW = datetime(2026, 6, 25, tzinfo=timezone.utc)
THRESHOLD = Decimal("0.05")
PERMISSIVE = ValidationRule(min_cost=Decimal("0"), max_cost=Decimal("1"), max_flagged_edge=Decimal("1"))


def _mkt(market_id, ask, legs, *, same_game=False, bid=None, fee_rate="0", liquidity="1000") -> ParlayMarket:
    return ParlayMarket(
        venue="kalshi", market_id=market_id, ask=Decimal(ask), legs=legs, same_game=same_game,
        bid=None if bid is None else Decimal(bid), fee_rate=Decimal(fee_rate),
        depth_usd=Decimal("300"), liquidity_usd=Decimal(liquidity),
    )


def _leg(home, away, outcome, prob, start, sport="baseball_mlb", market_type="WINNER", line=None) -> ParlayLeg:
    return ParlayLeg(
        event_key=event_key(sport, home, away, start, ALIASES), sport=sport, market_type=market_type,
        outcome_label=outcome, prob=Decimal(prob),
        line=None if line is None else Decimal(line), start_utc=start, home=home, away=away,
    )


# ---- detection (SPEC §11) -------------------------------------------------------------

def test_detect_signal_classes_and_same_game_control() -> None:
    future = datetime(2026, 7, 1, 23, 5, tzinfo=timezone.utc)
    legs = [_leg("a", "b", "a", "0.60", future), _leg("c", "d", "c", "0.55", future)]
    # ask 0.12 < lower Fréchet bound 0.15 -> underpriced under any correlation.
    under = detect_parlays([_mkt("M1", "0.12", legs)], THRESHOLD, Decimal("500"), now=NOW)
    assert len(under) == 1 and under[0].kind == "PARLAY" and under[0].status == "REVIEW"
    assert any("parlay-frechet-underpriced" in n for n in under[0].notes)
    assert under[0].edge_points == Decimal("0.030000")  # 0.15 - 0.12

    # ask 0.25 <= fair 0.33 - 0.05 -> independence-cheap, but ONLY cross-game.
    cheap = detect_parlays([_mkt("M2", "0.25", legs)], THRESHOLD, Decimal("500"), now=NOW)
    assert any("parlay-independence-cheap" in n for n in cheap[0].notes)
    same = detect_parlays([_mkt("M3", "0.25", legs, same_game=True)], THRESHOLD, Decimal("500"), now=NOW)
    assert same == []  # same-game: independence product is not a bound, so no signal


def test_fee_erodes_edge_below_signal() -> None:
    future = datetime(2026, 7, 1, 23, 5, tzinfo=timezone.utc)
    legs = [_leg("a", "b", "a", "0.60", future), _leg("c", "d", "c", "0.55", future)]
    # ask 0.14 alone is under 0.15; a 0.05 winning-bet fee pushes eff cost to 0.143 -> still
    # under here, but a higher fee crosses it. Verify fee is actually charged into eff_ask.
    no_fee = detect_parlays([_mkt("F1", "0.14", legs, fee_rate="0")], THRESHOLD, Decimal("0"), now=NOW)
    big_fee = detect_parlays([_mkt("F2", "0.14", legs, fee_rate="0.20")], THRESHOLD, Decimal("0"), now=NOW)
    assert no_fee and not big_fee  # 0.14 + 0.20*(1-0.14)=0.312 > 0.15 -> no signal


def test_thin_book_is_flagged() -> None:
    future = datetime(2026, 7, 1, 23, 5, tzinfo=timezone.utc)
    legs = [_leg("a", "b", "a", "0.60", future), _leg("c", "d", "c", "0.55", future)]
    out = detect_parlays([_mkt("T1", "0.12", legs, liquidity="100")], THRESHOLD, Decimal("500"), now=NOW)
    assert out and out[0].quality_flag == "thin-parlay-book"


def test_fixture_loader_roundtrip() -> None:
    markets = load_parlay_markets(json.loads((FIXTURES / "parlays" / "markets.json").read_text()))
    opps = detect_parlays(markets, THRESHOLD, Decimal("500"), now=NOW)
    classes = {n for o in opps for n in o.notes if n.startswith("parlay-")}
    assert "parlay-frechet-underpriced" in classes and "parlay-independence-cheap" in classes
    assert len(opps) == 2  # the same-game market is correctly silent


# ---- own ledger lane ------------------------------------------------------------------

def _win_parlay() -> ParlayMarket:
    dodgers = _leg("Pittsburgh Pirates", "Los Angeles Dodgers", "los angeles dodgers", "0.60",
                   datetime(2026, 6, 9, 22, 41, tzinfo=timezone.utc))
    royals = _leg("Kansas City Royals", "Texas Rangers", "kansas city royals", "0.55",
                  datetime(2026, 6, 9, 23, 41, tzinfo=timezone.utc))
    return _mkt("WIN-PARLAY", "0.12", [dodgers, royals])


def _place(store: Store, markets, *, now) -> None:
    opps = detect_parlays(markets, THRESHOLD, Decimal("500"), now=now)
    poll = store.start_poll("parlay")
    record_tracker_poll(store, poll, opps, PERMISSIVE, now=now, only_kinds=("PARLAY",))


def test_parlay_records_as_own_class(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    _place(store, [_win_parlay()], now=datetime(2026, 6, 9, 21, 0, tzinfo=timezone.utc))
    with store.connect() as con:
        rows = list(con.execute("SELECT * FROM flat_bets WHERE kind='PARLAY' AND status='OPEN'"))
    assert len(rows) == 1
    row = rows[0]
    assert row["trusted"] == 0                      # REVIEW, never in the headline
    assert Decimal(row["stake_usd"]) == Decimal("1.00")
    assert row["pnl_optimistic"] is None            # settles later, like EV
    assert Decimal(row["optimistic_cost"]) == Decimal("0.12")


def test_parlay_poll_does_not_close_other_kinds(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    # Seed an active ARB fingerprint via a raw OPEN row.
    with store.connect() as con:
        con.execute(
            "INSERT INTO flat_bets(fingerprint, opened_poll_id, category, sport, kind, trusted, live, "
            "edge, stake_usd, legs_json, status, placed_at, notes) "
            "VALUES('arb-fp', 1, 'sports', 'baseball_mlb', 'ARB', 1, 0, '0.03', '1.00', '[]', 'OPEN', ?, 'seed')",
            (NOW.isoformat(),),
        )
    _place(store, [_win_parlay()], now=datetime(2026, 6, 9, 21, 0, tzinfo=timezone.utc))
    with store.connect() as con:
        arb = con.execute("SELECT status FROM flat_bets WHERE fingerprint='arb-fp' ORDER BY id DESC LIMIT 1").fetchone()
    assert arb["status"] == "OPEN"  # the PARLAY-scoped poll left the ARB untouched


def test_parlay_pregame_only(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    # Record AFTER a leg has started -> not actionable.
    _place(store, [_win_parlay()], now=datetime(2026, 6, 9, 23, 0, tzinfo=timezone.utc))
    with store.connect() as con:
        open_rows = list(con.execute("SELECT * FROM flat_bets WHERE kind='PARLAY' AND status='OPEN'"))
        skipped = list(con.execute("SELECT * FROM flat_bets WHERE quality_flag='in-play-skipped'"))
    assert not open_rows and skipped


# ---- AND settlement -------------------------------------------------------------------

def _ns_config():
    return SimpleNamespace(settle_days_from=3)


def test_parlay_settles_won_when_all_legs_win(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    _place(store, [_win_parlay()], now=datetime(2026, 6, 9, 21, 0, tzinfo=timezone.utc))
    n = settle_parlays(store, _ns_config(), FIXTURES)
    assert n == 1
    with store.connect() as con:
        row = con.execute("SELECT * FROM flat_bets WHERE kind='PARLAY' AND status='SETTLED'").fetchone()
    assert row["result"] == "WON"
    # $1 at ask 0.12, both legs won -> profit (1-0.12)/0.12.
    assert Decimal(row["pnl_optimistic"]) == Decimal("7.33")


def test_parlay_settles_lost_on_one_losing_leg(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    # Mets LOST (0-7); Royals won. One loss -> the whole parlay loses the $1 stake.
    mets = _leg("New York Mets", "St. Louis Cardinals", "new york mets", "0.55",
                datetime(2026, 6, 9, 23, 11, tzinfo=timezone.utc))
    royals = _leg("Kansas City Royals", "Texas Rangers", "kansas city royals", "0.60",
                  datetime(2026, 6, 9, 23, 41, tzinfo=timezone.utc))
    _place(store, [_mkt("LOSE-PARLAY", "0.10", [mets, royals])],
           now=datetime(2026, 6, 9, 21, 0, tzinfo=timezone.utc))
    settle_parlays(store, _ns_config(), FIXTURES)
    with store.connect() as con:
        row = con.execute("SELECT * FROM flat_bets WHERE kind='PARLAY' AND status='SETTLED'").fetchone()
    assert row["result"] == "LOST"
    assert Decimal(row["pnl_optimistic"]) == Decimal("-1.00")
