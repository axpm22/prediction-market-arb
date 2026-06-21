from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from arb.config import load_config
from arb.models import Event, Opportunity, Quote
from arb.settlement import settle_manual, settle_sports
from arb.store import Store
from arb.tracker import record_tracker_poll

# Completed game in the recorded scores fixture: KC 5 - TEX 3 on 2026-06-09 (total 8).
SETTLED_EVENT = "baseball_mlb:2026-06-09:kansas city royals:texas rangers:3"
START = datetime(2026, 6, 9, 23, 41, tzinfo=timezone.utc)


def ev_opp(
    outcome: str,
    cost: str,
    market_type: str = "WINNER",
    line: str | None = None,
    *,
    quality_flag: str | None = None,
    kelly_stake: str | None = None,
) -> Opportunity:
    quote = Quote(
        event_key=SETTLED_EVENT,
        venue="kalshi",
        book=f"KXMLB-{outcome}-{line}",
        market_type=market_type,  # type: ignore[arg-type]
        line=None if line is None else Decimal(line),
        outcome_label=outcome,
        raw_price=Decimal(cost),
        cost=Decimal(cost),
        fee_cost=Decimal("0.0175"),
        depth_usd=Decimal("1200"),
        live=False,
        fetched_at=datetime.now(timezone.utc),
        start_utc=START,
    )
    event = Event(
        event_key=SETTLED_EVENT, category="sports", sport="baseball_mlb",
        home="kansas city royals", away="texas rangers", start_utc=START,
    )
    return Opportunity(
        kind="EV", category="sports", event=event, legs=[quote],
        edge=Decimal("0.03"), stakes={}, status="OK",
        quality_flag=quality_flag,
        kelly_f_star=None if kelly_stake is None else Decimal("0.10"),
        kelly_stake=None if kelly_stake is None else Decimal(kelly_stake),
    )


def place_bets(store: Store, opps) -> None:
    poll = store.start_poll("watch")
    record_tracker_poll(store, poll, opps)


def settled_rows(store: Store):
    with store.connect() as con:
        return list(con.execute("SELECT * FROM flat_bets WHERE status = 'SETTLED' ORDER BY id"))


def test_score_settlement_vectors(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    place_bets(store, [
        ev_opp("kansas city royals", "0.50"),              # effective 0.5175, KC won
        ev_opp("texas rangers", "0.40"),                   # TEX lost
        ev_opp("UNDER", "0.50", "TOTAL", "8.5"),           # total 8 < 8.5, won
        ev_opp("OVER", "0.30", "TOTAL", "8.5"),            # lost
    ])
    config = load_config()
    assert settle_sports(store, config, Path("tests/fixtures")) == 4
    rows = {(r["result"], r["pnl_optimistic"]) for r in settled_rows(store)}
    # SPEC §9 vector: win at effective cost 0.5175 pays (1-0.5175)/0.5175 = $0.93 to the cent.
    assert ("WON", "0.93") in rows
    assert ("LOST", "-1.00") in rows
    won = [r for r in settled_rows(store) if r["result"] == "WON"]
    lost = [r for r in settled_rows(store) if r["result"] == "LOST"]
    assert len(won) == 2 and len(lost) == 2
    # First-observation-only bets have no confirmed fill, so confirmed P&L stays NULL ("missed").
    assert all(r["pnl_confirmed"] is None for r in settled_rows(store))


def test_settlement_is_idempotent_and_append_only(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    place_bets(store, [ev_opp("kansas city royals", "0.50")])
    config = load_config()
    assert settle_sports(store, config, Path("tests/fixtures")) == 1
    assert settle_sports(store, config, Path("tests/fixtures")) == 0  # already settled
    with store.connect() as con:
        statuses = [r["status"] for r in con.execute("SELECT status FROM flat_bets ORDER BY id")]
    assert statuses == ["OPEN", "SETTLED"]


def test_confirmed_fill_settles_from_second_observation_price(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    place_bets(store, [ev_opp("kansas city royals", "0.50")])
    place_bets(store, [ev_opp("kansas city royals", "0.40")])  # second consecutive poll
    config = load_config()
    assert settle_sports(store, config, Path("tests/fixtures")) == 1
    row = settled_rows(store)[0]
    assert row["pnl_optimistic"] == "0.93"   # entry at 0.5175 effective
    assert row["pnl_confirmed"] == "1.40"    # confirmed at 0.4175 effective: (1-c)/c
    assert row["placed_at"] is not None


def test_settlement_records_kelly_pnl_from_base_sizing(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    place_bets(store, [ev_opp("kansas city royals", "0.50", kelly_stake="10.00")])
    place_bets(store, [ev_opp("kansas city royals", "0.40", kelly_stake="20.00")])
    config = load_config()
    assert settle_sports(store, config, Path("tests/fixtures")) == 1
    row = settled_rows(store)[0]
    assert row["pnl_kelly_optimistic"] == "9.32"
    # Confirmed P&L uses the second-observation cost, but the base row's Kelly size.
    assert row["pnl_kelly_confirmed"] == "13.95"
    assert row["kelly_stake_usd"] == "10.00"


def test_settlement_carries_quality_flag_to_scoring_row(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    place_bets(store, [ev_opp("kansas city royals", "0.50", quality_flag="suspect-edge")])
    config = load_config()
    assert settle_sports(store, config, Path("tests/fixtures")) == 1
    row = settled_rows(store)[0]
    assert row["quality_flag"] == "suspect-edge"


def test_manual_settlement(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    place_bets(store, [ev_opp("kansas city royals", "0.50")])
    assert settle_manual(store, SETTLED_EVENT, "kansas city royals") == 1
    assert settled_rows(store)[0]["result"] == "WON"
