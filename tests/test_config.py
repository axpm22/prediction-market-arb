"""Config loader: typed, strict both ways, Decimal numbers, loader-enforced invariants."""
from decimal import Decimal
from pathlib import Path

import pytest

from arb.config import ConfigError, load

ROOT = Path(__file__).resolve().parent.parent
SHIPPED = ROOT / "config.yaml"


def test_shipped_config_loads():
    cfg = load(SHIPPED)
    assert cfg["timezone"] == "America/New_York"
    assert cfg["ev_enabled"] is False
    assert cfg["trusted_pairs"] == []


def test_fractional_numbers_are_decimal():
    cfg = load(SHIPPED)
    assert cfg["negrisk"]["min_edge"] == Decimal("0.006")
    assert isinstance(cfg["negrisk"]["min_edge"], Decimal)
    assert isinstance(cfg["kalshi"]["fee_rate"], Decimal)
    assert cfg["kalshi"]["fee_rate"] == Decimal("0.07")


def test_unknown_key_is_startup_error(tmp_path):
    bad = tmp_path / "config.yaml"
    bad.write_text(SHIPPED.read_text() + "\nnot_a_real_key: 1\n")
    with pytest.raises(ConfigError, match="not_a_real_key"):
        load(bad)


def test_unknown_nested_key_is_startup_error(tmp_path):
    text = SHIPPED.read_text().replace("min_edge: 0.006", "min_edge: 0.006\n  bogus_sub: 1")
    bad = tmp_path / "config.yaml"
    bad.write_text(text)
    with pytest.raises(ConfigError, match="bogus_sub"):
        load(bad)


def test_missing_key_is_startup_error(tmp_path):
    text = SHIPPED.read_text().replace("watch_interval_secs: 60\n", "")
    bad = tmp_path / "config.yaml"
    bad.write_text(text)
    with pytest.raises(ConfigError, match="watch_interval_secs"):
        load(bad)


def test_a1_coupling_invariant_enforced(tmp_path):
    # min_edge must exceed (1 - mids_sum_min)/mids_sum_min; 0.98 window vs 0.006 edge violates
    text = SHIPPED.read_text().replace("mids_sum_min: 0.995", "mids_sum_min: 0.98")
    bad = tmp_path / "config.yaml"
    bad.write_text(text)
    with pytest.raises(ConfigError, match="min_edge"):
        load(bad)


def test_a2_half_spread_bound_tied_to_window_slack(tmp_path):
    text = SHIPPED.read_text().replace("max_half_spread_sum: 0.005", "max_half_spread_sum: 0.01")
    bad = tmp_path / "config.yaml"
    bad.write_text(text)
    with pytest.raises(ConfigError, match="max_half_spread_sum"):
        load(bad)


def test_whale_config_loads_with_spec_values():
    # literal spec §14 values, including V2/V3 null (NUM_OPT leaf) during the calibration window
    cfg = load(SHIPPED)
    w = cfg["whale"]
    assert w["enabled"] is True
    assert w["tape_db"] == "data/whale_tape.sqlite"
    assert w["poll_secs"] == 60
    assert w["collector_mode"] == "global"
    assert w["page_size"] == 10000
    assert w["taker_only"] is True
    assert w["backfill"] == {"wallets_per_cycle": 5, "pages_per_wallet": 40, "page_size": 50}
    assert w["fire"]["min_points"]["V1"] == 3
    assert w["fire"]["min_points"]["V2"] is None
    assert w["fire"]["min_points"]["V3"] is None
    assert w["fire"]["calibration_days"] == 14
    assert w["fire"]["calibration_min_whales"] == 10
    assert w["primary_variant"] == "V2"


def test_whale_unknown_key_is_startup_error(tmp_path):
    text = SHIPPED.read_text().replace(
        "tape_db: data/whale_tape.sqlite",
        "tape_db: data/whale_tape.sqlite\n  bogus_whale_key: 1")
    bad = tmp_path / "config.yaml"
    bad.write_text(text)
    with pytest.raises(ConfigError, match="bogus_whale_key"):
        load(bad)


def test_whale_fire_ratio_invariant(tmp_path):
    text = SHIPPED.read_text().replace("ratio: 0.80,", "ratio: 0.40,")
    bad = tmp_path / "config.yaml"
    bad.write_text(text)
    with pytest.raises(ConfigError, match="whale.fire.ratio"):
        load(bad)


def test_whale_exit_flip_ratio_invariant(tmp_path):
    text = SHIPPED.read_text().replace("flip_ratio: 0.50,", "flip_ratio: 0.90,")
    bad = tmp_path / "config.yaml"
    bad.write_text(text)
    with pytest.raises(ConfigError, match="flip_ratio"):
        load(bad)


def test_whale_size_stake_invariant(tmp_path):
    text = SHIPPED.read_text().replace("min_stake_usd: 0.10", "min_stake_usd: 5.00")
    bad = tmp_path / "config.yaml"
    bad.write_text(text)
    with pytest.raises(ConfigError, match="min_stake_usd"):
        load(bad)


def test_whale_poll_secs_invariant(tmp_path):
    text = SHIPPED.read_text().replace(
        "poll_secs: 60                       # collector cycle interval; invariant: poll_secs >= 30",
        "poll_secs: 10                       # collector cycle interval; invariant: poll_secs >= 30")
    bad = tmp_path / "config.yaml"
    bad.write_text(text)
    with pytest.raises(ConfigError, match="whale.poll_secs"):
        load(bad)


def test_dead_config_blocks_removed():
    cfg = load(SHIPPED)
    assert "tape_check" not in cfg
    assert "exit" not in cfg
    assert "flip_ratio" not in cfg["whale"]  # exit lives nested under whale now
