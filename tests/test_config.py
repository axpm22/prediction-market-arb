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
        "watch_venues: [polymarket]", "watch_venues: [polymarket, sx, robinhood]")
    bad = tmp_path / "config.yaml"
    bad.write_text(text)
    with pytest.raises(ConfigError, match="watch_venues"):
        load(bad)


def test_watch_venues_empty_list_is_startup_error(tmp_path):
    # review of 7528050: the non-empty invariant had no test of its own
    text = SHIPPED.read_text().replace("watch_venues: [polymarket]", "watch_venues: []")
    bad = tmp_path / "config.yaml"
    bad.write_text(text)
    with pytest.raises(ConfigError, match="watch_venues"):
        load(bad)


def test_shipped_watch_venues_excludes_kalshi():
    # operator decision 2026-09-04: watch only names the PM universe for the
    # whale lane; nothing reads kalshi's ~1.3M rows/day. Add it back by
    # editing config.yaml.
    cfg = load(SHIPPED)
    assert cfg["watch_venues"] == ["polymarket"]   # sx dropped 2026-09-04 evening (only PM whales)


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
    # `holdings_keep` added by the 2026-09-04 retention amendment: a poll keeps
    # the positions in a tracked set plus one summary row, not the whole page.
    # `universe` (2026-09-05 fix, new default): the tracked set is the lane's
    # own current-cycle set, not the `markets` table (which had grown to
    # every market backfill ever mapped and kept a whale's whole history).
    assert w["exit"] == {"flip_ratio": Decimal("0.50"), "sold_fraction": Decimal("0.50"),
                         "min_score_lb": Decimal("-0.01"), "holdings_poll_secs": 300,
                         "holdings_calls_per_cycle": 10, "holdings_page_size": 500,
                         "holdings_max_age_polls": 3, "holdings_max_pages": 4,
                         "holdings_keep": "universe"}
    # retention amendment 2026-09-04: prints are kept platform-wide, in full
    # inside the universes and at or above the global floor outside them
    assert w["retention"] == {"scope": "global", "global_min_print_usd": 1000,
                              "raw_json": "universe"}
    # the same value as the discovery door on purpose: a print that can never
    # contribute to discovery has nothing to contribute at all
    assert w["retention"]["global_min_print_usd"] == w["discovery_min_notional_usd"]
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
    # L14 amendment 2026-09-04 (§7 item 1b): entry needs the whales still
    # HOLDING to agree too, between exit.flip_ratio (0.50) and fire.ratio (0.80)
    assert w["fire"]["holders_ratio"] == Decimal("0.6667")
    # L14 amendment 2026-09-04 (§7 V6 dominance): a LITERAL threshold, never
    # null — the V2/V3 null blocked the primary variant for a week. V6a
    # (fixer-D review, task 0(b)): corrected 0.0020 -> 0.0008, re-derived on
    # the venue volume_24h denominator the shipped code actually divides by
    # (.superpowers/sdd/v6-threshold-measurement.md).
    assert w["fire"]["min_points"]["V6"] == Decimal("0.0008")
    # L14 amendment 2026-09-04 (§7 V5 elite): both LITERAL, set from the
    # measurement before registration — top 5%/10% retain zero of the 56 fires,
    # top 40% has a cutoff below min_score_lb and degenerates into V1.
    assert w["fire"]["min_points"]["V5"] == 2
    assert w["fire"]["elite_pct"] == 20
    assert w["dominance"] == {"half_life_secs": 1800, "min_volume_usd": 5000}
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


def test_holders_ratio_must_sit_between_flip_ratio_and_fire_ratio(tmp_path):
    """L14 amendment 2026-09-04 (§7 item 1b): the entry holders gate sits
    strictly above exit.flip_ratio and at most at fire.ratio, so a signal can
    never fire directly into a flip — the band is the hysteresis."""
    for value in ("0.40", "0.90"):          # below flip_ratio, above fire.ratio
        bad = tmp_path / f"config-{value}.yaml"
        bad.write_text(SHIPPED.read_text().replace("holders_ratio: 0.6667,",
                                                   f"holders_ratio: {value},"))
        with pytest.raises(ConfigError, match="holders_ratio"):
            load(bad)


def test_holders_ratio_boundary_equal_to_fire_ratio_passes(tmp_path):
    """M7 (fixer D): the interval is closed at the top (`<=`) — holders_ratio
    exactly equal to fire.ratio is a fired signal that is not YET flip-eligible
    (flip needs strictly below fire.ratio's neighbour), so it must load."""
    text = SHIPPED.read_text().replace("holders_ratio: 0.6667,", "holders_ratio: 0.80,")
    ok = tmp_path / "config.yaml"
    ok.write_text(text)
    load(ok)                                # must not raise


def test_holders_ratio_boundary_equal_to_flip_ratio_fails(tmp_path):
    """M7 (fixer D): the interval is open at the bottom (`<`, strict) —
    holders_ratio exactly equal to exit.flip_ratio fires a signal that is
    already flip-eligible on the very next cycle, exactly what the band
    exists to prevent."""
    text = SHIPPED.read_text().replace("holders_ratio: 0.6667,", "holders_ratio: 0.50,")
    bad = tmp_path / "config.yaml"
    bad.write_text(text)
    with pytest.raises(ConfigError, match="holders_ratio"):
        load(bad)


def test_whale_dominance_min_volume_usd_invariant(tmp_path):
    """M6 (fixer D): `dominance.min_volume_usd` is the floor on V6's
    normalising denominator — zero or negative would let the ratio blow up
    (zero) or invert the sign of every V6 point (negative)."""
    for value in ("0", "-1000"):
        bad = tmp_path / f"config-{value}.yaml"
        bad.write_text(SHIPPED.read_text().replace("min_volume_usd: 5000",
                                                   f"min_volume_usd: {value}"))
        with pytest.raises(ConfigError, match="min_volume_usd"):
            load(bad)


def test_elite_pct_must_be_a_percentage(tmp_path):
    """L14 amendment 2026-09-04 (§7 V5): `0 < elite_pct <= 100`. At 0 nobody is
    ever elite and the variant is dead; above 100 the cutoff index is
    meaningless."""
    for value in ("0", "120"):
        bad = tmp_path / f"config-{value}.yaml"
        bad.write_text(SHIPPED.read_text().replace("elite_pct: 20,",
                                                   f"elite_pct: {value},"))
        with pytest.raises(ConfigError, match="elite_pct"):
            load(bad)


def test_retention_and_holdings_modes_must_be_ones_the_code_implements(tmp_path):
    """Retention amendment 2026-09-04. These three keys decide what is written
    to disk and what is dropped; a typo in any of them would silently pick the
    permissive branch (or the destructive one) with nothing logged, so the
    loader refuses a value the code does not implement."""
    for old, new, match in (("scope: global", "scope: universal", "retention.scope"),
                            ("raw_json: universe", "raw_json: yes", "retention.raw_json"),
                            ("holdings_keep: universe", "holdings_keep: tracked",
                             "holdings_keep")):
        bad = tmp_path / f"config-{new}.yaml".replace(" ", "-").replace(":", "")
        bad.write_text(SHIPPED.read_text().replace(old, new))
        with pytest.raises(ConfigError, match=re.escape(match)):
            load(bad)


def test_the_retention_comment_carries_arithmetic_that_adds_up():
    """The operator signed up for a disk bill, so the measurement behind the
    floor is part of the key's comment (rule 4) — and has to stay consistent:
    rows/day x KB/row = MB/day, x 365 = GB/year. Measured 2026-09-04 at 1.0 KB
    of table+index per compact row."""
    text = SHIPPED.read_text()
    block = text[text.index("  retention:"):text.index("  page_size: 10000")]
    kb = Decimal(re.search(r"([\d.]+) KB/compact row", block).group(1))
    rows = Decimal(re.search(r"~(\d+)k rows/day", block).group(1)) * 1000
    mb = Decimal(re.search(r"~(\d+) MB/day", block).group(1))
    gb = Decimal(re.search(r"~(\d+) GB/year", block).group(1))
    assert kb == Decimal("1.0")
    assert round(rows * kb / 1000) == mb
    assert round(mb * 365 / 1000) == gb


def test_the_holdings_keep_comment_carries_the_before_and_after_row_rate():
    """Addendum 2026-09-04: `holdings` was the bigger storage hog of the two —
    ~39k rows in ~2.5 h over 20 wallets. The key's comment states that measured
    before-rate and the after-rate the change bounds it to (one summary row per
    wallet poll), so the next operator can see what was traded for what."""
    text = SHIPPED.read_text()
    block = text[text.index("  exit:"):text.index("  rank:")]
    before = Decimal(re.search(r"~(\d+)k rows/day before", block).group(1)) * 1000
    wallets = Decimal(re.search(r"over (\d+) wallets", block).group(1))
    summaries = Decimal(re.search(r"~([\d,]+) summary rows/day",
                                  block).group(1).replace(",", ""))
    cadence = Decimal(re.search(r"holdings_poll_secs: (\d+)", text).group(1))
    assert round(Decimal(39000) / Decimal("2.5") * 24 / 1000) * 1000 == before
    assert wallets * (Decimal(86400) / cadence) == summaries


def test_whale_size_stake_invariant(tmp_path):
    text = SHIPPED.read_text().replace("min_stake_usd: 0.10", "min_stake_usd: 5.00")
    bad = tmp_path / "config.yaml"
    bad.write_text(text)
    with pytest.raises(ConfigError, match="min_stake_usd"):
        load(bad)


def test_whale_size_kelly_fraction_is_the_registered_quarter():
    """§8 amendment 2026-09-05: quarter Kelly on a lower-bound edge."""
    assert load(SHIPPED)["whale"]["size"]["kelly_fraction"] == Decimal("0.25")


@pytest.mark.parametrize("value", ["0", "-0.25", "1.50"])
def test_whale_size_kelly_fraction_invariant(tmp_path, value):
    text = SHIPPED.read_text().replace("kelly_fraction: 0.25 }",
                                       f"kelly_fraction: {value} }}")
    bad = tmp_path / "config.yaml"
    bad.write_text(text)
    with pytest.raises(ConfigError, match="kelly_fraction"):
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


def test_shipped_dashboard_cache_secs():
    # live 2026-09-04: /api/whale recompute pileup fix. TTL == refresh_secs —
    # a read-only panel may lag the tape by one refresh, never more.
    cfg = load(SHIPPED)
    assert cfg["dashboard"]["cache_secs"] == 300   # 60 -> 300 on 2026-09-05: four heavy endpoints per refresh saturated the CPU the live lane shares
    assert cfg["dashboard"]["cache_secs"] == cfg["dashboard"]["refresh_secs"]


def test_shipped_dashboard_cache_max_entries():
    # review of 9e5951a: /api/whale/traders folds the unvalidated `wallet=`
    # query param into the cache key, so an unbounded cache grows one
    # permanent entry+lock per distinct wallet ever queried. Bound it.
    cfg = load(SHIPPED)
    assert cfg["dashboard"]["cache_max_entries"] == 256


def test_shipped_dashboard_mode_is_whale():
    """Operator instruction 2026-09-04: the whale lane is the face of the
    instrument for now — dashboard.py/report.py/cmd_status all key off this
    one flag. 'full' restores today's arb-first layout."""
    cfg = load(SHIPPED)
    assert cfg["dashboard"]["mode"] == "whale"


def test_dashboard_mode_rejects_an_unimplemented_value(tmp_path):
    text = SHIPPED.read_text().replace("mode: whale", "mode: bogus")
    bad = tmp_path / "config.yaml"
    bad.write_text(text)
    with pytest.raises(ConfigError, match="dashboard.mode"):
        load(bad)


def test_shipped_supervisor_lanes_are_whale_only():
    """Operator decision 2026-09-04: run only the Polymarket whale lane. watch
    stays on because the whale lane's universe comes from watch's Polymarket
    quotes; live/negrisk/political/mapped/verify are off until re-enabled."""
    cfg = load(SHIPPED)
    on = {name for name, enabled in cfg["supervisor"]["processes"].items() if enabled}
    assert on == {"watch", "whale", "whale-backfill"}


# --- L14 non-sports roots (I3, fix pass) ------------------------------------

TAGS = "tags: { politics: politics, geopolitics: geopolitics, crypto: crypto }"


def test_a_root_that_is_not_a_polymarket_fee_category_is_a_boot_error(tmp_path):
    """The amendment's own "single most likely silent bug": a root IS the fee
    category that prices the leg, so an unknown one is priced at the default
    `polymarket.fee_rate` with nothing logged."""
    bad = tmp_path / "config.yaml"
    bad.write_text(SHIPPED.read_text().replace(
        TAGS, "tags: { politics: elections, geopolitics: geopolitics, crypto: crypto }"))
    with pytest.raises(ConfigError, match="nonsports.tags"):
        load(bad)


def test_sports_as_a_nonsports_root_is_a_boot_error(tmp_path):
    """`sports` as a value would pool a politics record with the sports one and
    defeat the no-transfer decision (operator, 2026-09-03 item 4)."""
    bad = tmp_path / "config.yaml"
    bad.write_text(SHIPPED.read_text().replace(
        TAGS, "tags: { politics: sports, geopolitics: geopolitics, crypto: crypto }"))
    with pytest.raises(ConfigError, match="nonsports.tags"):
        load(bad)


def test_the_whale_lane_ships_its_own_nonsports_horizon(tmp_path):
    """H (fix pass): pre-plumbed at today's shared `nonsports_max_days`, so the
    key exists for the operator to raise without moving every other lane."""
    cfg = load(SHIPPED)
    assert cfg["whale"]["nonsports"]["max_days"] == cfg["nonsports_max_days"] == 7
