"""CI grep-tests (SPEC rules 9 and 12) + log redaction (SPEC §6.4)."""
import re
from pathlib import Path

ARB = Path(__file__).resolve().parent.parent / "arb"

FORBIDDEN = [
    re.compile(r"UPDATE\s+flat_bets", re.I),
    re.compile(r"DELETE\s+FROM\s+flat_bets", re.I),
    re.compile(r"UPDATE\s+bet_events", re.I),
    re.compile(r"DELETE\s+FROM\s+bet_events", re.I),
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
