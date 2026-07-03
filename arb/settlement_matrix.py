"""MODEL_FOUNDATIONS Phase B — settlement equivalence as a data structure.

A cross-venue pair is only as trusted as the (sport, scenario) cells that have been checked. This
module loads data/settlement_matrix.yaml and answers the one question the trusted gate needs:
is a sport's section GREEN for the venues involved — every scenario filled (no UNKNOWN) and no
venue disagreement? Any hole or disagreement keeps the pair in REVIEW, with the offending
scenario named so the note is actionable (`settle-mismatch:mma-cancelled_bout`).
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

MATRIX_PATH = Path("data/settlement_matrix.yaml")
UNKNOWN = "UNKNOWN"


@lru_cache(maxsize=1)
def _load(path_str: str) -> dict:
    path = Path(path_str)
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def load_matrix(path: Path = MATRIX_PATH) -> dict:
    return _OVERRIDE if _OVERRIDE is not None else _load(str(path))


# Test/ops override so callers of section_issues that don't thread a matrix (status_for_legs) can
# still be exercised against a controlled section. None = use the shipped YAML.
_OVERRIDE: dict | None = None


def use_matrix(matrix: dict | None) -> None:
    global _OVERRIDE
    _OVERRIDE = matrix


def _verdict_kind(verdict: str) -> str:
    """Collapse a cell to what matters for agreement: SETTLES / VOIDS / CONDITIONAL / UNKNOWN.
    Two venues agree iff their kinds match (a CONDITIONAL rule text differs per venue by nature,
    so CONDITIONAL vs anything is treated as a disagreement — it needs human sign-off)."""
    v = str(verdict).strip().upper()
    if v.startswith("CONDITIONAL"):
        return "CONDITIONAL"
    return v if v in {"SETTLES", "VOIDS", UNKNOWN} else UNKNOWN


def section_issues(sport: str, venues, matrix: dict | None = None) -> list[str]:
    """Return the list of blocking scenarios for `sport` across `venues` (empty = GREEN).
    A scenario blocks if any involved venue is UNKNOWN/missing, or the involved venues disagree."""
    matrix = matrix if matrix is not None else load_matrix()
    section = matrix.get(sport)
    venues = [v for v in venues if v != "oddsapi"]
    if not section:
        return [f"{sport}-no-matrix-section"]
    issues: list[str] = []
    for scenario, cells in section.items():
        kinds = []
        for venue in venues:
            kind = _verdict_kind(cells.get(venue, UNKNOWN)) if isinstance(cells, dict) else UNKNOWN
            kinds.append(kind)
        if UNKNOWN in kinds or "CONDITIONAL" in kinds or len(set(kinds)) > 1:
            issues.append(scenario)
    return issues


def section_green(sport: str, venues, matrix: dict | None = None) -> bool:
    return not section_issues(sport, venues, matrix)
