"""Settlement matrix loader (SPEC §11.4): the gate to any trusted path."""
from __future__ import annotations

from pathlib import Path

import yaml

MATRIX_PATH = Path("data/settlement_matrix.yaml")


def load_matrix(path: str | Path = MATRIX_PATH) -> dict:
    return yaml.safe_load(Path(path).read_text()) or {}


def sport_all_green(matrix: dict, sport: str) -> bool:
    """Trusted gate: every cell for the sport GREEN (§11.4)."""
    cells = matrix.get(sport)
    if not cells:
        return False  # no matrix section = not evaluated = not trusted
    return all(c.get("status") == "GREEN" for c in cells.values())
