from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _fees_never_stale_in_tests(monkeypatch):
    """`confirmed_on` in config.yaml is a real record of when each venue's fee schedule was
    last verified, so it ages against the wall clock: once past `fee_stale_days`, every
    `load_config()` bet picks up quality_flag='stale-fee' and unrelated EV tests fail by
    calendar rather than by logic. Neutralise it globally — the quarantine itself is covered
    by test_stale_fee_venue_is_quarantined, which injects stale_fee_venues explicitly.
    """
    monkeypatch.setattr("arb.config._warn_stale_fees", lambda raw, venues: set())

