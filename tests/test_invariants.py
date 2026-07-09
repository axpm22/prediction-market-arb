"""H6 — invariant 4's escape hatch. A reappearance-storm annotation (now
actually written by the tracker) must silence the invariant for a fingerprint
carrying more than max_reappearances rows in the 24h window."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from arb.config import load
from arb.invariants import check
from arb.store import Store

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def cfg():
    return load(ROOT / "config.yaml")


def _storm_rows(store, fp, n, when):
    for _ in range(n):
        store.insert("flat_bets", {
            "created_at": when.isoformat(), "fingerprint": fp, "kind": "EV",
            "legs_json": "[]", "stake_usd": "1", "status": "OPEN", "event_key": "e"})


def test_invariant4_fires_on_fresh_storm_then_hatch_silences(cfg, tmp_path):
    store = Store(tmp_path / "inv.sqlite")
    now = datetime.now(timezone.utc)
    fp = "stormfp"
    limit = int(cfg["tracker"]["max_reappearances"])
    # > limit rows created 1h ago -> inside the 24h window (proves fresh rows count)
    _storm_rows(store, fp, limit + 1, now - timedelta(hours=1))
    viol = check(cfg, store)
    assert any("invariant-4" in m and fp in m for m in viol)
    # the reappearance-storm annotation the tracker writes silences it
    store.insert("annotations", {
        "created_at": now.isoformat(), "target_kind": "fingerprint",
        "target_id": fp, "kind": "reappearance-storm", "detail": f"{fp}: storm"})
    assert not any("invariant-4" in m and fp in m for m in check(cfg, store))
