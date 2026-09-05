"""CI grep-tests (SPEC rules 9 and 12) + log redaction (SPEC §6.4)."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARB = ROOT / "arb"

FORBIDDEN = [
    re.compile(r"UPDATE\s+flat_bets", re.I),
    re.compile(r"DELETE\s+FROM\s+flat_bets", re.I),
    re.compile(r"UPDATE\s+bet_events", re.I),
    re.compile(r"DELETE\s+FROM\s+bet_events", re.I),
    # matpos fix pass (review of 783301f, M1): `prints` is the tape's RECORD
    # (rule 7) and its monotonic INTEGER PRIMARY KEY is load-bearing for the
    # positions cache — `whale_mat._enqueue` derives the dirty set from the
    # rowid range above `meta.mat_watermark`, and `freshness` measures how far
    # behind a reader is from MAX(id). A delete would silently make both lie.
    re.compile(r"DELETE\s+FROM\s+prints", re.I),
]


def _arb_sources():
    return list(ARB.rglob("*.py"))


def test_no_ledger_mutation_sql_under_arb():
    assert _arb_sources(), "arb/ package missing"
    for path in _arb_sources():
        text = path.read_text()
        for pat in FORBIDDEN:
            assert not pat.search(text), f"{pat.pattern} found in {path}"


def test_eligibility_predicate_defined_in_exactly_one_module():
    defining = [p for p in _arb_sources()
                if re.search(r"^def classify\(", p.read_text(), re.M)]
    # Before Phase 5 nothing defines it; from Phase 5 on, only arb/eligibility.py may.
    assert all(p.name == "eligibility.py" for p in defining), defining
    assert len(defining) <= 1


def test_log_sinks_redact_query_strings():
    from arb.logutil import redact
    assert "apiKey" not in redact(
        "GET https://api.the-odds-api.com/v4/sports/?apiKey=SECRET&regions=us -> 200")
    assert redact("no urls here") == "no urls here"
    # host+path stay greppable
    assert "api.the-odds-api.com/v4/sports/" in redact(
        "https://api.the-odds-api.com/v4/sports/?apiKey=SECRET")


def test_child_log_drain_redacts_each_line():
    # sibling sink to test_log_sinks_redact_query_strings (C1, review of
    # 9cb356b): a child's stdout/stderr used to hit logs/children/<name>.log
    # via a raw fd, bypassing every logging-based redaction path entirely.
    # arb/child_log.py's drain thread must apply the SAME redact() line by
    # line as it writes, not just at read time.
    import io

    from arb.child_log import _drain
    pipe = io.BytesIO(
        b"GET https://api.the-odds-api.com/v4/sports/?apiKey=SECRET&regions=us -> 200\n"
        b"plain line, nothing to redact\n")
    out = io.BytesIO()
    _drain(pipe, out)
    written = out.getvalue().decode()
    assert "apiKey" not in written and "SECRET" not in written
    assert "plain line, nothing to redact" in written


def test_regrade_never_touches_the_odds_api():
    # rule 2: arb regrade reads ONLY the local DB + the keyless MLB Stats API
    text = (ARB / "regrade.py").read_text()
    assert "oddsapi" not in text
    assert "INSERT" not in text.upper()


def test_no_test_config_points_at_the_production_tapes():
    # regrade 2026-08-29: tests/conftest.py's autouse `_no_production_tape`
    # fixture must keep every test's whale/maker/kalshi-history tape path off
    # the live files under data/ (rewritten every cycle by the running
    # collectors) — this is the trip-wire that fails loudly if it ever stops
    # applying, e.g. a future conftest edit that narrows or drops the fixture.
    from arb.config import load
    cfg = load(ROOT / "config.yaml")
    for section, key in (("whale", "tape_db"), ("maker", "tape_db"),
                         ("bench", "tape_db")):
        path = str(cfg[section][key])
        assert not path.startswith("data/"), f"{section}.{key} = {path}"


def test_no_hardcoded_schedule_times_or_windows():
    # rule 4 (review 2026-07-10): shape/canary fire times and the two 6h match/
    # settle windows moved to config.yaml; literals must not reappear in code
    hb = (ARB / "heartbeat.py").read_text()
    assert "05:00" not in hb and "05:30" not in hb
    assert "timedelta(hours=6)" not in (ARB / "matching.py").read_text()
    assert "timedelta(hours=6)" not in (ARB / "settlement.py").read_text()
