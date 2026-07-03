"""02 — instant-settle tripwire + `arb check-invariants`.

The prompt-01 guards (in-play refusal at placement, future-start settle guard) make a normal
pregame bet unable to instant-settle. The tripwire is INSURANCE for when a guard fails — so
these tests simulate that failure state (an absent leg start = the clock-skew/stale-book case the
tripwire backstops) rather than a normally-placed bet.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from arb.config import load_config
from arb.experimental.annotations import annotated_bet_ids
from arb.invariants import check_invariants
from arb.settlement import settle_sports
from arb.store import Store
from arb.tracker import record_tracker_poll
from arb.validation import ValidationRule

from tests.test_settlement import ev_opp, place_bets, settled_rows, strip_leg_start_times

FIXTURES = Path("tests/fixtures")
_RULE = ValidationRule(min_cost=Decimal("0"), max_cost=Decimal("1"), max_flagged_edge=Decimal("1"))


def _place_and_instant_settle(store: Store, opps) -> int:
    """Place pregame (start ~1h out so placement passes), then drop the leg start so the settle
    guards no longer see a start — the bet then settles the same instant it was placed."""
    place_bets(store, opps)          # placed_at = start - 1h ≈ now
    strip_leg_start_times(store)     # simulate the lost/absent start
    return settle_sports(store, load_config(), FIXTURES)


def _tags(store: Store, bet_id: int) -> list[str]:
    with store.connect() as con:
        return [r["tag"] for r in con.execute("SELECT tag FROM annotations WHERE bet_id=?", (bet_id,))]


def test_instant_settle_is_tripwired_excluded_and_blacklisted(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    soon = datetime.now(timezone.utc) + timedelta(hours=1)
    opp = ev_opp("kansas city royals", "0.50", start=soon)
    assert _place_and_instant_settle(store, [opp]) == 1

    settled = settled_rows(store)[0]
    # (1) annotated settle-tripwire, (2) therefore excluded from every clean P&L cell.
    assert "settle-tripwire" in _tags(store, int(settled["id"]))
    assert int(settled["id"]) in annotated_bet_ids(store)

    # (3) fingerprint blacklisted: a later (pregame) re-sighting is rejected, not reopened.
    open_before = _count(store, "status='OPEN'")
    poll = store.start_poll("watch")
    resight = ev_opp("kansas city royals", "0.50", start=datetime.now(timezone.utc) + timedelta(hours=2))
    record_tracker_poll(store, poll, [resight], validation_rule=_RULE,
                        now=datetime.now(timezone.utc))
    assert resight.fingerprint == opp.fingerprint
    assert _count(store, "status='OPEN'") == open_before  # no new OPEN row
    with store.connect() as con:
        last = con.execute("SELECT quality_flag FROM flat_bets WHERE fingerprint=? ORDER BY id DESC LIMIT 1",
                           (resight.fingerprint,)).fetchone()
    assert last["quality_flag"] == "terminal-fingerprint-skipped"


def test_slow_settle_is_untouched(tmp_path) -> None:
    # Normal path: placed ~an hour pregame, game finished weeks ago → settled_at - placed_at is huge.
    store = Store(tmp_path / "arb.sqlite")
    place_bets(store, [ev_opp("kansas city royals", "0.50")])
    assert settle_sports(store, load_config(), FIXTURES) == 1
    settled = settled_rows(store)[0]
    assert "settle-tripwire" not in _tags(store, int(settled["id"]))
    assert int(settled["id"]) not in annotated_bet_ids(store)


def test_winner_and_loser_loops_treated_identically(tmp_path) -> None:
    # Integrity, not P&L sign: a winning instant-settle and a losing one are both tripwired.
    store = Store(tmp_path / "arb.sqlite")
    soon = datetime.now(timezone.utc) + timedelta(hours=1)
    assert _place_and_instant_settle(store, [
        ev_opp("kansas city royals", "0.50", start=soon),  # KC won
        ev_opp("texas rangers", "0.40", start=soon),        # TEX lost
    ]) == 2
    by_result = {r["result"]: int(r["id"]) for r in settled_rows(store)}
    assert set(by_result) == {"WON", "LOST"}
    assert "settle-tripwire" in _tags(store, by_result["WON"])
    assert "settle-tripwire" in _tags(store, by_result["LOST"])


# --------------------------------------------------------------------------------------------
# check-invariants
# --------------------------------------------------------------------------------------------
def _count(store: Store, where: str) -> int:
    with store.connect() as con:
        return int(con.execute(f"SELECT COUNT(*) FROM flat_bets WHERE {where}").fetchone()[0])


def _insert(con, poll_id, *, fingerprint, status="SETTLED", placed_at, settled_at=None,
            result="WON", pnl="0.500000", legs_json='[{"event_key":"x"}]', kind="EV"):
    con.execute(
        """INSERT INTO flat_bets(fingerprint, opened_poll_id, category, sport, kind, trusted, live,
               edge, stake_usd, legs_json, status, placed_at, settled_at, result, pnl_optimistic,
               notes, ai_matched, settlement_mismatch)
           VALUES (?, ?, 'sports', 'baseball_mlb', ?, 0, 0, '0.03', '1.00', ?, ?, ?, ?, ?, ?, '', 0, 0)""",
        (fingerprint, poll_id, kind, legs_json, status, placed_at, settled_at, result, pnl),
    )


def test_check_invariants_reports_1_2_3_on_dirty_ledger(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    poll = store.start_poll("watch")
    with store.connect() as con:
        # fp1: two settled rows, both settled within a poll interval → checks 1 (duplicate) + 3 (instant).
        _insert(con, poll, fingerprint="fp1", placed_at="2026-06-09T10:00:00+00:00",
                settled_at="2026-06-09T10:00:05+00:00")
        _insert(con, poll, fingerprint="fp1", placed_at="2026-06-09T10:00:00+00:00",
                settled_at="2026-06-09T10:00:20+00:00")
        # fp2: placed after its leg start (settled hours later) → check 2 (post-start) only.
        _insert(con, poll, fingerprint="fp2", placed_at="2026-06-09T10:00:00+00:00",
                settled_at="2026-06-09T14:00:00+00:00",
                legs_json='[{"start_utc":"2026-06-09T09:00:00+00:00"}]')

    violations = check_invariants(store, poll_interval_secs=60, max_reappearances=10)
    kinds = {v.split("]")[0] + "]" for v in violations}
    assert "[1]" in kinds  # duplicate-settlement
    assert "[2]" in kinds  # post-start
    assert "[3]" in kinds  # instant-settle


def test_check_invariants_silent_on_clean_ledger(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    poll = store.start_poll("watch")
    with store.connect() as con:
        _insert(con, poll, fingerprint="clean", placed_at="2026-06-09T10:00:00+00:00",
                settled_at="2026-06-09T16:00:00+00:00",  # 6h after placement
                legs_json='[{"start_utc":"2026-06-09T12:00:00+00:00"}]')  # placed before start
    assert check_invariants(store, poll_interval_secs=60, max_reappearances=10) == []


def test_check_invariants_reappearance_and_decimal(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    poll = store.start_poll("watch")
    with store.connect() as con:
        # check 4: 3 rows for one fingerprint inside 24h, max_reappearances=2.
        for i in range(3):
            _insert(con, poll, fingerprint="chain", status="OPEN", settled_at=None,
                    placed_at=f"2026-06-09T1{i}:00:00+00:00", legs_json='[{"event_key":"x"}]')
        # check 5: a settled P&L with 7 dp is float drift, not q6-clean.
        _insert(con, poll, fingerprint="drift", placed_at="2026-06-09T10:00:00+00:00",
                settled_at="2026-06-09T16:00:00+00:00", pnl="0.1234567",
                legs_json='[{"start_utc":"2026-06-09T12:00:00+00:00"}]')
    violations = check_invariants(store, poll_interval_secs=60, max_reappearances=2)
    kinds = {v.split("]")[0] + "]" for v in violations}
    assert "[4]" in kinds  # reappearance-chain
    assert "[5]" in kinds  # decimal drift
