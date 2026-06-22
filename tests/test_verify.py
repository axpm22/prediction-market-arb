from datetime import datetime, timedelta, timezone
from decimal import Decimal

from arb.config import load_config
from arb.models import Event, Opportunity, Quote
from arb.store import Store
from arb.tracker import record_tracker_poll
from arb.experimental.verify import run_verify_once, unverified_arb_bets

EVENT_KEY = "baseball_mlb:2026-06-12:kansas city royals:texas rangers"
VCFG = {"poll_secs": 10, "max_bet_age_secs": 900}


def leg(venue: str, outcome: str, cost: str) -> Quote:
    price = Decimal(cost)
    return Quote(
        event_key=EVENT_KEY, venue=venue, book=f"{venue}-{outcome}", market_type="WINNER",
        line=None, outcome_label=outcome, raw_price=price, cost=price, fee_cost=Decimal("0"),
        depth_usd=Decimal("500"), live=False, fetched_at=datetime.now(timezone.utc),
        start_utc=datetime.now(timezone.utc) + timedelta(hours=4),
    )


def arb_bet(store: Store) -> Opportunity:
    legs = [leg("kalshi", "kansas city royals", "0.48"), leg("polymarket", "texas rangers", "0.48")]
    event = Event(event_key=EVENT_KEY, category="sports", sport="baseball_mlb",
                  home="kansas city royals", away="texas rangers",
                  start_utc=datetime.now(timezone.utc) + timedelta(hours=4))
    opp = Opportunity(kind="ARB", category="sports", event=event, legs=legs,
                      edge=Decimal("0.041667"), stakes={}, status="REVIEW")
    record_tracker_poll(store, store.start_poll("watch"), [opp])
    return opp


def settle_statuses(store: Store) -> list[tuple[str, str, str]]:
    with store.connect() as con:
        return [(r["status"], r["edge"], r["notes"]) for r in con.execute(
            "SELECT * FROM flat_bets WHERE notes LIKE 'flash-verify%' ORDER BY id")]


def test_verified_when_requote_still_sums_below_one(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    arb_bet(store)
    fetchers = {"kalshi": lambda l: Decimal("0.49"), "polymarket": lambda l: Decimal("0.49")}
    assert run_verify_once(store, load_config(), VCFG, fetchers) == 1
    [(status, edge, note)] = settle_statuses(store)
    assert status == "VERIFIED"
    assert Decimal(edge) == Decimal("0.020408")  # 1/0.98 - 1 at the re-quoted prices
    # Sequential fill: hold one leg at its detection price (0.48), re-quote the other
    # (0.49) → 1/0.97 - 1, under both orderings (symmetric here). Worst is headlined.
    assert "seq_worst=0.030928" in note
    assert "seq_best=0.030928" in note
    assert "lag=" in note


def test_vanished_when_requote_sums_above_one_or_book_gone(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    arb_bet(store)
    fetchers = {"kalshi": lambda l: Decimal("0.60"), "polymarket": lambda l: Decimal("0.55")}
    run_verify_once(store, load_config(), VCFG, fetchers)
    [(status, edge, _)] = settle_statuses(store)
    assert status == "VANISHED"
    assert Decimal(edge) < 0  # the recomputed edge shows the damage

    store2 = Store(tmp_path / "arb2.sqlite")
    arb_bet(store2)
    fetchers = {"kalshi": lambda l: None, "polymarket": lambda l: Decimal("0.49")}
    run_verify_once(store2, load_config(), VCFG, fetchers)
    assert settle_statuses(store2)[0][0] == "VANISHED"


def test_each_bet_verified_once_and_old_bets_skipped(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    arb_bet(store)
    fetchers = {"kalshi": lambda l: Decimal("0.49"), "polymarket": lambda l: Decimal("0.49")}
    config = load_config()
    assert run_verify_once(store, config, VCFG, fetchers) == 1
    assert run_verify_once(store, config, VCFG, fetchers) == 0  # already has a verdict row
    future = datetime.now(timezone.utc) + timedelta(seconds=2000)
    assert unverified_arb_bets(store, future, VCFG["max_bet_age_secs"]) == []  # too old to bother


def test_flash_verdict_does_not_reopen_same_active_fingerprint(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    opp = arb_bet(store)
    fetchers = {"kalshi": lambda l: Decimal("0.49"), "polymarket": lambda l: Decimal("0.49")}
    run_verify_once(store, load_config(), VCFG, fetchers)

    record_tracker_poll(store, store.start_poll("watch"), [opp])

    with store.connect() as con:
        statuses = [r["status"] for r in con.execute("SELECT status FROM flat_bets ORDER BY id")]
    assert statuses == ["OPEN", "VERIFIED", "CONFIRMED"]


def test_sx_legs_are_marked_unverifiable(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    arb_bet(store)
    fetchers = {"kalshi": lambda l: Decimal("0.49")}  # no polymarket fetcher → unverifiable
    run_verify_once(store, load_config(), VCFG, fetchers)
    assert settle_statuses(store)[0][0] == "UNVERIFIABLE"
