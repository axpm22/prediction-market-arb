"""Config loader: typed, strict both ways, Decimal numbers, loader-enforced invariants."""
import re
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
    # ^-anchored regex hits only the unindented top-level line (and its trailing
    # comment) — nonsports.watch_interval_secs is indented, shares the same
    # value (300) since 2026-09-04, and must not be touched
    text = re.sub(r"(?m)^watch_interval_secs: 300.*\n", "", SHIPPED.read_text())
    bad = tmp_path / "config.yaml"
    bad.write_text(text)
    with pytest.raises(ConfigError, match="watch_interval_secs"):
        load(bad)


def test_watch_venues_bad_name_is_startup_error(tmp_path):
    text = SHIPPED.read_text().replace(
        "watch_venues: [polymarket, sx]", "watch_venues: [polymarket, sx, robinhood]")
    bad = tmp_path / "config.yaml"
    bad.write_text(text)
    with pytest.raises(ConfigError, match="watch_venues"):
        load(bad)


def test_watch_venues_empty_list_is_startup_error(tmp_path):
    # review of 7528050: the non-empty invariant had no test of its own
    text = SHIPPED.read_text().replace("watch_venues: [polymarket, sx]", "watch_venues: []")
    bad = tmp_path / "config.yaml"
    bad.write_text(text)
    with pytest.raises(ConfigError, match="watch_venues"):
        load(bad)


def test_shipped_watch_venues_excludes_kalshi():
    # operator decision 2026-09-04: watch only names the PM universe for the
    # whale lane; nothing reads kalshi's ~1.3M rows/day. Add it back by
    # editing config.yaml.
    cfg = load(SHIPPED)
    assert cfg["watch_venues"] == ["polymarket", "sx"]


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
    # L14 amendment 2026-09-04: true -> false, so a whale exiting through a
    # RESTING order is recorded as its own SELL leg (maker legs carry the
    # maker's wallet and side — live probe, data/verified_facts.md).
    assert w["taker_only"] is False
    # item C of the same amendment: the authoritative holdings check reads
    # data-api `positions?user=` for the wallets behind an open bet.
    assert w["exit"] == {"flip_ratio": Decimal("0.50"), "sold_fraction": Decimal("0.50"),
                         "min_score_lb": Decimal("-0.01"), "holdings_poll_secs": 300,
                         "holdings_calls_per_cycle": 10, "holdings_page_size": 500,
                         "holdings_max_age_polls": 3}
    assert w["backfill"] == {"wallets_per_cycle": 3, "pages_per_wallet": 40,
                             "page_size": 50, "cadence_secs": 600,
                             # spec §14, amended 2026-09-04: the resolution
                             # sweep's per-cycle bound (final review C1),
                             # raised 40 -> 100 once the sweep always runs
                             # first (whale-backfill fix pass); map_per_cycle
                             # bounds the now-separate mapping step, raised
                             # 40 -> 200 (V4 review, same day), then LOWERED
                             # 200 -> 100 (mapper fix pass 2, same day): F0's
                             # closed=true retry can double mapping's Gamma
                             # calls, so 200 pushed the child's worst case to
                             # exactly its 900s cap with no margin; worst case
                             # at 100: leaderboard 80 + sweep 50 + wallets 120
                             # + map 100 Gamma calls ~= 11 min, under the 900s
                             # cap with ~200s margin. Raised AGAIN, 100 -> 1000
                             # each (legacy re-map, 2026-09-04): GAMMA_IDS_PER_
                             # CALL 2 -> 20 (verified live) means the SAME
                             # 350-call worst case (leaderboard 80 + sweep
                             # ceil(1000/20)=50 + wallets 120 + map 2 x
                             # ceil(1000/20)=100) now processes 10x more ids a
                             # cycle — needed to drain the 12,091 legacy
                             # slug-NULL `markets` rows REVISIT_SQL can newly
                             # reach
                             "resolve_per_cycle": 1000, "map_per_cycle": 1000}
    assert w["universe_poll_lookback"] == 5
    assert w["fire"]["min_points"]["V1"] == 3
    assert w["fire"]["min_points"]["V2"] is None
    assert w["fire"]["min_points"]["V3"] is None
    # L14 amendment 2026-09-04: V4 ranked is literal like V1 — the calibration
    # guard only ever applies to a NULL threshold, so V4 fires from day one.
    assert w["fire"]["min_points"]["V4"] == 3
    assert w["leaderboard"]["time_periods"] == ["DAY", "WEEK", "MONTH", "ALL"]
    assert w["leaderboard"]["pages"] == 20        # offset cap 1000 / page size 50
    # esports prefixes stay unmapped until an `esports` hierarchy root exists:
    # `root_of` would otherwise pool CS2 skill into the sports root
    assert not {"cs2", "lol", "dota2", "val"} & set(w["slug_sport_prefixes"])
    assert w["fire"]["calibration_days"] == 3   # 14 -> 3 amended 2026-09-04 (provisional calibration)
    assert w["fire"]["calibration_min_whales"] == 10
    assert w["primary_variant"] == "V2"
    # read-time trader ranking (2026-09-04): min_n is a DISPLAY floor, never a
    # scoring floor — a profile below it is still computed, just not ranked
    assert w["rank"] == {"min_n": 5, "top": 15}


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
    with pytest.raises(ConfigError, match="must exceed 0.5"):
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
    text = SHIPPED.read_text().replace("  poll_secs: 60", "  poll_secs: 10")
    bad = tmp_path / "config.yaml"
    bad.write_text(text)
    with pytest.raises(ConfigError, match="whale.poll_secs"):
        load(bad)


def test_dead_config_blocks_removed():
    cfg = load(SHIPPED)
    assert "tape_check" not in cfg
    assert "exit" not in cfg


def test_shipped_supervisor_lanes_are_whale_only():
    """Operator decision 2026-09-04: run only the Polymarket whale lane. watch
    stays on because the whale lane's universe comes from watch's Polymarket
    quotes; live/negrisk/political/mapped/verify are off until re-enabled."""
    cfg = load(SHIPPED)
    on = {name for name, enabled in cfg["supervisor"]["processes"].items() if enabled}
    assert on == {"watch", "whale", "whale-backfill"}
