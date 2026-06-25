from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - dependency is pinned for real installs.
    yaml = None

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:  # pragma: no cover
    load_dotenv = None


DEFAULT_CONFIG_PATH = Path("config.yaml")
# Toggles from the dashboard control plane are written here only; config.yaml (the canonical
# source of truth, CLAUDE.md rule #4) is never mutated. This overlay is deep-merged on top of
# it at load time, so any nested key can be overridden and reverting is just deleting the key.
LOCAL_OVERLAY_NAME = "config.local.yaml"


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge overlay onto base. Dicts merge key-by-key; scalars/lists replace."""
    out = dict(base)
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def overlay_path(base: Path = DEFAULT_CONFIG_PATH) -> Path:
    return base.with_name(LOCAL_OVERLAY_NAME)


def load_overlay(base: Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    path = overlay_path(base)
    if yaml is None or not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def effective_raw(base: Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    """config.yaml deep-merged with config.local.yaml — the values load_config actually uses."""
    if yaml is None or not base.exists():
        return {}
    raw = yaml.safe_load(base.read_text()) or {}
    return _deep_merge(raw, load_overlay(base))


@dataclass(frozen=True)
class AppConfig:
    timezone: str
    bankroll_usd: Decimal
    max_stake_pct: Decimal
    ev_threshold: Decimal
    divergence_threshold: Decimal
    arb_stale_warn: Decimal
    max_leg_skew_secs: int
    max_start_drift_mins: int
    match_suspect_prob_diff: Decimal
    capital_charge_annual: Decimal
    sport_duration_hours: dict
    venue_settlement_lag_hours: dict
    min_books_for_consensus: int
    min_books_for_consensus_totals: int
    devig_method: str
    consensus_outlier_points: Decimal | None
    sharp_books: frozenset[str]
    weight_sharp_books: bool
    sharp_book_weight: int
    min_liquidity_usd: Decimal
    min_executable_usd: Decimal
    micro_enabled: bool
    micro_min_depth_usd: Decimal
    micro_max_stake_usd: Decimal
    micro_e_min: Decimal
    max_days_to_settlement: int
    nonsports_max_days: int
    scan_times: list[str]
    daily_report_time: str
    settle_time: str
    settle_days_from: int
    settle_health_overdue_max: int
    settle_grace_hours: int
    sports: list[str]
    consensus_sports: list[str]
    scan_breaks: bool
    break_windows: dict[str, list[int]]
    watch_interval_secs: int
    consensus_enabled: bool
    consensus_max_age_hours: int
    consensus_pregame_max_age_hours: int
    ev_e_min: Decimal
    ev_kelly_fraction: Decimal
    ev_edge_suspect_above: Decimal
    ev_enforce_depth_floor: bool
    ev_reject_one_sided: bool
    ev_edge_decay_per_hour: Decimal
    ev_dispersion_gate: bool
    ev_edge_se_min: Decimal
    ev_dispersion_kelly_haircut: Decimal
    ev_min_expected_profit_usd: Decimal
    ev_dispersion_book_correlation: Decimal
    ev_shrink_prior: Decimal
    ev_shrink_prior_strength: Decimal
    ev_push_pricing: bool
    ev_settlement_lag_hours: Decimal
    ev_correlation_rho: Decimal
    ev_correlation_group_cap_fraction: Decimal
    ev_require_move_time: bool
    auto_fill: bool
    trusted_pairs: set[str]
    fuzzy_match_threshold: int
    kalshi_fee_rate: Decimal
    polymarket_fee_rate: Decimal
    polymarket_sports_fee_rate: Decimal
    polymarket_crypto_fee_rate: Decimal
    sx_fee_rate: Decimal | None
    sx_win_fee_rate: Decimal
    taker_price_buffer: Decimal
    taker_slippage_model: str
    slippage_ref_stake_usd: Decimal
    arb_size_by_ladder: bool
    arb_fillable_fraction: Decimal
    arb_size_safety_frac: Decimal
    arb_min_ladder_fraction: Decimal
    arb_max_actionable_skew_secs: int
    arb_min_adj_edge_points: Decimal
    arb_line_velocity: dict
    arb_doubleheader_cluster_tol_secs: int
    odds_api_region: str
    odds_api_warn_below_credits: int
    odds_api_abort_below_credits: int
    parlay_enabled: bool
    parlay_divergence_threshold: Decimal
    sizing_kelly_fraction: Decimal
    sizing_max_fraction: Decimal
    scoring: dict
    arb_sizing: dict
    negrisk_min_edge: Decimal
    negrisk_require_exhaustive: bool
    negrisk_max_outcomes: int
    negrisk_scan_limit: int
    negrisk_scan_pages: int
    negrisk_min_executable_usd: Decimal
    btc_window_match_secs: int
    btc_max_window_minutes: int
    btc_trusted: bool
    flb: dict
    forecast: dict
    validation: dict
    live_baseball_ev: dict
    per_venue_caps: dict
    odds_api_key: str | None
    stale_fee_venues: frozenset[str] = frozenset()
    database_path: Path = Path("data/arb.sqlite")


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _load_env() -> None:
    if load_dotenv is not None:
        load_dotenv()
        return
    env_path = Path(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        if not line or line.strip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _warn_stale_fees(raw: dict[str, Any], venues: dict[str, dict[str, Any]]) -> set[str]:
    # Stale fees warn (never halt the scanner — silent downtime is worse) but the venue is
    # returned as stale so detection can quarantine its bets: a wrong/stale fee silently
    # shifts every edge, and fees are first-order at sub-4% edges (pass3#6 / weakness#8).
    stale_days = int(raw.get("fee_stale_days", 30))
    today = datetime.now(timezone.utc).date()
    stale: set[str] = set()
    for venue, block in venues.items():
        confirmed = block.get("confirmed_on")
        if confirmed is None:
            print(f"config: WARNING {venue} fee_rate has no confirmed_on date.")
            stale.add(venue)
            continue
        confirmed = confirmed if isinstance(confirmed, date) else date.fromisoformat(str(confirmed))
        age = (today - confirmed).days
        if age > stale_days:
            print(f"config: WARNING {venue} fee confirmed {age} days ago (> {stale_days}); re-check {block.get('source_url', 'source')}.")
            stale.add(venue)
    return stale


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> AppConfig:
    _load_env()
    if not path.exists():
        raise FileNotFoundError(f"Missing config file: {path}")
    if yaml is None:
        raise RuntimeError("pyyaml is required to load config.yaml. Run `pip install -r requirements.txt`.")
    raw = yaml.safe_load(path.read_text()) or {}
    # Deep-merge the dashboard overlay (config.local.yaml) on top of the canonical config.
    overlay = load_overlay(path)
    if overlay:
        raw = _deep_merge(raw, overlay)
    odds_api = raw.get("odds_api", {})
    ev = raw.get("ev", {})
    kalshi = raw.get("kalshi", {})
    polymarket = raw.get("polymarket", {})
    sx = raw.get("sx", {})
    stale_fee_venues = _warn_stale_fees(raw, {"kalshi": kalshi, "polymarket": polymarket, "sx": sx})
    parlay = raw.get("parlay", {})
    sizing = raw.get("sizing", {})
    micro = raw.get("micro", {})
    negrisk = raw.get("negrisk", {})
    btc = raw.get("btc_arb", {})
    database_path = Path(raw.get("database_path", "data/arb.sqlite"))
    sx_fee = sx.get("fee_rate")
    return AppConfig(
        timezone=str(raw["timezone"]),
        bankroll_usd=_decimal(raw["bankroll_usd"]),
        max_stake_pct=_decimal(raw["max_stake_pct"]),
        ev_threshold=_decimal(raw["ev_threshold"]),
        divergence_threshold=_decimal(raw["divergence_threshold"]),
        arb_stale_warn=_decimal(raw["arb_stale_warn"]),
        max_leg_skew_secs=int(raw["max_leg_skew_secs"]),
        max_start_drift_mins=int(raw["max_start_drift_mins"]),
        match_suspect_prob_diff=_decimal(raw["match_suspect_prob_diff"]),
        capital_charge_annual=_decimal(raw.get("capital_charge_annual", "0.05")),
        sport_duration_hours={k: _decimal(v) for k, v in (raw.get("sport_typical_duration_hours") or {}).items()},
        venue_settlement_lag_hours={k: _decimal(v) for k, v in (raw.get("venue_settlement_lag_hours") or {}).items()},
        min_books_for_consensus=int(raw["min_books_for_consensus"]),
        min_books_for_consensus_totals=int(raw.get("min_books_for_consensus_totals", raw["min_books_for_consensus"])),
        devig_method=str(raw.get("devig_method", "proportional")),
        consensus_outlier_points=(None if raw.get("consensus_outlier_points") in (None, "")
                                  else _decimal(raw.get("consensus_outlier_points"))),
        sharp_books=frozenset(raw.get("sharp_books") or []),
        weight_sharp_books=bool(raw.get("weight_sharp_books", False)),
        sharp_book_weight=int(raw.get("sharp_book_weight", 2)),
        min_liquidity_usd=_decimal(raw["min_liquidity_usd"]),
        min_executable_usd=_decimal(raw["min_executable_usd"]),
        micro_enabled=bool(micro.get("enabled", False)),
        micro_min_depth_usd=_decimal(micro.get("min_depth_usd", "5")),
        micro_max_stake_usd=_decimal(micro.get("max_stake_usd", "25")),
        micro_e_min=_decimal(micro.get("e_min", ev.get("e_min", "0.05"))),
        max_days_to_settlement=int(raw["max_days_to_settlement"]),
        nonsports_max_days=int(raw["nonsports_max_days"]),
        scan_times=list(raw["scan_times"]),
        daily_report_time=str(raw["daily_report_time"]),
        settle_time=str(raw["settle_time"]),
        settle_days_from=int(raw["settle_days_from"]),
        settle_health_overdue_max=int(raw.get("settle_health_overdue_max", 0)),
        settle_grace_hours=int(raw.get("settle_grace_hours", 6)),
        sports=list(raw["sports"]),
        consensus_sports=list(raw["consensus_sports"]),
        scan_breaks=bool(raw["scan_breaks"]),
        break_windows=dict(raw["break_windows"]),
        watch_interval_secs=int(raw["watch_interval_secs"]),
        consensus_enabled=bool(raw["consensus_enabled"]),
        consensus_max_age_hours=int(raw["consensus_max_age_hours"]),
        consensus_pregame_max_age_hours=int(raw.get("consensus_pregame_max_age_hours", 3)),
        ev_e_min=_decimal(ev.get("e_min", "0.05")),
        ev_kelly_fraction=_decimal(ev.get("kelly_fraction", "0.25")),
        ev_edge_suspect_above=_decimal(ev.get("edge_suspect_above", "0.25")),
        ev_enforce_depth_floor=bool(ev.get("enforce_depth_floor", True)),
        ev_reject_one_sided=bool(ev.get("reject_one_sided_books", True)),
        ev_edge_decay_per_hour=_decimal(ev.get("edge_decay_per_hour", "0")),
        ev_dispersion_gate=bool(ev.get("dispersion_gate", False)),
        ev_edge_se_min=_decimal(ev.get("edge_se_min", "1.0")),
        ev_dispersion_kelly_haircut=_decimal(ev.get("dispersion_kelly_haircut", "1.0")),
        ev_min_expected_profit_usd=_decimal(ev.get("min_expected_profit_usd", "0")),
        # B6: effective-sample-size correction for correlated books (0 = legacy independent SE).
        ev_dispersion_book_correlation=_decimal(ev.get("dispersion_book_correlation", "0")),
        # B7: shrink thin-consensus p_hat toward this prior; strength 0 = off (no shrinkage).
        ev_shrink_prior=_decimal(ev.get("shrink_prior", "0.5")),
        ev_shrink_prior_strength=_decimal(ev.get("shrink_prior_strength", "0")),
        # B11: price the push risk on integer totals instead of only flagging it.
        ev_push_pricing=bool(ev.get("push_pricing", False)),
        # B12: capital is locked until settlement, not first pitch; add this lag to start_utc.
        ev_settlement_lag_hours=_decimal(ev.get("settlement_lag_hours", "0")),
        # B8: correlation-aware sizing across same-event EV bets (rho 0 / cap 0 = off).
        ev_correlation_rho=_decimal(ev.get("correlation_rho", "0")),
        ev_correlation_group_cap_fraction=_decimal(ev.get("correlation_group_cap_fraction", "0")),
        # C16: stamp consensus age only from books reporting a real line-move time (don't mix the
        # move clock with our fetch clock). False = legacy fetched_at fallback.
        ev_require_move_time=bool(ev.get("require_move_time", False)),
        auto_fill=bool(raw["auto_fill"]),
        trusted_pairs=set(raw["trusted_pairs"]),
        fuzzy_match_threshold=int(raw["fuzzy_match_threshold"]),
        kalshi_fee_rate=_decimal(kalshi["fee_rate"]),
        polymarket_fee_rate=_decimal(polymarket["fee_rate"]),
        polymarket_sports_fee_rate=_decimal(polymarket.get("sports_fee_rate", polymarket["fee_rate"])),
        polymarket_crypto_fee_rate=_decimal(polymarket.get("crypto_fee_rate", polymarket["fee_rate"])),
        sx_fee_rate=None if sx_fee is None else _decimal(sx_fee),
        sx_win_fee_rate=_decimal(sx.get("win_fee_rate", "0")),
        taker_price_buffer=_decimal(raw.get("execution", {}).get("taker_price_buffer", "0")),
        taker_slippage_model=str(raw.get("execution", {}).get("slippage_model", "flat")),
        slippage_ref_stake_usd=_decimal(raw.get("execution", {}).get("slippage_ref_stake_usd", "50")),
        arb_size_by_ladder=bool(raw.get("execution", {}).get("arb_size_by_ladder", False)),
        arb_fillable_fraction=_decimal(raw.get("execution", {}).get("fillable_fraction", "1.0")),
        arb_size_safety_frac=_decimal(raw.get("execution", {}).get("arb_size_safety_frac", "0.5")),
        arb_min_ladder_fraction=_decimal(raw.get("execution", {}).get("min_ladder_fraction", "0.5")),
        arb_max_actionable_skew_secs=int(raw.get("execution", {}).get("max_actionable_skew_secs", 10)),
        arb_min_adj_edge_points=_decimal(raw.get("execution", {}).get("min_adj_edge_points", "0.002")),
        arb_line_velocity={k: _decimal(v) for k, v in (raw.get("execution", {}).get("line_velocity_points_per_sec") or {"default": "0.001"}).items()},
        arb_doubleheader_cluster_tol_secs=int(raw.get("execution", {}).get("doubleheader_cluster_tol_secs", 7200)),
        odds_api_region=str(odds_api["region"]),
        odds_api_warn_below_credits=int(odds_api["warn_below_credits"]),
        odds_api_abort_below_credits=int(odds_api["abort_below_credits"]),
        parlay_enabled=bool(parlay["enabled"]),
        parlay_divergence_threshold=_decimal(parlay["divergence_threshold"]),
        sizing_kelly_fraction=_decimal(sizing.get("kelly_fraction", "0.25")),
        sizing_max_fraction=_decimal(sizing.get("max_kelly_fraction_of_bankroll", "0.05")),
        scoring=dict(raw.get("scoring", {})),
        arb_sizing=dict(raw.get("arb_sizing", {})),
        negrisk_min_edge=_decimal(negrisk.get("min_edge", "0.005")),
        negrisk_require_exhaustive=bool(negrisk.get("require_exhaustive_exclusive", True)),
        negrisk_max_outcomes=int(negrisk.get("max_outcomes", 30)),
        negrisk_scan_limit=int(negrisk.get("scan_limit", 200)),
        negrisk_scan_pages=int(negrisk.get("scan_pages", 3)),
        # Lower floor than the global min_executable: single-venue, single-settlement, so no
        # cross-venue leg risk justifies measuring thinner baskets (Lever C).
        negrisk_min_executable_usd=_decimal(negrisk.get("min_executable_usd", "5")),
        btc_window_match_secs=int(btc.get("window_match_secs", 5)),
        btc_max_window_minutes=int(btc.get("max_window_minutes", 15)),
        btc_trusted=bool(btc.get("trusted", False)),
        flb=dict(raw.get("flb", {})),
        forecast=dict(raw.get("forecast", {})),
        validation=dict(raw.get("validation", {})),
        live_baseball_ev=dict(raw.get("live_baseball_ev", {})),
        per_venue_caps={k: _decimal(v) for k, v in (raw.get("per_venue_caps") or {}).items() if v not in (None, "")},
        odds_api_key=os.environ.get("ODDS_API_KEY") or None,
        stale_fee_venues=frozenset(stale_fee_venues),
        database_path=database_path,
    )
