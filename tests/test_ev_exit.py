from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from arb.config import load_config
from arb.experimental.ev_exit import run_ev_exit_once
from arb.models import Event, Opportunity, Quote
from arb.settlement import settle_manual
from arb.store import Store
from arb.tracker import record_tracker_poll


def _ev_opp(now: datetime, *, stake: str = "1.00", quality_flag: str | None = None) -> Opportunity:
    start = now + timedelta(days=1)
    event_key = f"baseball_mlb:{start.date().isoformat()}:away:home"
    quote = Quote(
        event_key=event_key,
        venue="polymarket",
        book="token-yes",
        market_type="WINNER",
        line=None,
        outcome_label="home",
        raw_price=Decimal("0.50"),
        cost=Decimal("0.50"),
        fee_cost=Decimal("0.00"),
        depth_usd=Decimal("100"),
        live=False,
        fetched_at=now,
        start_utc=start,
    )
    event = Event(
        event_key=event_key,
        category="sports",
        sport="baseball_mlb",
        home="home",
        away="away",
        start_utc=start,
    )
    return Opportunity(
        kind="EV",
        category="sports",
        event=event,
        legs=[quote],
        edge=Decimal("0.10"),
        stakes={"home": Decimal(stake)},
        status="OK",
        quality_flag=quality_flag,
    )


def _place(store: Store, opp: Opportunity) -> None:
    poll = store.start_poll("watch")
    record_tracker_poll(store, poll, [opp])


def _cfg() -> dict[str, object]:
    return {
        "enabled": True,
        "max_bet_age_hours": 72,
        "include_micro": True,
        "min_exit_depth_usd": "5",
        "min_profit_usd": "0.05",
        "min_profit_roi": "0.03",
        "exit_edge_ceiling": "0.01",
        "require_fair_value": True,
    }


def test_ev_exit_sells_when_profit_is_fillable_and_edge_is_gone(tmp_path) -> None:
    now = datetime.now(timezone.utc)
    config = replace(load_config(), database_path=tmp_path / "arb.sqlite", consensus_max_age_hours=8)
    store = Store(config.database_path)
    opp = _ev_opp(now)
    _place(store, opp)
    store.save_consensus({(opp.event.event_key, "home"): Decimal("0.565")}, now)

    exited = run_ev_exit_once(
        store,
        config,
        _cfg(),
        fetchers={"polymarket": lambda _leg: (Decimal("0.56"), Decimal("100"))},
        now=now,
    )

    assert exited == 1
    with store.connect() as con:
        rows = list(con.execute("SELECT * FROM flat_bets ORDER BY id"))
    assert [row["status"] for row in rows] == ["OPEN", "EXITED"]
    assert rows[1]["result"] == "SOLD"
    assert rows[1]["pnl_optimistic"] == "0.12"
    assert "remaining_edge=0.005000" in rows[1]["notes"]


def test_ev_exit_refuses_to_sell_when_model_still_has_edge(tmp_path) -> None:
    now = datetime.now(timezone.utc)
    config = replace(load_config(), database_path=tmp_path / "arb.sqlite", consensus_max_age_hours=8)
    store = Store(config.database_path)
    opp = _ev_opp(now)
    _place(store, opp)
    store.save_consensus({(opp.event.event_key, "home"): Decimal("0.650")}, now)

    exited = run_ev_exit_once(
        store,
        config,
        _cfg(),
        fetchers={"polymarket": lambda _leg: (Decimal("0.56"), Decimal("100"))},
        now=now,
    )

    assert exited == 0
    with store.connect() as con:
        statuses = [row["status"] for row in con.execute("SELECT status FROM flat_bets ORDER BY id")]
    assert statuses == ["OPEN"]


def test_ev_exit_closes_bet_for_settlement_and_allows_reentry(tmp_path) -> None:
    now = datetime.now(timezone.utc)
    config = replace(load_config(), database_path=tmp_path / "arb.sqlite", consensus_max_age_hours=8)
    store = Store(config.database_path)
    opp = _ev_opp(now)
    _place(store, opp)
    store.save_consensus({(opp.event.event_key, "home"): Decimal("0.565")}, now)

    assert run_ev_exit_once(
        store,
        config,
        _cfg(),
        fetchers={"polymarket": lambda _leg: (Decimal("0.56"), Decimal("100"))},
        now=now,
    ) == 1
    assert settle_manual(store, opp.event.event_key, "home") == 0

    _place(store, opp)
    with store.connect() as con:
        rows = list(con.execute("SELECT id, status, reappearance_of FROM flat_bets ORDER BY id"))
    assert [row["status"] for row in rows] == ["OPEN", "EXITED", "OPEN"]
    assert rows[2]["reappearance_of"] == rows[1]["id"]


def test_ev_exit_requires_enough_exit_depth(tmp_path) -> None:
    now = datetime.now(timezone.utc)
    config = replace(load_config(), database_path=tmp_path / "arb.sqlite", consensus_max_age_hours=8)
    store = Store(config.database_path)
    opp = _ev_opp(now, stake="10.00", quality_flag="micro")
    _place(store, opp)
    store.save_consensus({(opp.event.event_key, "home"): Decimal("0.565")}, now)

    exited = run_ev_exit_once(
        store,
        config,
        _cfg(),
        fetchers={"polymarket": lambda _leg: (Decimal("0.56"), Decimal("3"))},
        now=now,
    )

    assert exited == 0
