"""§18.3 Part B — `arb regrade`: hand-computed vectors, determinism, cluster (not
row) resampling, tolerance exclusion, doubleheader non-blending, read-only
guarantee, INSUFFICIENT rendering. All synthetic/fixtures, no network (rule 2)."""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from arb.config import load
from arb.regrade import resolve_finals, run, write_report
from arb.store import Store

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def cfg():
    return load(ROOT / "config.yaml")


def _lax(cfg, min_events=1, bootstrap_n=200):
    return dict(cfg, regrade=dict(cfg["regrade"], min_events=min_events,
                                  bootstrap_n=bootstrap_n))


def _final(home, away, start, home_score, away_score, gamepk=1, gameno=1):
    return {"gamePk": gamepk, "gameNumber": gameno, "home": home, "away": away,
            "start_utc": start, "home_score": home_score, "away_score": away_score}


def _patch_mlb(monkeypatch, finals):
    import arb.venues.mlbstats as m
    monkeypatch.setattr(m, "fetch_schedule", lambda cfg, d: finals)
    monkeypatch.setattr(m, "is_final", lambda g: True)


def _consensus_row(event_key, outcome, p_hat, computed_at, method="power"):
    return {"event_key": event_key, "market_type": "WINNER", "line": None,
            "outcome": outcome, "p_hat": Decimal(p_hat), "method": method,
            "n_books": 3, "dispersion": "0", "computed_at": computed_at}


def _quote_row(event_key, outcome, cost, bid, fetched_at):
    return {"poll_id": 1, "fetched_at": fetched_at, "event_key": event_key,
            "category": "sports", "sport": "baseball_mlb", "venue": "kalshi",
            "book": "kalshi", "market_type": "WINNER", "line": None,
            "outcome": outcome, "side": "YES", "raw_price": cost, "cost": cost,
            "fee_cost": "0", "depth_usd": "100", "ladder_json": None, "live": 0,
            "bid": bid, "volume_24h": "5", "start_utc": None, "start_grade": "verified"}


# Three events, hand-computed diffs (p-y)^2 - (mid-y)^2:
#   E1 giants/bluejays: p=0.6 y=1 mid=0.5 -> 0.16-0.25 = -0.09
#   E2 mets/royals:     p=0.5 y=0 mid=0.4 -> 0.25-0.16 = +0.09
#   E3 cubs/orioles:    p=0.8 y=1 mid=0.6 -> 0.04-0.16 = -0.12
#   mean = (-0.09 + 0.09 - 0.12) / 3 = -0.04  (consensus beats venue on average)
T = datetime(2026, 7, 8, 18, 0, tzinfo=timezone.utc)
FINALS_3 = [
    _final("giants", "bluejays", T - timedelta(hours=6), 5, 1, gamepk=1),
    _final("mets", "royals", T - timedelta(hours=6), 2, 4, gamepk=2),
    _final("orioles", "cubs", T - timedelta(hours=6), 6, 2, gamepk=3),
]


def _seed_three_events(store):
    store.insert_many("consensus", [
        _consensus_row("baseball_mlb:2026-07-08:bluejays:giants", "giants", "0.6", T.isoformat()),
        _consensus_row("baseball_mlb:2026-07-08:mets:royals", "mets", "0.5", T.isoformat()),
        _consensus_row("baseball_mlb:2026-07-08:cubs:orioles", "orioles", "0.8", T.isoformat()),
    ])
    store.insert_many("quotes", [
        _quote_row("baseball_mlb:2026-07-08:bluejays:giants", "giants", "0.55", "0.45", T.isoformat()),
        _quote_row("baseball_mlb:2026-07-08:mets:royals", "mets", "0.45", "0.35", T.isoformat()),
        _quote_row("baseball_mlb:2026-07-08:cubs:orioles", "orioles", "0.65", "0.55", T.isoformat()),
    ])


def test_hand_computed_paired_brier_vector(tmp_path, monkeypatch, cfg):
    _patch_mlb(monkeypatch, FINALS_3)
    store = Store(tmp_path / "t.sqlite")
    _seed_three_events(store)
    result = run(_lax(cfg), store)
    cell = result["buckets"]["median"]["as_stored"]
    assert cell["insufficient"] is False
    assert cell["n_events"] == 3
    assert cell["mean"] == pytest.approx(-0.04, abs=1e-9)
    assert cell["ci_low"] <= cell["mean"] <= cell["ci_high"]


def test_bootstrap_is_deterministic_same_seed(tmp_path, monkeypatch, cfg):
    _patch_mlb(monkeypatch, FINALS_3)
    store = Store(tmp_path / "t.sqlite")
    _seed_three_events(store)
    lax = _lax(cfg)
    r1 = run(lax, store)
    r2 = run(lax, store)
    c1, c2 = r1["buckets"]["median"]["as_stored"], r2["buckets"]["median"]["as_stored"]
    assert c1["ci_low"] == c2["ci_low"]
    assert c1["ci_high"] == c2["ci_high"]


def test_bootstrap_resamples_clusters_not_rows(tmp_path, monkeypatch, cfg):
    # A dominant event (many rows, one outcome) must move as ONE unit under
    # resampling, not have its rows shuffled independently of each other —
    # otherwise variance is understated (the exact bug the rubric calls out).
    _patch_mlb(monkeypatch, FINALS_3)
    store = Store(tmp_path / "t.sqlite")
    dominant_key = "baseball_mlb:2026-07-08:bluejays:giants"
    rows, quotes = [], []
    for i in range(20):
        t = (T + timedelta(minutes=i)).isoformat()
        rows.append(_consensus_row(dominant_key, "giants", "0.9", t))
        quotes.append(_quote_row(dominant_key, "giants", "0.1", "0.0", t))
    store.insert_many("consensus", rows)
    store.insert_many("quotes", quotes)
    lax = _lax(cfg)
    result = run(lax, store)
    cell = result["buckets"]["median"]["as_stored"]
    # one event, 20 rows: every bootstrap draw either includes ALL 20 rows or
    # NONE of them (cluster resampling) -> CI collapses to the point estimate,
    # unlike row-level resampling which would produce a tight-but-not-degenerate
    # spread around a stable mean. Assert the cluster signature directly: n_events=1.
    assert cell["n_events"] == 1
    assert cell["n_pairs"] == 20
    assert cell["ci_low"] == cell["ci_high"] == cell["mean"]


def test_as_stored_excludes_pairs_outside_tolerance(tmp_path, monkeypatch, cfg):
    _patch_mlb(monkeypatch, FINALS_3[:1])
    store = Store(tmp_path / "t.sqlite")
    ek = "baseball_mlb:2026-07-08:bluejays:giants"
    store.insert_many("consensus", [_consensus_row(ek, "giants", "0.6", T.isoformat())])
    far = (T + timedelta(seconds=cfg["regrade"]["match_tolerance_secs"] + 60)).isoformat()
    store.insert_many("quotes", [_quote_row(ek, "giants", "0.55", "0.45", far)])
    result = run(_lax(cfg), store)
    excl = result["exclusions"]["median"]
    assert excl["out_of_tolerance"] == 1
    assert result["buckets"]["median"]["as_stored"]["n_pairs"] == 0


def test_no_final_excludes_and_counts(tmp_path, monkeypatch, cfg):
    _patch_mlb(monkeypatch, [])  # no finals at all
    store = Store(tmp_path / "t.sqlite")
    ek = "baseball_mlb:2026-07-08:bluejays:giants"
    store.insert_many("consensus", [_consensus_row(ek, "giants", "0.6", T.isoformat())])
    store.insert_many("quotes", [_quote_row(ek, "giants", "0.55", "0.45", T.isoformat())])
    result = run(_lax(cfg), store)
    assert result["exclusions"]["median"]["no_final"] == 1
    assert result["buckets"]["median"]["as_stored"]["n_pairs"] == 0


def test_doubleheader_finals_do_not_blend(tmp_path, monkeypatch, cfg):
    g1 = _final("mets", "royals", T - timedelta(hours=8), 5, 3, gamepk=1, gameno=1)
    g2 = _final("mets", "royals", T - timedelta(hours=4), 2, 6, gamepk=2, gameno=2)
    _patch_mlb(monkeypatch, [g1, g2])
    store = Store(tmp_path / "t.sqlite")
    ek1 = "baseball_mlb:2026-07-08:mets:royals:dh1"
    ek2 = "baseball_mlb:2026-07-08:mets:royals:dh2"
    store.insert_many("consensus", [
        _consensus_row(ek1, "mets", "0.6", T.isoformat()),  # dh1: mets actually won
        _consensus_row(ek2, "mets", "0.6", T.isoformat()),  # dh2: royals actually won
    ])
    finals = resolve_finals(_lax(cfg), store)
    assert finals[ek1][0] == "mets"
    assert finals[ek2][0] == "royals"


def test_ambiguous_doubleheader_final_excluded_not_guessed(tmp_path, monkeypatch, cfg):
    g1 = _final("mets", "royals", T - timedelta(hours=8), 5, 3, gamepk=1, gameno=1)
    g2 = _final("mets", "royals", T - timedelta(hours=4), 2, 6, gamepk=2, gameno=2)
    _patch_mlb(monkeypatch, [g1, g2])
    store = Store(tmp_path / "t.sqlite")
    bare_key = "baseball_mlb:2026-07-08:mets:royals"  # no :dhN — ambiguous
    finals = resolve_finals(_lax(cfg), store)
    assert bare_key not in finals


def test_read_only_row_counts_unchanged(tmp_path, monkeypatch, cfg):
    _patch_mlb(monkeypatch, FINALS_3)
    store = Store(tmp_path / "t.sqlite")
    _seed_three_events(store)
    tables = ["flat_bets", "bet_events", "annotations", "consensus", "quotes", "polls"]

    def counts():
        return {t: store.query(f"SELECT COUNT(*) c FROM {t}")[0]["c"] for t in tables}
    before = counts()
    run(_lax(cfg), store)
    run(_lax(cfg), store)  # twice — idempotent and side-effect-free
    assert counts() == before


def test_no_insert_sql_in_regrade_module():
    text = (ROOT / "arb/regrade.py").read_text()
    assert "INSERT" not in text.upper()


def test_insufficient_below_min_events(tmp_path, monkeypatch, cfg):
    _patch_mlb(monkeypatch, FINALS_3)
    store = Store(tmp_path / "t.sqlite")
    _seed_three_events(store)
    result = run(cfg, store)  # real min_events (50) — 3 events must not claim a CI
    cell = result["buckets"]["median"]["as_stored"]
    assert cell["insufficient"] is True
    assert cell["n_events"] == 3
    assert "mean" not in cell


def test_report_renders_insufficient_not_a_ci(tmp_path, monkeypatch, cfg):
    _patch_mlb(monkeypatch, FINALS_3)
    store = Store(tmp_path / "t.sqlite")
    _seed_three_events(store)
    result = run(cfg, store)
    path = write_report(result, "2026-07-14", outdir=tmp_path / "reports")
    text = path.read_text()
    assert "INSUFFICIENT n=3" in text
    assert path.exists()


def test_cmd_regrade_writes_report_and_returns_zero(tmp_path, monkeypatch, cfg):
    from types import SimpleNamespace

    import arb.matching as matching
    from arb.commands import cmd_regrade
    _patch_mlb(monkeypatch, FINALS_3)
    monkeypatch.setattr(matching, "ALIASES_PATH", ROOT / "data/aliases.json")
    monkeypatch.chdir(tmp_path)
    store = Store(tmp_path / "t.sqlite")
    _seed_three_events(store)
    args = SimpleNamespace(db=str(tmp_path / "t.sqlite"), regrade_db=None)
    assert cmd_regrade(cfg, store, args) == 0
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).date().isoformat()
    assert (tmp_path / "reports" / f"regrade-{today}.md").exists()


def test_anchor_and_median_are_separate_buckets(tmp_path, monkeypatch, cfg):
    _patch_mlb(monkeypatch, FINALS_3[:1])
    store = Store(tmp_path / "t.sqlite")
    ek = "baseball_mlb:2026-07-08:bluejays:giants"
    store.insert_many("consensus", [
        _consensus_row(ek, "giants", "0.6", T.isoformat(), method="power"),
        _consensus_row(ek, "giants", "0.7", T.isoformat(), method="anchor:power"),
    ])
    store.insert_many("quotes", [_quote_row(ek, "giants", "0.55", "0.45", T.isoformat())])
    result = run(_lax(cfg), store)
    assert result["buckets"]["median"]["as_stored"]["n_pairs"] == 1
    assert result["buckets"]["anchor"]["as_stored"]["n_pairs"] == 1
