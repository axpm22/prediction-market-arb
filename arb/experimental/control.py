"""Dashboard control plane — config overlay writes only, never the ledger.

Surfaces strategy/worker lanes and model *versions* as toggles and persists each change to
`config.local.yaml` via `arb.config` — a deep-merged overlay, so the canonical `config.yaml`
(CLAUDE.md rule #4) is never mutated and reverting a toggle is just deleting the key.

Hard guarantees:
- This module reads/writes config only. It NEVER touches `flat_bets` or any ledger table.
- It NEVER enables order placement. `betting_enabled`-class flags are marked `locked` and the
  dashboard refuses to flip them — they stay managed in `config.yaml` by hand. The system is
  paper-only by construction; surfacing a betting flag does not wire any order path.
- The served payload is an explicit allowlist (REGISTRY) — no secrets (`ODDS_API_KEY`/.env)
  can leak because nothing outside the registry is ever returned.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover
    yaml = None

from arb.config import DEFAULT_CONFIG_PATH, effective_raw, load_overlay, overlay_path

# Each flag: id, label, group, path (dotted config key), type, options (enum), description,
# restart (which worker must restart to pick it up), betting (guardrail), locked (refuse
# writes), pnl_kind (ledger `kind` whose realized P&L is this version's head-to-head line).
REGISTRY: list[dict[str, Any]] = [
    # --- Strategy / worker lanes ---
    {"id": "micro_enabled", "label": "Micro-EV lane", "group": "Lanes", "path": "micro.enabled",
     "type": "bool", "restart": "daemon", "pnl_kind": "EV:micro",
     "desc": "Thin-book EV ($5–$50 depth), stricter edge, depth-capped stake. Kept off the headline."},
    {"id": "ev_exit_enabled", "label": "EV-exit lane", "group": "Lanes", "path": "ev_exit.enabled",
     "type": "bool", "restart": "ev-exit",
     "desc": "Paper take-profit exits at the executable sell bid once remaining edge is mostly gone."},
    {"id": "ev_exit_stop_loss", "label": "EV-exit stop-loss", "group": "Lanes", "path": "ev_exit.stop_loss_enabled",
     "type": "bool", "restart": "ev-exit",
     "desc": "Cut a losing EV position once it is down stop_loss_roi AND model edge has gone to the floor."},
    {"id": "negrisk_enabled", "label": "Negative-risk sweep", "group": "Lanes", "path": "negrisk.enabled",
     "type": "bool", "restart": "negrisk", "pnl_kind": "NEGRISK",
     "desc": "Single-venue basket arbs; only clear if capturable depth exists."},
    {"id": "parlay_enabled", "label": "Parlay", "group": "Lanes", "path": "parlay.enabled",
     "type": "bool", "restart": "daemon",
     "desc": "Correlated-leg divergence parlays (research lane)."},
    {"id": "live_baseball_enabled", "label": "Live Baseball EV (measure)", "group": "Lanes", "path": "live_baseball_ev.enabled",
     "type": "bool", "restart": "daemon",
     "desc": "In-play MLB EV measurement lane. Isolated; never folded into clean pregame EV."},
    {"id": "mlbstats_enabled", "label": "MLB Stats settle", "group": "Lanes", "path": "mlbstats_enabled",
     "type": "bool", "restart": "daemon",
     "desc": "Backfill MLB settlements from the MLB Stats API (rescues the Odds-API window backlog)."},
    {"id": "consensus_enabled", "label": "Consensus EV engine", "group": "Lanes", "path": "consensus_enabled",
     "type": "bool", "restart": "daemon", "pnl_kind": "EV:clean",
     "desc": "Compute de-vigged consensus EV flags. Betting stays paper; every flag is REVIEW-gated."},
    # --- Model versions ---
    {"id": "devig_method", "label": "De-vig method", "group": "Versions", "path": "devig_method",
     "type": "enum", "options": ["proportional", "power", "shin"], "restart": "daemon",
     "desc": "How the vig is removed to get the fair consensus probability. Pick the one validated on CLV."},
    {"id": "weight_sharp_books", "label": "Sharp-book weighting (EV#2)", "group": "Versions", "path": "weight_sharp_books",
     "type": "bool", "restart": "daemon",
     "desc": "Weight the consensus toward books shown sharp on CLV (sharp_books) instead of a plain median."},
    {"id": "ev_dispersion_gate", "label": "EV uncertainty gate (EV#3)", "group": "Versions", "path": "ev.dispersion_gate",
     "type": "bool", "restart": "daemon",
     "desc": "Treat p̂ as uncertain: require edge ≥ edge_se_min × cross-book dispersion and size off the lower bound."},
    {"id": "ev_edge_decay", "label": "Stale-consensus decay (EV#7)", "group": "Versions", "path": "ev.edge_decay_per_hour",
     "type": "number", "min": 0.0, "max": 0.05, "step": 0.001, "restart": "daemon",
     "desc": "Edge haircut per hour of consensus age before the floor. 0 = legacy binary stale cutoff."},
    {"id": "ev_enforce_depth_floor", "label": "EV depth floor", "group": "Versions", "path": "ev.enforce_depth_floor",
     "type": "bool", "restart": "daemon",
     "desc": "Require depth ≥ min_executable_usd for an EV flag to count as clean."},
    {"id": "ev_reject_one_sided", "label": "Reject one-sided books", "group": "Versions", "path": "ev.reject_one_sided_books",
     "type": "bool", "restart": "daemon",
     "desc": "Drop books quoting only one side (no implied vig to remove → unreliable fair value)."},
    {"id": "slippage_model", "label": "Slippage model (does-well#2)", "group": "Versions", "path": "execution.slippage_model",
     "type": "enum", "options": ["flat", "ladder"], "restart": "daemon", "versioned_cost": True,
     "desc": "Cost-vector version: 'flat' constant buffer vs 'ladder' depth-scaled walk. Changes every recorded cost."},
    {"id": "arb_size_by_ladder", "label": "Ladder-scaled ARB sizing", "group": "Versions", "path": "execution.arb_size_by_ladder",
     "type": "bool", "restart": "daemon",
     "desc": "Size ARBs off the joint edge×depth ladder break-even (needs captured multi-level ladders)."},
    {"id": "negrisk_exhaustive", "label": "NegRisk exhaustive baskets", "group": "Versions", "path": "negrisk.require_exhaustive_exclusive",
     "type": "bool", "restart": "negrisk",
     "desc": "Only clear a basket when the outcomes are proven mutually exclusive AND exhaustive."},
    # --- Betting guardrail (surfaced, locked) ---
    {"id": "live_betting_enabled", "label": "Live-baseball BETTING", "group": "Guardrail", "path": "live_baseball_ev.betting_enabled",
     "type": "bool", "betting": True, "locked": True, "restart": "daemon",
     "desc": "Paper-only project: this is read-only here. Promotion to betting is gated in config.yaml, never the UI."},
]
REGISTRY_BY_ID = {flag["id"]: flag for flag in REGISTRY}


def _get(raw: dict[str, Any], dotted: str) -> Any:
    node: Any = raw
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _set(raw: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = raw
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


def _in_overlay(overlay: dict[str, Any], dotted: str) -> bool:
    node: Any = overlay
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def _coerce(flag: dict[str, Any], value: Any) -> Any:
    kind = flag["type"]
    if kind == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"true", "1", "yes", "on"}
    if kind == "enum":
        text = str(value)
        if text not in flag.get("options", []):
            raise ValueError(f"{text!r} is not one of {flag['options']}")
        return text
    if kind == "number":
        num = float(value)
        lo, hi = flag.get("min"), flag.get("max")
        if lo is not None and num < lo:
            raise ValueError(f"{num} below minimum {lo}")
        if hi is not None and num > hi:
            raise ValueError(f"{num} above maximum {hi}")
        return num
    raise ValueError(f"unknown flag type {kind!r}")


def effective_controls(base: Path = DEFAULT_CONFIG_PATH,
                       pnl_by_kind: dict[str, float] | None = None) -> list[dict[str, Any]]:
    """Current value + source for every registry flag (read-only; safe to serve)."""
    raw = effective_raw(base)
    overlay = load_overlay(base)
    out: list[dict[str, Any]] = []
    for flag in REGISTRY:
        value = _get(raw, flag["path"])
        pnl = None
        if pnl_by_kind is not None and flag.get("pnl_kind"):
            pnl = pnl_by_kind.get(flag["pnl_kind"])
        out.append({
            "id": flag["id"], "label": flag["label"], "group": flag["group"],
            "type": flag["type"], "options": flag.get("options"),
            "value": value, "source": "overlay" if _in_overlay(overlay, flag["path"]) else "base",
            "desc": flag["desc"], "restart": flag.get("restart"),
            "betting": bool(flag.get("betting")), "locked": bool(flag.get("locked")),
            "versioned_cost": bool(flag.get("versioned_cost")),
            "min": flag.get("min"), "max": flag.get("max"), "step": flag.get("step"),
            "pnl": pnl, "pnl_kind": flag.get("pnl_kind"),
        })
    return out


def _atomic_write_overlay(data: dict[str, Any], base: Path = DEFAULT_CONFIG_PATH) -> Path:
    if yaml is None:
        raise RuntimeError("pyyaml is required to write the config overlay.")
    path = overlay_path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():  # keep a one-deep backup of the prior overlay before replacing it
        path.with_name(path.name + ".bak").write_text(path.read_text())
    header = (
        "# Machine-managed by the dashboard control plane (arb.experimental.control).\n"
        "# Deep-merged on top of config.yaml at load. Delete a key to revert to the canonical value.\n"
        f"# Last written {datetime.now(timezone.utc).isoformat()}\n"
    )
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(header + yaml.safe_dump(data, sort_keys=True, default_flow_style=False))
    os.replace(tmp, path)
    return path


def set_control(flag_id: str, value: Any, base: Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    flag = REGISTRY_BY_ID.get(flag_id)
    if flag is None:
        return {"ok": False, "error": f"unknown control {flag_id!r}"}
    if flag.get("locked"):
        return {"ok": False, "error": f"{flag['label']} is a betting guardrail — managed in config.yaml only, never the dashboard (paper-only)."}
    try:
        coerced = _coerce(flag, value)
    except (ValueError, TypeError) as exc:
        return {"ok": False, "error": str(exc)}
    overlay = load_overlay(base)
    _set(overlay, flag["path"], coerced)
    _atomic_write_overlay(overlay, base)
    return {
        "ok": True, "id": flag_id, "value": coerced, "source": "overlay",
        "restart": flag.get("restart"),
        "message": f"{flag['label']} → {coerced}" + (f" · restart {flag['restart']} to apply" if flag.get("restart") else ""),
    }
