"""08 (slice) — Phase F cadence honesty wired into the daily report.

The p95 poll gap printed in the report is the loop's REAL spec — no silent "60s" claim.
"""
from datetime import datetime, timedelta, timezone

from arb.experimental.analysis.cadence import cadence_stats, format_cadence
from arb.tracker import _cadence_lines, write_daily_report
from arb.store import Store

T0 = datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc)


def _polls(store: Store, kind: str, gaps_secs: list[int]) -> None:
    t = T0
    with store.connect() as con:
        con.execute("INSERT INTO polls(started_at, kind) VALUES (?, ?)", (t.isoformat(), kind))
        for g in gaps_secs:
            t = t + timedelta(seconds=g)
            con.execute("INSERT INTO polls(started_at, kind) VALUES (?, ?)", (t.isoformat(), kind))


def test_cadence_stats_p50_p95(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    _polls(store, "watch", [60, 140, 300])  # three gaps
    st = cadence_stats(store, "watch", planned_secs=60)
    assert st["n_gaps"] == 3
    assert st["p50_secs"] == 140.0          # median of [60,140,300]
    assert st["p95_secs"] == 300.0          # top gap
    assert st["planned_secs"] == 60
    assert "2.3x planned" in format_cadence(st)  # 140/60 ≈ 2.3 — the honest drift


def test_cadence_section_omitted_when_no_polls() -> None:
    assert _cadence_lines(None) == []
    assert _cadence_lines([]) == []


def test_daily_report_includes_cadence_section(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    _polls(store, "watch", [60, 140, 300])
    _, md_path = write_daily_report(store, output_dir=tmp_path / "reports")
    text = md_path.read_text()
    assert "## Cadence (Phase F" in text
    assert "cadence[watch]" in text and "p95" in text
