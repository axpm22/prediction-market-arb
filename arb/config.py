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
    min_books_for_consensus: int
    min_liquidity_usd: Decimal
    min_executable_usd: Decimal
    max_days_to_settlement: int
    nonsports_max_days: int
    scan_times: list[str]
    daily_report_time: str
    settle_time: str
    settle_days_from: int
    sports: list[str]
    consensus_sports: list[str]
    scan_breaks: bool
    break_windows: dict[str, list[int]]
    watch_interval_secs: int
    consensus_enabled: bool
    consensus_max_age_hours: int
    ev_e_min: Decimal
    ev_kelly_fraction: Decimal
    ev_edge_suspect_above: Decimal
    ev_enforce_depth_floor: bool
    ev_reject_one_sided: bool
    auto_fill: bool
    trusted_pairs: set[str]
    fuzzy_match_threshold: int
    kalshi_fee_rate: Decimal
    polymarket_fee_rate: Decimal
    sx_fee_rate: Decimal | None
    odds_api_region: str
    odds_api_warn_below_credits: int
    odds_api_abort_below_credits: int
    parlay_enabled: bool
    parlay_divergence_threshold: Decimal
    sizing_kelly_fraction: Decimal
    sizing_max_fraction: Decimal
    scoring: dict
    negrisk_min_edge: Decimal
    negrisk_require_exhaustive: bool
    negrisk_max_outcomes: int
    btc_window_match_secs: int
    btc_max_window_minutes: int
    btc_trusted: bool
    flb: dict
    forecast: dict
    validation: dict
    odds_api_key: str | None
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


def _warn_stale_fees(raw: dict[str, Any], venues: dict[str, dict[str, Any]]) -> None:
    # Stale fees warn but never halt — silent downtime is worse than a dated number.
    stale_days = int(raw.get("fee_stale_days", 30))
    today = datetime.now(timezone.utc).date()
    for venue, block in venues.items():
        confirmed = block.get("confirmed_on")
        if confirmed is None:
            print(f"config: WARNING {venue} fee_rate has no confirmed_on date.")
            continue
        confirmed = confirmed if isinstance(confirmed, date) else date.fromisoformat(str(confirmed))
        age = (today - confirmed).days
        if age > stale_days:
            print(f"config: WARNING {venue} fee confirmed {age} days ago (> {stale_days}); re-check {block.get('source_url', 'source')}.")


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> AppConfig:
    _load_env()
    if not path.exists():
        raise FileNotFoundError(f"Missing config file: {path}")
    if yaml is None:
        raise RuntimeError("pyyaml is required to load config.yaml. Run `pip install -r requirements.txt`.")
    raw = yaml.safe_load(path.read_text()) or {}
    odds_api = raw.get("odds_api", {})
    ev = raw.get("ev", {})
    kalshi = raw.get("kalshi", {})
    polymarket = raw.get("polymarket", {})
    sx = raw.get("sx", {})
    _warn_stale_fees(raw, {"kalshi": kalshi, "polymarket": polymarket, "sx": sx})
    parlay = raw.get("parlay", {})
    sizing = raw.get("sizing", {})
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
        min_books_for_consensus=int(raw["min_books_for_consensus"]),
        min_liquidity_usd=_decimal(raw["min_liquidity_usd"]),
        min_executable_usd=_decimal(raw["min_executable_usd"]),
        max_days_to_settlement=int(raw["max_days_to_settlement"]),
        nonsports_max_days=int(raw["nonsports_max_days"]),
        scan_times=list(raw["scan_times"]),
        daily_report_time=str(raw["daily_report_time"]),
        settle_time=str(raw["settle_time"]),
        settle_days_from=int(raw["settle_days_from"]),
        sports=list(raw["sports"]),
        consensus_sports=list(raw["consensus_sports"]),
        scan_breaks=bool(raw["scan_breaks"]),
        break_windows=dict(raw["break_windows"]),
        watch_interval_secs=int(raw["watch_interval_secs"]),
        consensus_enabled=bool(raw["consensus_enabled"]),
        consensus_max_age_hours=int(raw["consensus_max_age_hours"]),
        ev_e_min=_decimal(ev.get("e_min", "0.05")),
        ev_kelly_fraction=_decimal(ev.get("kelly_fraction", "0.25")),
        ev_edge_suspect_above=_decimal(ev.get("edge_suspect_above", "0.25")),
        ev_enforce_depth_floor=bool(ev.get("enforce_depth_floor", True)),
        ev_reject_one_sided=bool(ev.get("reject_one_sided_books", True)),
        auto_fill=bool(raw["auto_fill"]),
        trusted_pairs=set(raw["trusted_pairs"]),
        fuzzy_match_threshold=int(raw["fuzzy_match_threshold"]),
        kalshi_fee_rate=_decimal(kalshi["fee_rate"]),
        polymarket_fee_rate=_decimal(polymarket["fee_rate"]),
        sx_fee_rate=None if sx_fee is None else _decimal(sx_fee),
        odds_api_region=str(odds_api["region"]),
        odds_api_warn_below_credits=int(odds_api["warn_below_credits"]),
        odds_api_abort_below_credits=int(odds_api["abort_below_credits"]),
        parlay_enabled=bool(parlay["enabled"]),
        parlay_divergence_threshold=_decimal(parlay["divergence_threshold"]),
        sizing_kelly_fraction=_decimal(sizing.get("kelly_fraction", "0.25")),
        sizing_max_fraction=_decimal(sizing.get("max_kelly_fraction_of_bankroll", "0.05")),
        scoring=dict(raw.get("scoring", {})),
        negrisk_min_edge=_decimal(negrisk.get("min_edge", "0.005")),
        negrisk_require_exhaustive=bool(negrisk.get("require_exhaustive_exclusive", True)),
        negrisk_max_outcomes=int(negrisk.get("max_outcomes", 30)),
        btc_window_match_secs=int(btc.get("window_match_secs", 5)),
        btc_max_window_minutes=int(btc.get("max_window_minutes", 15)),
        btc_trusted=bool(btc.get("trusted", False)),
        flb=dict(raw.get("flb", {})),
        forecast=dict(raw.get("forecast", {})),
        validation=dict(raw.get("validation", {})),
        odds_api_key=os.environ.get("ODDS_API_KEY") or None,
        database_path=database_path,
    )
