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
# Append-only lived-settlement observations. Lived settlements are the ONLY ground truth that
# counts (Phase B), so these override the hand-curated, rulebook-cited base per cell — without
# editing the base yaml (a PyYAML round-trip would destroy its citations/comments).
OBSERVED_PATH = Path("data/settlement_matrix_observed.yaml")
UNKNOWN = "UNKNOWN"


@lru_cache(maxsize=2)
def _load(path_str: str) -> dict:
    path = Path(path_str)
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def _merge_observed(base: dict, observed: dict) -> dict:
    """Deep-merge observed cells over base (observed wins per venue cell)."""
    merged = {sport: {sc: dict(cells) for sc, cells in (section or {}).items()}
              for sport, section in (base or {}).items()}
    for sport, section in (observed or {}).items():
        for scenario, cells in (section or {}).items():
            merged.setdefault(sport, {}).setdefault(scenario, {}).update(cells or {})
    return merged


def load_matrix(path: Path = MATRIX_PATH) -> dict:
    if _OVERRIDE is not None:
        return _OVERRIDE
    return _merge_observed(_load(str(path)), _load(str(OBSERVED_PATH)))


def record_observed_settlement(sport: str, scenario: str, venue: str, verdict: str,
                               path: Path | None = None) -> None:
    """Append a lived-settlement observation (reconcile retro-check). Overrides the base cell so a
    real cross-venue mismatch immediately reverts the pair to REVIEW via section_green — without
    touching the cited base yaml. Latest observation per (sport, scenario, venue) wins."""
    path = path or OBSERVED_PATH  # resolve at call time so ops/tests can repoint OBSERVED_PATH
    data = (yaml.safe_load(path.read_text()) if path.exists() else {}) or {}
    data.setdefault(sport, {}).setdefault(scenario, {})[venue] = str(verdict)
    header = ("# Lived-settlement observations (arb reconcile retro-check). Overrides the curated\n"
              "# data/settlement_matrix.yaml per cell — lived settlements are the only ground truth.\n"
              "# Machine-written; latest observation per cell wins. Rebuildable from the ledger.\n")
    path.write_text(header + yaml.safe_dump(data, sort_keys=True))
    _load.cache_clear()


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


def warn_ungreen_trusted_pairs(trusted_pairs, matrix: dict | None = None) -> list[str]:
    """Phase B startup guard. A `trusted_pairs` entry that points at a non-green matrix section is
    already ignored by the trusted gate (status_for_legs demotes it to REVIEW) — but silently. Warn
    loudly at startup so an ineffective whitelist entry is never invisible. Returns the warnings."""
    warnings: list[str] = []
    for entry in sorted(str(e) for e in trusted_pairs):
        venue_part, _, sport = entry.partition(":")
        if not sport:
            continue  # sport-less entry — the gate resolves it per-opportunity
        issues = section_issues(sport, venue_part.split("+"), matrix)
        if issues:
            shown = ", ".join(issues[:3]) + ("…" if len(issues) > 3 else "")
            msg = (f"WARNING: trusted_pairs '{entry}' points at a NON-GREEN settlement-matrix "
                   f"section — IGNORED (stays REVIEW) until green. Blocking: {shown}")
            warnings.append(msg)
            print(msg)
    return warnings


def format_matrix(matrix: dict | None = None) -> str:
    """Human-readable dump of the effective matrix (base + observed) with each sport's green status
    for the kalshi+polymarket pair — the counterfactual 'which sports would be whitelistable'."""
    matrix = matrix if matrix is not None else load_matrix()
    lines = ["Settlement matrix (effective = curated base + lived observations):"]
    for sport in sorted(matrix):
        issues = section_issues(sport, ["kalshi", "polymarket"], matrix)
        status = "GREEN — whitelistable" if not issues else f"REVIEW — {len(issues)} blocking scenario(s)"
        lines.append(f"\n## {sport}: {status}")
        for scenario, cells in matrix[sport].items():
            flag = "" if scenario not in issues else "  <- blocks"
            rendered = ", ".join(f"{v}={cells[v]}" for v in sorted(cells)) if isinstance(cells, dict) else str(cells)
            lines.append(f"  {scenario}: {rendered}{flag}")
    return "\n".join(lines)
