"""MODEL_FOUNDATIONS Phase F — loop cadence, graded start reliability, consensus age (fixtures-free)."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from arb.cli import detect_ev_from_consensus
from arb.config import load_config
from arb.experimental.analysis.cadence import cadence_stats, format_cadence
from arb.matching import start_grade
from arb.models import Quote
from arb.store import Store

NOW = datetime(2026, 7, 2, 18, 0, tzinfo=timezone.utc)


def test_start_grade_generalizes_tennis_fix():
    unreliable = {"polymarket": ["tennis"]}
    assert start_grade("polymarket", "tennis", unreliable) == "nominal"
    assert start_grade("polymarket", "baseball_mlb", unreliable) == "verified"
    assert start_grade("kalshi", "tennis", unreliable) == "verified"


def test_cadence_reports_actual_vs_planned(tmp_path):
    store = Store(tmp_path / "arb.sqlite")
    # three watch polls 163s apart — the real cadence, not the configured 60
    with store.connect() as con:
        for i in range(3):
            con.execute("INSERT INTO polls(started_at, kind) VALUES (?, 'watch')",
                        ((NOW + timedelta(seconds=163 * i)).isoformat(),))
    stats = cadence_stats(store, "watch", planned_secs=60)
    assert stats["n_gaps"] == 2
    assert stats["p50_secs"] == 163.0 and stats["planned_secs"] == 60
    assert "2.7x planned" in format_cadence(stats)     # honest drift, not the config lie


def _quote(cost):
    return Quote(event_key="mma:2026-07-11:a:b", venue="polymarket", book="b", market_type="WINNER",
                 line=None, outcome_label="bobby green", raw_price=Decimal(cost), cost=Decimal(cost),
                 fee_cost=Decimal("0.001"), depth_usd=Decimal("600"), live=False, fetched_at=NOW,
                 start_utc=NOW + timedelta(hours=5), home="a", away="b")


def test_consensus_age_secs_stamped_on_ev_row():
    config = load_config()
    computed = datetime.now(timezone.utc) - timedelta(seconds=1200)   # 20-min-old consensus
    consensus = {("mma:2026-07-11:a:b", "bobby green"): (Decimal("0.45"), computed)}
    opps = detect_ev_from_consensus([_quote("0.35")], consensus, max_age_hours=8,
                                    trusted_pairs=set(), config=config)
    assert opps
    age_notes = [n for n in opps[0].notes if n.startswith("consensus_age_secs:")]
    assert age_notes and 1100 < int(age_notes[0].split(":")[1]) < 1300
