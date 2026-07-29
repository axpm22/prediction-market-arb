"""Typed config loader — THE only default/validation site (SPEC §16 Phase 1, I9).

Strict both ways: unknown keys and missing keys are startup errors. All YAML
floats parse directly to Decimal (rule 5). Loader enforces the dry-run-patch
invariants A1/A2 (SPEC §8.4) so the config pair can never drift apart.
"""
from __future__ import annotations

import datetime
from decimal import Decimal
from pathlib import Path

import yaml


class ConfigError(Exception):
    pass


class _DecimalLoader(yaml.SafeLoader):
    pass


_DecimalLoader.add_constructor(
    "tag:yaml.org,2002:float",
    lambda loader, node: Decimal(loader.construct_scalar(node)),
)

# Leaf type specs
NUM = (int, Decimal)          # any number (ints allowed where YAML omits the dot)
BOOL = bool
STR = str
DATE = datetime.date
LIST = list


def MAP(valtype):
    """Open-keyed map (per-sport / per-venue tables)."""
    return ("__map__", valtype)


SCHEMA = {
    "timezone": STR, "bankroll_usd": NUM, "max_stake_pct": NUM, "ev_threshold": NUM,
    "divergence_threshold": NUM, "push_pricing": BOOL, "arb_stale_warn": NUM,
    "max_leg_skew_secs": NUM, "max_start_drift_mins": NUM, "match_suspect_prob_diff": NUM,
    "schedule_match_window_hours": NUM, "settle_doubleheader_window_hours": NUM,
    "capital_charge_annual": NUM,
    "sport_typical_duration_hours": MAP(NUM), "venue_settlement_lag_hours": MAP(NUM),
    "reconcile_give_up_days": NUM,
    "min_books_for_consensus": NUM, "min_books_for_consensus_totals": NUM,
    "consensus_outlier_points": NUM, "devig_method": STR, "sharp_books": LIST,
    "weight_sharp_books": BOOL,
    "consensus_anchor": {"enabled": BOOL, "book": STR, "region": STR, "markets": STR},
    "min_executable_usd": NUM,
    "micro": {"enabled": BOOL, "min_depth_usd": NUM, "max_stake_usd": NUM, "e_min": NUM,
              "e_min_thin": NUM, "assume_thin_venues": LIST, "max_event_exposure_usd": NUM,
              "max_venue_exposure_usd": NUM},
    "max_days_to_settlement": NUM, "nonsports_max_days": NUM, "sports": LIST,
    "unreliable_start_venues": MAP(LIST), "activity_lookahead_hours": NUM,
    "consensus_sports": LIST, "odds_api_sport_aliases": MAP(STR),
    "odds_api_markets": MAP(STR), "scan_breaks": BOOL, "break_windows": MAP(LIST),
    "watch_interval_secs": NUM, "consensus_enabled": BOOL, "ev_enabled": BOOL,
    "consensus_max_age_hours": NUM, "consensus_pregame_max_age_hours": NUM,
    "ev": {"e_min": NUM, "clv_window_mins": NUM, "kelly_fraction": NUM,
           "edge_suspect_above": NUM, "max_edge": NUM, "require_exact_match": LIST,
           "enforce_depth_floor": BOOL, "reject_one_sided_books": BOOL},
    "regrade": {"match_tolerance_secs": NUM, "bootstrap_n": NUM,
                "bootstrap_seed": NUM, "min_events": NUM},
    "scan_times": LIST, "daily_report_time": STR, "settle_time": STR,
    "archive_time": STR, "archive_after_days": NUM,
    "settle_days_from": NUM, "settle_tripwire_secs": NUM,
    "tracker": {"terminal_mute_grace_mins": NUM, "max_reappearances": NUM,
                "dead_checks": NUM},
    "mlbstats_enabled": BOOL, "auto_fill": BOOL, "trusted_pairs": LIST,
    "fuzzy_match_threshold": NUM, "fee_stale_days": NUM,
    "kalshi": {"fee_rate": NUM, "maker_fee_rate": NUM, "source_url": STR, "confirmed_on": DATE},
    "polymarket": {"sports_fee_rate": NUM, "crypto_fee_rate": NUM, "politics_fee_rate": NUM,
                   "econ_weather_fee_rate": NUM, "geopolitics_fee_rate": NUM, "fee_rate": NUM,
                   "source_url": STR, "confirmed_on": DATE},
    "sx": {"fee_rate": NUM, "win_fee_rate": NUM, "source_url": STR, "confirmed_on": DATE},
    "execution": {"taker_price_buffer": NUM, "slippage_model": STR, "fillable_fraction": NUM,
                  "fixed_cost_per_fill_usd": MAP(NUM), "arb_size_by_ladder": BOOL,
                  "arb_size_safety_frac": NUM, "min_ladder_fraction": NUM,
                  "max_actionable_skew_secs": NUM, "venue_fetch_timeout_secs": NUM,
                  "venue_deadline_secs": NUM,
                  "venue_max_retries": NUM, "tls_block_cache_secs": NUM,
                  "min_adj_edge_points": NUM,
                  "line_velocity_points_per_sec": MAP(NUM),
                  "doubleheader_cluster_tol_secs": NUM},
    "odds_api": {"region": STR, "warn_below_credits": NUM, "abort_below_credits": NUM},
    "negrisk": {"enabled": BOOL, "min_edge": NUM, "scan_no_side": BOOL, "kalshi_events": BOOL,
                "kalshi_event_checks": NUM,
                "require_exhaustive_exclusive": BOOL,
                "exhaustive_proof": {"require_venue_flag": BOOL, "mids_sum_min": NUM,
                                     "mids_sum_max": NUM, "max_half_spread_sum": NUM,
                                     "require_two_sided_all_outcomes": BOOL},
                "max_outcomes": NUM, "scan_limit": NUM, "scan_pages": NUM,
                "min_executable_usd": NUM},
    "btc_arb": {"enabled": BOOL, "window_match_secs": NUM, "max_window_minutes": NUM,
                "trusted": BOOL},
    "parlay": {"enabled": BOOL, "divergence_threshold": NUM, "min_liquidity_usd": NUM,
               "sx_win_fee_rate": NUM},
    "ai_matcher": {"enabled": BOOL, "model": STR, "min_confidence": NUM,
                   "max_new_pairs_per_day": NUM, "max_rules_chars": NUM, "paper_fill": BOOL,
                   "paper_stake_usd": NUM},
    "nonsports": {"watch_interval_secs": NUM, "gamma_scan_pages": NUM,
                  "match_date_window_days": NUM, "max_candidates": NUM,
                  "max_series_fetch": NUM, "series_fetch_workers": NUM,
                  "match_deadline_secs": NUM,
                  "candidate_threshold": NUM,
                  "kalshi_categories": LIST, "polymarket_tags": LIST},
    "verify": {"poll_secs": NUM, "max_bet_age_secs": NUM},
    "hot": {"poll_secs": NUM, "check_interval_secs": NUM, "watch_duration_secs": NUM,
            "dead_checks": NUM, "max_watchers": NUM},
    "scoring": {"score_min_n": NUM, "block_below": NUM, "recent_window": NUM,
                "calibration_gate": BOOL, "ece_estimator": STR, "ece_max": NUM,
                "ece_bins": NUM},
    "sizing": {"kelly_fraction": NUM, "max_kelly_fraction_of_bankroll": NUM},
    "validation": {"ev_filter": {"rule_id": STR, "kind": STR, "min_cost": NUM, "max_cost": NUM,
                                 "max_flagged_edge": NUM, "target_n": NUM, "halfway_n": NUM,
                                 "kill_consecutive_losses": NUM}},
    "supervisor": {"processes": MAP(BOOL), "commands": MAP(STR),
                   "max_child_runtime_secs": MAP(NUM),
                   "schedule_catchup_grace_mins": NUM, "stagger_secs": NUM,
                   "shape_time": STR},
    "data_stale_alarm_secs": NUM, "heartbeat_stale_polls": NUM,
    "clock_skew_alarm_secs": NUM, "log_retention_days": NUM,
    "db_backup": {"time": STR, "dest": STR, "keep_last": NUM},
    "canary": {"enabled": BOOL, "day_of_month": NUM, "time": STR},
    "dashboard": {"host": STR, "port": NUM},
    "tape_check": {"enabled": BOOL, "window_secs": NUM},
    "zombie_spread_min": NUM, "max_lead_hours": NUM,
    "risk_free_rate": {"value": NUM, "source_url": STR, "confirmed_on": DATE},
    "deployment": {"annualized_hurdle": NUM, "xspread_autobook": BOOL,
                   "auto_book_min_net_edge": STR, "leg_failure_suspend_rate": NUM,
                   "fok_window_secs": NUM},
    "slippage": {"max_edge_fraction": NUM, "depth_divisor": NUM},
    "mc_sim": {"n_bets": NUM, "block": STR, "ruin_threshold": NUM, "max_ruin_prob": NUM,
               "min_history_bets": NUM},
    "mismatch_pricing": {"lgm": NUM, "rule_of_three": BOOL},
    "caps": {"per_event_frac": NUM, "per_sport_frac": NUM, "per_day_totals_frac": NUM},
    "kind_taxonomy": {"cross_venue": STR, "same_venue_locked": STR},
    "live_lane": {"enabled": BOOL, "cadence_secs": NUM, "shortlist_max_events": NUM,
                  "max_game_hours": MAP(NUM), "max_actionable_skew_secs": NUM,
                  "line_velocity_points_per_sec": MAP(NUM),
                  "kill": {"max_days": NUM, "min_flags": NUM,
                           "confirm_rate_floor": NUM}},
    "leadlag": {"min_move_points": NUM, "match_window_secs": NUM,
                "tie_epsilon_secs": NUM, "max_lag_polls": NUM,
                "min_matched_moves": NUM, "min_events": NUM},
    "bench": {"db": STR, "request_pace_secs": NUM, "max_event_pages": NUM,
              "max_market_pages": NUM, "max_series_per_category": NUM,
              "max_candle_markets": NUM, "max_candle_per_series": NUM, "min_volume_fp": NUM, "max_trade_markets": NUM, "tape_db": STR,
              "large_print_percentile": NUM, "markout_trades_ahead": NUM,
              "candle_period_mins": NUM, "max_candle_periods": NUM, "horizon_hours": NUM,
              "price_bands": LIST, "primary_band": LIST, "tail_band": NUM,
              "max_spread": NUM,
              "n_min_units": NUM, "null_band": NUM, "ci_z": NUM,
              "anj_probe_region": STR, "anj_probe_markets": STR,
              "anj_probe_file": STR, "anj_probe_sport": STR,
              "credits_per_month": MAP(NUM)},
    "scale_in": {"enabled": BOOL, "min_edge_widening": NUM, "require_edge_moved": BOOL,
                 "max_tranches": NUM, "max_position_frac": NUM},
    "exit": {"enabled": BOOL, "policies": LIST, "capture_fraction": NUM,
             "trail_fraction": NUM, "min_peak_edge_fraction": NUM, "exit_price_basis": STR,
             "promote_requires": {"min_n": NUM, "ci_excludes_zero": BOOL,
                                  "replication_month": BOOL}},
}


def _validate(data, spec, path=""):
    if isinstance(spec, tuple) and spec and spec[0] == "__map__":
        if not isinstance(data, dict):
            raise ConfigError(f"{path}: expected map, got {type(data).__name__}")
        for k, v in data.items():
            _validate(v, spec[1], f"{path}.{k}")
        return
    if isinstance(spec, dict):
        if not isinstance(data, dict):
            raise ConfigError(f"{path}: expected map, got {type(data).__name__}")
        unknown = set(data) - set(spec)
        if unknown:
            raise ConfigError(f"unknown config key(s) at {path or 'top level'}: {sorted(unknown)}")
        missing = set(spec) - set(data)
        if missing:
            raise ConfigError(f"missing config key(s) at {path or 'top level'}: {sorted(missing)}")
        for k, sub in spec.items():
            _validate(data[k], sub, f"{path}.{k}" if path else k)
        return
    # leaf: spec is a type or tuple of types; bool is an int subclass — reject bool for NUM
    if spec is not BOOL and isinstance(data, bool):
        raise ConfigError(f"{path}: expected {spec}, got bool")
    if not isinstance(data, spec):
        raise ConfigError(f"{path}: expected {spec}, got {type(data).__name__} ({data!r})")


def _check_invariants(cfg):
    proof = cfg["negrisk"]["exhaustive_proof"]
    slack = Decimal(1) - Decimal(proof["mids_sum_min"])
    p_tail_bound = slack / Decimal(proof["mids_sum_min"])
    if not Decimal(cfg["negrisk"]["min_edge"]) > p_tail_bound:
        raise ConfigError(
            f"negrisk.min_edge ({cfg['negrisk']['min_edge']}) must exceed "
            f"(1-mids_sum_min)/mids_sum_min ({p_tail_bound}) — dry-run patch A1: the proof "
            "tolerance would admit worst-case-negative baskets into the headline")
    if Decimal(proof["max_half_spread_sum"]) != slack:
        raise ConfigError(
            f"negrisk.exhaustive_proof.max_half_spread_sum ({proof['max_half_spread_sum']}) "
            f"must equal the window slack 1-mids_sum_min ({slack}) — dry-run patch A2")


def load(path: str | Path = "config.yaml") -> dict:
    raw = Path(path).read_text()
    data = yaml.load(raw, Loader=_DecimalLoader)
    _validate(data, SCHEMA)
    _check_invariants(data)
    return data
