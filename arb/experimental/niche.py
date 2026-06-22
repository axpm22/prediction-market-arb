"""Curated niche cross-venue verticals — weather, World Cup, macro.

NOT fuzzy matching. Each pair is a hand-written entry in data/nonsports_map.yaml that records
both venues' rulebook fields. The hard safety rule: an entry can only go live if
`validate_entry` proves the two rulebooks resolve the SAME question — same source/station,
same period rules, same void handling, same threshold. Everything else stays REVIEW and is
never auto-filled; settlement is manual until each vertical has a verified adapter.

This is the opposite of broad-politics cross-venue, where near-identical wording quietly
resolves differently and turns an "arb" into a loss.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover
    yaml = None

MAP_PATH = Path("data/nonsports_map.yaml")
VERTICALS = {"weather", "worldcup", "macro"}
SAME_REQUIRED = "SAME_REQUIRED"


@dataclass(frozen=True)
class Validation:
    ok: bool
    field: str | None = None
    reason: str = "approved"

    @property
    def approved(self) -> bool:
        return self.ok


def _norm(value) -> str:
    """Canonicalize a rulebook value for comparison (case/space-insensitive)."""
    return str(value).strip().lower()


def normalize_rulebook(venue_entry: dict) -> dict:
    return {k: _norm(v) for k, v in (venue_entry.get("rulebook") or {}).items()}


def validate_entry(entry: dict) -> Validation:
    """can_enable_pair: prove both venues' rulebooks resolve the same question.

    For each `required_match` field: SAME_REQUIRED means the two venues must agree with each
    other; any other value means BOTH venues must equal that literal. void_rules must always
    match. World Cup adds extra_time_counts and draw_handling agreement.
    """
    vertical = entry.get("vertical")
    if vertical not in VERTICALS:
        return Validation(False, "vertical", f"unknown vertical {vertical!r}")
    if "kalshi" not in entry or "polymarket" not in entry:
        return Validation(False, "venues", "entry missing a venue section")

    left = normalize_rulebook(entry["kalshi"])
    right = normalize_rulebook(entry["polymarket"])
    required = entry.get("required_match") or {}
    if not required:
        return Validation(False, "required_match", "no required_match block to verify against")

    for field, expected in required.items():
        if field not in left or field not in right:
            return Validation(False, field, f"both venues must record rulebook.{field}")
        if _norm(expected) == _norm(SAME_REQUIRED):
            if left[field] != right[field]:
                return Validation(False, field, f"{field} differs: kalshi={left[field]} polymarket={right[field]}")
        else:
            exp = _norm(expected)
            if left[field] != exp or right[field] != exp:
                return Validation(False, field, f"{field} must be {exp} on both (kalshi={left[field]} polymarket={right[field]})")

    # void/postponement rules must always match, even if not listed in required_match.
    if left.get("void_rules") != right.get("void_rules"):
        return Validation(False, "void_rules", f"void_rules differ: {left.get('void_rules')} vs {right.get('void_rules')}")

    if vertical == "worldcup":
        for field in ("extra_time_counts", "penalties_count", "draw_handling"):
            if left.get(field) != right.get(field):
                return Validation(False, field, f"{field} differs: {left.get(field)} vs {right.get(field)}")

    return Validation(True)


def load_niche_map(path: Path = MAP_PATH) -> list[dict]:
    if yaml is None:
        raise RuntimeError("pyyaml is required to load the niche map.")
    if not path.exists():
        return []
    return [e for e in (yaml.safe_load(path.read_text()) or []) if isinstance(e, dict)]


def enabled_validated_entries(path: Path = MAP_PATH) -> list[dict]:
    """Entries cleared for live quoting: enabled AND reviewed AND validator-approved.

    An entry flagged enabled but failing validation is REFUSED (and the reason surfaced),
    never silently quoted — the whole point of the safety rule.
    """
    out = []
    for entry in load_niche_map(path):
        if not (entry.get("enabled") and entry.get("reviewed")):
            continue
        result = validate_entry(entry)
        if not result.approved:
            print(f"niche: REFUSING {entry.get('id')} — {result.field}: {result.reason}")
            continue
        out.append(entry)
    return out
