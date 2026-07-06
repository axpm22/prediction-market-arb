"""Rule 9 guard: no production code may UPDATE or DELETE the append-only flat_bets ledger.

Corrections land as new annotation rows, never in-place edits. This mirrors the SPEC_V2
T-ELIG-5 grep-test convention: assert the dangerous string does not exist under arb/.
(Tests may set up fixtures with UPDATE — only arb/ source is scanned.)
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = re.compile(r"(UPDATE\s+flat_bets|DELETE\s+FROM\s+flat_bets)", re.IGNORECASE)


def test_no_flat_bets_mutation_in_source():
    offenders = []
    for py in (ROOT / "arb").rglob("*.py"):
        for i, line in enumerate(py.read_text().splitlines(), 1):
            if FORBIDDEN.search(line):
                offenders.append(f"{py.relative_to(ROOT)}:{i}: {line.strip()}")
    assert not offenders, "flat_bets is append-only (rule 9); found:\n" + "\n".join(offenders)
