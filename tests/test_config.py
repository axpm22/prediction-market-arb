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
