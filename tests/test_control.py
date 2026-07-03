"""Control plane: config.local.yaml overlay writes, never the canonical config or ledger."""
import shutil
from pathlib import Path

import pytest

from arb.config import effective_raw, load_config, overlay_path
from arb.experimental import control

REAL_CONFIG = Path("config.yaml")


@pytest.fixture
def cfg(tmp_path):
    """Isolated copy of the real config.yaml so overlay writes never touch the repo."""
    dst = tmp_path / "config.yaml"
    shutil.copy(REAL_CONFIG, dst)
    return dst


def test_overlay_deep_merges_and_load_config_reflects_it(cfg):
    assert load_config(cfg).devig_method == "power"  # canonical baseline (Phase A: proportional→power)
    res = control.set_control("devig_method", "shin", base=cfg)
    assert res["ok"] and res["value"] == "shin"
    # canonical file untouched; overlay carries the change; load_config merges it.
    assert "devig_method: power" in cfg.read_text() and "devig_method: shin" not in cfg.read_text()
    assert overlay_path(cfg).exists()
    assert effective_raw(cfg)["devig_method"] == "shin"
    assert load_config(cfg).devig_method == "shin"


def test_overlay_merges_nested_without_dropping_siblings(cfg):
    control.set_control("micro_enabled", False, base=cfg)
    cfgobj = load_config(cfg)
    assert cfgobj.micro_enabled is False
    # sibling micro.* values from the base survive the partial overlay merge.
    assert cfgobj.micro_min_depth_usd > 0 and cfgobj.micro_max_stake_usd > 0


def test_second_write_keeps_a_backup(cfg):
    control.set_control("weight_sharp_books", True, base=cfg)
    control.set_control("weight_sharp_books", False, base=cfg)
    backup = overlay_path(cfg).with_name(overlay_path(cfg).name + ".bak")
    assert backup.exists()
    assert load_config(cfg).weight_sharp_books is False


def test_betting_guardrail_is_locked(cfg):
    res = control.set_control("live_betting_enabled", True, base=cfg)
    assert not res["ok"] and "paper" in res["error"].lower()
    assert not overlay_path(cfg).exists()  # nothing was written
    assert load_config(cfg).live_baseball_ev.get("betting_enabled") is False


def test_enum_and_number_validation(cfg):
    assert not control.set_control("devig_method", "banana", base=cfg)["ok"]
    assert not control.set_control("ev_edge_decay", 99, base=cfg)["ok"]   # above max
    ok = control.set_control("ev_edge_decay", 0.01, base=cfg)
    assert ok["ok"] and ok["value"] == 0.01
    assert not control.set_control("does_not_exist", True, base=cfg)["ok"]


def test_effective_controls_serves_no_secrets(cfg):
    rows = control.effective_controls(cfg, pnl_by_kind={"EV:clean": 31.75, "NEGRISK": 0.0})
    ids = {r["id"] for r in rows}
    assert "devig_method" in ids and "live_betting_enabled" in ids
    blob = str(rows).lower()
    assert "odds_api_key" not in blob and "api_key" not in blob and "secret" not in blob
    consensus = next(r for r in rows if r["id"] == "consensus_enabled")
    assert consensus["pnl"] == 31.75  # head-to-head P&L attached from the ledger kind map
    betting = next(r for r in rows if r["id"] == "live_betting_enabled")
    assert betting["locked"] and betting["betting"]
