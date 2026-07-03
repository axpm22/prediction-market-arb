"""08 follow-up — consensus_age_secs + leg_skew_secs as real columns (tracked live for the week)."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from arb.store import Store
from arb.tracker import _consensus_age_from_notes, _leg_skew_secs, record_tracker_poll
from arb.validation import ValidationRule

from tests.test_tracker import NOW, arb_opp, ev_opp


def test_consensus_age_from_notes() -> None:
    assert _consensus_age_from_notes(("stale-consensus", "consensus_age_secs:420")) == 420
    assert _consensus_age_from_notes(("capturable:0.01",)) is None
    assert _consensus_age_from_notes(()) is None


def test_leg_skew_secs() -> None:
    t = datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc)
    from tests.test_tracker import quote
    a = replace(quote("a", "0.50"), fetched_at=t)
    b = replace(quote("b", "0.50"), fetched_at=t + timedelta(seconds=45))
    assert _leg_skew_secs([a, b]) == 45.0
    assert _leg_skew_secs([a]) is None  # single leg → no skew


def _place(store: Store, opp) -> None:
    poll = store.start_poll("watch")
    record_tracker_poll(store, poll, [opp],
                        validation_rule=ValidationRule(min_cost=Decimal("0"), max_cost=Decimal("1"),
                                                       max_flagged_edge=Decimal("1")),
                        now=NOW)


def test_columns_persisted_on_bet(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    # EV bet carrying a consensus_age note → column stamped
    ev = ev_opp("0.50")
    ev = replace(ev, notes=("consensus_age_secs:300",))
    _place(store, ev)
    # two-leg arb with 30s fetch skew → leg_skew_secs stored
    from tests.test_tracker import quote
    arb = arb_opp("0.55", "0.40")
    base = datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc)
    arb = replace(arb, legs=[replace(arb.legs[0], fetched_at=base),
                             replace(arb.legs[1], fetched_at=base + timedelta(seconds=30))])
    _place(store, arb)

    with store.connect() as con:
        rows = {r["kind"]: r for r in con.execute(
            "SELECT kind, consensus_age_secs, leg_skew_secs FROM flat_bets WHERE status='OPEN'")}
    assert rows["EV"]["consensus_age_secs"] == 300
    assert rows["EV"]["leg_skew_secs"] is None          # single leg
    assert rows["ARB"]["leg_skew_secs"] == 30.0
    assert rows["ARB"]["consensus_age_secs"] is None     # arb carries no consensus age
