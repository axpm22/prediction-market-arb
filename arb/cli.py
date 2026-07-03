from __future__ import annotations

import argparse
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import requests

from .activity import active_sports
from .arbitrage import build_event, detect_divergences, detect_true_arbs, split_match_suspects, status_for_legs
from .config import load_config
from .matching import event_start_map, load_aliases
from .models import Opportunity, Quote
from .paper import fill_opportunity
from .pricing import consensus_all, consensus_dispersion, consensus_outcome_key, consensus_outcome_times, consensus_probabilities, depth_scaled_buffer, edge_points, expected_value, is_economic, money, q6, walk_ladder
from .quality import classify_quality
from .sizing import kelly_f_star, kelly_stake
from .report import print_opportunities, print_positions, print_report
from .settlement import settle_backfill_mlb, settle_manual, settle_sports
from .store import Store
from .tracker import record_tracker_poll, write_daily_report
from .validation import load_validation_rule
from .venues.kalshi import KalshiAdapter
from .venues.oddsapi import OddsApiAdapter, OddsApiCreditsLow
from .venues.polymarket import PolymarketAdapter
from .venues.sx import SxAdapter


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m arb")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="refresh sportsbook consensus (no-op if consensus is disabled)")
    scan.add_argument("--dry-run", action="store_true", help="fetch and display without persisting")
    scan.add_argument("--fixtures", action="store_true", help="use tests/fixtures instead of live endpoints")
    scan.add_argument("--sport", action="append", help="scan only this sport key; may be repeated")

    watch = sub.add_parser("watch", help="primary keyless venue loop")
    watch.add_argument("--once", action="store_true", help="run one watch cycle")
    watch.add_argument("--dry-run", action="store_true", help="display without persisting")
    watch.add_argument("--fixtures", action="store_true", help="use tests/fixtures instead of live endpoints")
    watch.add_argument("--sport", action="append", help="watch only this sport key; may be repeated")

    report = sub.add_parser("report", help="show P&L and aggregate counts")
    report.add_argument("--daily", nargs="?", const="today", help="write/recompute a daily tracker report")
    sub.add_parser("positions", help="show open and historical paper positions")
    settle = sub.add_parser("settle", help="resolve open EV flat bets against final scores")
    settle.add_argument("--manual", nargs=2, metavar=("EVENT_KEY", "WINNING_OUTCOME"),
                        help="settle one event by hand (required for non-sports)")
    settle.add_argument("--fixtures", action="store_true", help="use recorded scores fixtures")
    backfill = sub.add_parser(
        "settle-backfill",
        help="settle MLB bets via the MLB Stats API (no 3-day window; rescues orphaned bets)")
    backfill.add_argument("--fixtures", action="store_true", help="use recorded mlbstats fixtures")
    migrate = sub.add_parser("migrate-unique", help="dedup within-poll duplicate quotes + add UNIQUE index (pass3#7)")
    migrate.add_argument("--apply", action="store_true", help="back up, dedup, and create the index (default: dry-run)")
    parlay_scan = sub.add_parser("parlay-scan", help="detect parlay mispricings (SPEC §11); REVIEW-only, own ledger lane")
    parlay_scan.add_argument("--fixtures", action="store_true", help="load parlay markets from tests/fixtures/parlays/markets.json")
    parlay_scan.add_argument("--file", help="path to a parlay markets JSON file (venue-agnostic schema)")
    parlay_scan.add_argument("--dry-run", action="store_true", help="detect and display without persisting")
    sub.add_parser("settle-health", help="report overdue open EV/ARB bets and last settlement time")
    sub.add_parser("reconcile", help="cross-check settled ARBs against venue-actual resolution (audit fix #1)")
    sub.add_parser("exposure", help="total + per-venue committed capital across active bets (audit fix #3)")
    sub.add_parser("scores", help="per-segment edge scores from settled clean bets (Phase 2)")
    sub.add_parser("backtest-flb", help="favorite-longshot calibration over settled clean bets (S2)")
    sub.add_parser("dashboard", help="regenerate reports/dashboard.json from the ledger (Phase 6)")
    regrade = sub.add_parser("regrade", help="score consensus vs venue mids on settled events + EV-subset (Phase A)")
    regrade.add_argument("--sport", default="baseball_mlb")
    regrade.add_argument("--db", default=None,
                         help="analyze an archive DB read-only (e.g. data/archive-pre-v2/arb.sqlite) instead of live")
    sub.add_parser("status", help="every loop: alive/dead, beats, code version, last poll (Phase D)")
    sub.add_parser("supervisor", help="own + restart all enabled loops; single-run (Phase D)")
    sub.add_parser("check-invariants", help="ledger integrity scan (5 checks); CRITICAL, never blocks (02)")
    forecast_log = sub.add_parser("forecast-log", help="append a forecast probability triple (Phase 8)")
    forecast_log.add_argument("--vertical", required=True)
    forecast_log.add_argument("--category", required=True)
    forecast_log.add_argument("--market", required=True)
    forecast_log.add_argument("--p-you", required=True)
    forecast_log.add_argument("--p-market", required=True)
    forecast_log.add_argument("--p-base")
    forecast_log.add_argument("--executable-price")
    forecast_log.add_argument("--settle-by")
    forecast_log.add_argument("--paper-bet", action="store_true")
    forecast_settle = sub.add_parser("forecast-settle", help="settle a logged forecast by id")
    forecast_settle.add_argument("forecast_id", type=int)
    forecast_settle.add_argument("outcome", type=int, choices=[0, 1])
    forecast_settle.add_argument("--closing-price")
    sub.add_parser("forecast-report", help="score settled forecasts against market probabilities")
    sub.add_parser("daemon", help="watch loop + scheduled scans + settlement + daily report writer")
    annotate = sub.add_parser("annotate", help="void bad flat_bets rows via append-only annotations (stop_inplay_betting_fix)")
    annotate.add_argument("--reason", required=True, help="annotation reason, e.g. inplay-mislabeled")
    annotate.add_argument("--where", required=True, help="SQL predicate on flat_bets selecting rows to void")
    annotate.add_argument("--notes", help="optional free-text note stored on each annotation")
    annotate.add_argument("--yes", action="store_true", help="write the annotations (default: dry-run preview)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config()
    store = Store(config.database_path)
    if args.command == "report":
        if args.daily:
            report_date = None if args.daily == "today" else datetime.fromisoformat(args.daily).date()
            csv_path, md_path = write_daily_report(store, report_date)
            print(f"Wrote {csv_path} and {md_path}")
            return 0
        print_report(store)
        return 0
    if args.command == "positions":
        print_positions(store)
        return 0
    if args.command == "annotate":
        return run_annotate(args, store)
    if args.command == "reconcile":
        from .reconcile import default_resolvers, reconcile_settlements

        counts = reconcile_settlements(store, default_resolvers())
        print(f"Reconciled settled ARBs vs venue-actual resolution: {counts}")
        if counts["corrected"]:
            print(f"  {counts['corrected']} SETTLER-ERROR(s) — realized P&L differed from the lock.")
        if counts["skipped"]:
            print(f"  {counts['skipped']} skipped (a leg's venue resolution not available yet).")
        return 0
    if args.command == "migrate-unique":
        from .migrations import format_migration, migrate_unique

        print(format_migration(migrate_unique(store, apply=args.apply)))
        return 0
    if args.command == "exposure":
        from .exposure import compute_exposure, format_exposure

        per_venue_cap = config.bankroll_usd * config.max_stake_pct
        print(format_exposure(compute_exposure(store, config.bankroll_usd, per_venue_cap,
                                               per_venue_caps=config.per_venue_caps,
                                               sport_duration_hours=config.sport_duration_hours,
                                               venue_settlement_lag_hours=config.venue_settlement_lag_hours)))
        return 0
    if args.command == "scores":
        from .scoring import persist_scores, score_segments

        scores = score_segments(store, config.scoring)
        persist_scores(store, scores)
        print(f"{'SEGMENT':28s} {'N':>4} {'WR':>6} {'ROI':>8} {'CONF%':>6} {'SCORE':>6}  TIER")
        for s in scores:
            print(f"{s.segment:28s} {s.n:>4} {s.win_rate:>6.1%} {s.roi:>8.2%} "
                  f"{s.confirmation_rate:>6.0%} {s.score:>6.1f}  {s.tier}")
        if not scores:
            print("(no settled clean bets yet — INSUFFICIENT_DATA across the board)")
        return 0
    if args.command == "backtest-flb":
        from .bias import SELECTION_CAVEAT, calibration
        from .quote_settlement import population_binaries

        flb = config.flb
        bands = list(flb.get("bands", []))
        min_samples = int(flb.get("min_samples_per_band", 200))
        min_edge = float(flb.get("flb_min_edge", 0.02))
        # Prefer the population sample (settled persisted moneyline quotes, de-vigged); fall
        # back to the selection-biased flagged-bet sample only if none could be settled.
        population = population_binaries(store)
        if population:
            print(f"POPULATION sample: {len(population)} settled pregame moneyline observations (de-vigged).")
            stats = calibration(store, bands, min_samples, min_edge, data=population)
        else:
            print(SELECTION_CAVEAT)
            stats = calibration(store, bands, min_samples, min_edge)
        print(f"{'BAND':>14} {'N':>6} {'MEAN_COST':>10} {'REALIZED':>9} {'EDGE':>8}  SIGNAL")
        for s in stats:
            print(f"  [{s.lo:.2f},{s.hi:.2f}) {s.n:>6} {s.mean_cost:>10.1%} {s.realized_win_rate:>9.1%} {s.edge:>+8.1%}  {s.signal}")
        return 0
    if args.command == "status":
        from .heartbeat import format_status, git_head, read_heartbeats
        print("Loop status (Phase D):")
        print(format_status(read_heartbeats(store), current_head=git_head()))
        return 0
    if args.command == "supervisor":
        from .heartbeat import run_supervisor
        return run_supervisor(config, store)
    if args.command == "check-invariants":
        from .invariants import check_invariants, format_invariants
        violations = check_invariants(store, config.settle_tripwire_secs, config.tracker_max_reappearances)
        print(format_invariants(violations))
        return 0
    if args.command == "regrade":
        from .experimental.analysis.regrade import ev_subset_test, format_regrade, score_sources
        from .experimental.analysis.devig_brier import format_methods, score_methods
        # --db opens an archive read-only (Phase A analysis lives in the archive, not the fresh DB).
        target = Store(Path(args.db), read_only=True) if args.db else store
        if args.db:
            print(f"regrade: analyzing {args.db} (read-only); scores via MLB Stats (free/keyless oracle).")
        sources = score_sources(target, args.sport)
        print(format_regrade(sources, ev_subset_test(target)))
        print("\nDe-vig method regrade (pick devig_method by whichever wins):")
        print(format_methods(score_methods(target, config.min_books_for_consensus)))
        return 0
    if args.command == "dashboard":
        from .experimental.dashboard import write_dashboard

        path = write_dashboard(store, config)
        print(f"Wrote {path}")
        return 0
    if args.command == "forecast-log":
        from .forecast.ledger import log_forecast

        forecast_cfg = config.forecast
        forecast_id = log_forecast(
            store,
            vertical=args.vertical,
            category=args.category,
            market=args.market,
            p_you=Decimal(args.p_you),
            p_market=Decimal(args.p_market),
            p_base=None if args.p_base is None else Decimal(args.p_base),
            executable_price=None if args.executable_price is None else Decimal(args.executable_price),
            settle_by=args.settle_by,
            edge_min=Decimal(str(forecast_cfg.get("edge_min", "0.05"))),
            record_paper_bet=args.paper_bet,
        )
        print(f"Logged forecast {forecast_id}")
        return 0
    if args.command == "forecast-settle":
        from .forecast.ledger import settle_forecast

        forecast_cfg = config.forecast
        settle_forecast(
            store,
            args.forecast_id,
            args.outcome,
            closing_price=None if args.closing_price is None else Decimal(args.closing_price),
            clip=float(forecast_cfg.get("logloss_clip", 0.000001)),
        )
        print(f"Settled forecast {args.forecast_id}")
        return 0
    if args.command == "forecast-report":
        from .forecast.ledger import format_report, report_data

        forecast_cfg = config.forecast
        data = report_data(
            store,
            bins=int(forecast_cfg.get("calibration_bins", 10)),
            clip=float(forecast_cfg.get("logloss_clip", 0.000001)),
        )
        print(format_report(data, int(forecast_cfg.get("min_n_for_claim", 100))))
        return 0
    if args.command == "settle":
        if args.manual:
            settled = settle_manual(store, args.manual[0], args.manual[1])
        else:
            fixture_dir = Path("tests/fixtures") if args.fixtures else None
            settled = settle_sports(store, config, fixture_dir)
        print(f"Settled {settled} flat bets.")
        # Audit A2 — close the reconciliation loop after every live settle (the daemon
        # already does this in run_settle_job). Skipped under --fixtures so tests never
        # make live venue calls; non-fatal so a venue hiccup can't undo the settle.
        if not args.manual and not args.fixtures:
            try:
                from .reconcile import default_resolvers, reconcile_settlements

                counts = reconcile_settlements(store, default_resolvers())
                print(f"Reconciled settled ARBs vs venue-actual resolution: {counts}")
            except Exception as exc:  # noqa: BLE001
                print(f"settle: reconciliation failed (settle still stands): {exc}")
        # 02: invariant scan at the end of the settle cycle. CRITICAL log, never blocks.
        from .invariants import check_invariants, format_invariants
        print(format_invariants(check_invariants(
            store, config.settle_tripwire_secs, config.tracker_max_reappearances)))
        return 0
    if args.command == "parlay-scan":
        return _run_parlay_scan(store, config, args)
    if args.command == "settle-backfill":
        fixture_dir = Path("tests/fixtures") if args.fixtures else None
        settled = settle_backfill_mlb(store, fixture_dir, config.settle_tripwire_secs)
        print(f"Backfilled {settled} MLB flat bets via the MLB Stats API.")
        return 0
    if args.command == "settle-health":
        from .settlement_health import format_settle_health, settle_health

        health = settle_health(store, config.timezone, config.settle_grace_hours)
        print(format_settle_health(health))
        return 1 if health.overdue_count > config.settle_health_overdue_max else 0
    if args.command == "scan":
        return run_scan(args, config, store)
    if args.command == "watch":
        return run_watch(args, config, store)
    if args.command == "daemon":
        return run_daemon(config, store)
    return 2


def stop_requested(process_name: str) -> bool:
    """Graceful shutdown via flag files: touch data/stop-all or data/stop-<name>.
    Checked between transactions so a stop never interrupts a half-written poll."""
    return Path("data/stop-all").exists() or Path(f"data/stop-{process_name}").exists()


DAEMON_META_PATH = Path("data/daemon.meta")


def _git_state() -> tuple[str | None, bool | None]:
    """(HEAD sha, dirty-tree?) or (None, None) when git is unavailable."""
    import subprocess

    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, timeout=5).stdout.strip())
        return (head or None), dirty
    except Exception:  # noqa: BLE001 - a missing git must not break the scanner.
        return None, None


def write_daemon_meta(name: str) -> tuple[str | None, bool | None]:
    """Record the code identity a long-running process STARTED on (stop_inplay_betting_fix step 4):
    the fix that sat in the tree while the OLD daemon kept betting is the incident this makes visible."""
    import json
    import os

    head, dirty = _git_state()
    DAEMON_META_PATH.parent.mkdir(parents=True, exist_ok=True)
    DAEMON_META_PATH.write_text(json.dumps({
        "name": name, "pid": os.getpid(), "started_at": datetime.now(timezone.utc).isoformat(),
        "git_head": head, "dirty": dirty,
    }))
    return head, dirty


def code_change_warning(start_state: tuple[str | None, bool | None]) -> str | None:
    """Loud message if HEAD or the dirty flag drifted from the process's start state — code changed
    under the running daemon, which does NOT reload it. Returns None when unchanged or git absent."""
    head, dirty = _git_state()
    if head is None or (head, dirty) == start_state:
        return None
    return (f"code changed under running daemon (was {start_state[0]} dirty={start_state[1]}, "
            f"now {head} dirty={dirty}) — restart to pick it up")


def run_watch(args: argparse.Namespace, config, store: Store) -> int:
    start_state = write_daemon_meta("watch") if not args.once else (None, None)
    warned: set[tuple] = set()
    while True:
        code = run_watch_once(args, config, store)
        if args.once:
            return code
        warning = code_change_warning(start_state)
        if warning and warning not in warned:
            print(f"WARNING: {warning}")
            warned.add(warning)
        if stop_requested("daemon"):
            print("daemon: stop flag found, exiting cleanly.")
            return 0
        time.sleep(config.watch_interval_secs)


def bookable_opportunities(kept: list, ev_enabled: bool) -> list:
    """Phase A freeze: when ev_enabled is false, consensus EV flags (kind 'EV' — pregame + micro)
    are recorded as opportunities but never booked as paper bets, because the archive regrade showed
    the consensus is a worse forecaster than venue mids (the flag selects consensus error, not edge).
    LIVE_BASEBALL_EV and every other kind are untouched. One gate, in one place."""
    return kept if ev_enabled else [o for o in kept if o.kind != "EV"]


def run_watch_once(args: argparse.Namespace, config, store: Store) -> int:
    fixture_dir = Path("tests/fixtures") if args.fixtures else None
    adapters = [
        KalshiAdapter(config, fixture_dir),
        PolymarketAdapter(config, fixture_dir),
        SxAdapter(config, fixture_dir),
    ]
    sports = args.sport or config.sports
    quotes, failed_venues = fetch_all_venues(adapters, sports)
    if failed_venues:
        print(f"venue fetch failed/empty: {sorted(failed_venues)} — their fingerprints are "
              f"exempt from absent-closing this poll (K3).")
    _warn_if_no_cross_venue_overlap(quotes)

    now = datetime.now(timezone.utc)
    # SKIP_DEAD_SPORTS: venue listings are the season calendar. Dormant sports (no fixture
    # within the lookahead) are excluded from detection but their quotes still persist.
    # ponytail: venues are still fetched every poll (they're free and reactivation is then
    # instant); add the dormant-recheck cadence only if poll sweep time ever matters.
    active = active_sports(quotes, now, config.activity_lookahead_hours)
    dormant = {sport for sport in sports if sport not in active}
    for name in sorted(dormant):
        print(f"skip-dormant-sport: {name} (no venue fixtures within "
              f"{config.activity_lookahead_hours}h)")

    pregame, in_play = split_pregame(quotes, now, config.unreliable_start_venues)
    if in_play:
        print(f"{len(in_play)} in-play quotes persisted with live=1 and excluded from detection (pregame only).")
    # Depth floor BEFORE detection: a derived 1-cent ask backed by $3 of resting size is
    # not a main-lane price. A separate EV-only micro lane can measure small real edges,
    # but thin ARB/divergence stays excluded because partial fills create leg risk.
    detectable, micro_detectable, too_thin = split_depth_lanes(pregame, config)
    if dormant:
        detectable = [q for q in detectable if q.sport not in dormant]
        micro_detectable = [q for q in micro_detectable if q.sport not in dormant]
    thin_count = len(micro_detectable) + len(too_thin)
    if thin_count:
        if micro_detectable:
            print(
                f"{thin_count} thin quotes (depth < ${config.min_executable_usd}): "
                f"{len(micro_detectable)} micro-eligible EV-only quotes "
                f"(${config.micro_min_depth_usd}-${config.min_executable_usd} depth, "
                f"edge >= {config.micro_e_min}); {len(too_thin)} too thin and excluded."
            )
        else:
            print(f"{thin_count} thin quotes (depth < ${config.min_executable_usd}) excluded from detection.")
    opportunities = detect_true_arbs(
        detectable,
        bankroll=config.bankroll_usd,
        max_stake_pct=config.max_stake_pct,
        trusted_pairs=config.trusted_pairs,
        stale_warn=config.arb_stale_warn,
        max_leg_skew_secs=config.max_leg_skew_secs,
        max_start_drift_mins=config.max_start_drift_mins,
        match_suspect_prob_diff=config.match_suspect_prob_diff,
        capital_charge_annual=config.capital_charge_annual,
        size_by_ladder=config.arb_size_by_ladder,
        size_safety_frac=config.arb_size_safety_frac,
        fillable_fraction=config.arb_fillable_fraction,
        line_velocity=config.arb_line_velocity,
        max_actionable_skew_secs=config.arb_max_actionable_skew_secs,
        min_adj_edge_points=config.arb_min_adj_edge_points,
        doubleheader_cluster_tol_secs=config.arb_doubleheader_cluster_tol_secs,
        sport_duration_hours=config.sport_duration_hours,
        venue_settlement_lag_hours=config.venue_settlement_lag_hours,
    )
    opportunities.extend(
        detect_divergences(
            detectable, config.divergence_threshold, config.trusted_pairs,
            config.max_leg_skew_secs, config.max_start_drift_mins, config.match_suspect_prob_diff,
        )
    )
    # SPEC §1: the +EV class exists only when consensus_enabled. watch never calls
    # The Odds API; EV uses consensus stored by the scan cadence.
    ev_suppressed: list[dict] = []  # Part 1: EV candidates deliberately not booked (by reason)
    if config.consensus_enabled:
        calibrator = None
        if config.flb.get("apply_calibration"):  # EV#5: p_hat_calibrated from settled raw quotes
            from .bias import fit_calibrator
            from .quote_settlement import population_binaries
            calibrator = fit_calibrator(population_binaries(store),
                                        int(config.flb.get("calibration_min_samples", 200)))
        opportunities.extend(
            detect_ev_from_consensus(
                detectable,
                store.load_consensus(),
                max_age_hours=config.consensus_max_age_hours,
                trusted_pairs=config.trusted_pairs,
                e_min=config.ev_e_min,
                config=config,
                pregame_max_age_hours=config.consensus_pregame_max_age_hours,
                calibrator=calibrator,
                dispersion=store.load_consensus_dispersion(),
                left_on_table=ev_suppressed,
            )
        )
        if ev_suppressed:
            from collections import Counter
            buckets = Counter(row["reason"] for row in ev_suppressed)
            print("EV left on table (not booked): " + ", ".join(f"{r}={n}" for r, n in buckets.items()))
        opportunities.extend(
            detect_micro_ev_from_consensus(micro_detectable, store.load_consensus(), config)
        )
    opportunities, suspects = split_match_suspects(opportunities)
    if suspects:
        print(f"{len(suspects)} match-suspect pairs quarantined for human review (not recorded as flags).")
    kept, left_on_table = apply_opportunity_filters(opportunities, config)
    if left_on_table:
        left_edge = sum((opp.edge for opp in left_on_table), Decimal("0"))
        print(f"Left on table: {len(left_on_table)} flags (summed edge {left_edge}) killed by liquidity/depth/horizon filters.")
    kept = apply_segment_gates(kept, store, config)
    kept = apply_correlation_sizing(kept, config)  # B8
    print_opportunities(kept, ev_display_threshold=config.ev_threshold)

    if args.dry_run:
        print("Watch dry run complete; quotes and tracker rows were not persisted.")
        return 0

    poll_id = store.start_poll("watch")
    store.save_poll_quotes(poll_id, [*pregame, *in_play])
    store.save_poll_opportunities(poll_id, kept)  # opportunities still recorded (study data)
    store.save_suspect_matches(poll_id, suspects)
    store.save_left_on_table(poll_id, ev_suppressed)
    # Phase A verdict (2026-07-02): consensus is a worse forecaster than venue mids, so EV flags
    # are anti-edge. Freeze EV BET creation (kind EV = pregame + micro, both consensus-based) while
    # still recording the opportunities above for the venue-leads-books study. LIVE_BASEBALL_EV
    # (in-play, own lane) is unaffected.
    bookable = bookable_opportunities(kept, config.ev_enabled)
    if not config.ev_enabled and len(bookable) != len(kept):
        print(f"ev_enabled=false: {len(kept) - len(bookable)} EV flag(s) recorded but NOT booked (Phase A freeze).")
    record_tracker_poll(store, poll_id, bookable, load_validation_rule(config),
                        terminal_mute_grace_mins=config.tracker_terminal_mute_grace_mins,
                        failed_venues=failed_venues, dormant_sports=dormant,
                        event_starts=event_start_map(quotes, config.unreliable_start_venues))
    if config.auto_fill:
        for opportunity in kept:
            fill_opportunity(store, poll_id, opportunity, config.bankroll_usd, config.max_stake_pct)
    store.finish_poll(poll_id, None, len(quotes), len(kept))
    # Phase D: beat so `arb status` / the supervisor can see this loop is alive and current.
    from .heartbeat import beat
    beat(store, "watch", polls_done=poll_id, interval_secs=config.watch_interval_secs)
    print(f"Persisted watch poll {poll_id}: {len(quotes)} quotes, {len(kept)} opportunities.")
    return 0


def _run_parlay_scan(store: Store, config, args: argparse.Namespace) -> int:
    """Detect parlay mispricings (SPEC §11) and record them as REVIEW-only PARLAY bets in their
    own ledger lane. Fixtures-first: live Kalshi MVE / Polymarket CAOC / SX parlay field-mapping
    must be verified before live use (hard rule #3), so markets come from a fixture/JSON file."""
    import json

    from .parlay import detect_parlays, load_parlay_markets

    if not config.parlay_enabled and not args.dry_run:
        print("parlay-scan: parlay.enabled is false — forcing --dry-run (no ledger writes). "
              "Set parlay.enabled: true in config.yaml to record.")
        args.dry_run = True

    path = args.file or ("tests/fixtures/parlays/markets.json" if args.fixtures else None)
    if path is None:
        print("parlay-scan: no live parlay adapters yet — Kalshi MVE / Polymarket CAOC / SX "
              "field-mapping is unverified (hard rule #3). Pass --fixtures or --file PATH.")
        return 1
    if not Path(path).exists():
        print(f"parlay-scan: markets file not found: {path}")
        return 1

    markets = load_parlay_markets(json.loads(Path(path).read_text()))
    opportunities = detect_parlays(
        markets, config.parlay_divergence_threshold, config.parlay_min_liquidity_usd
    )
    print(f"Parlay markets scanned: {len(markets)}; signals: {len(opportunities)}")
    for opp in opportunities:
        klass = next((n for n in opp.notes if n.startswith("parlay-")), "parlay")
        print(f"  [{opp.status}] {opp.event.sport} {klass} edge_pts={opp.edge_points} legs={len(opp.legs) - 1}")
    if args.dry_run:
        print("(dry-run — nothing recorded)")
        return 0

    poll_id = store.start_poll("parlay")
    quotes = [leg for opp in opportunities for leg in opp.legs]
    store.save_poll_quotes(poll_id, quotes)
    store.save_poll_opportunities(poll_id, opportunities)
    # only_kinds scopes the lifetime bookkeeping to PARLAY so this separate cadence never
    # closes the watch stream's active ARB/EV fingerprints.
    record_tracker_poll(store, poll_id, opportunities, load_validation_rule(config),
                        only_kinds=("PARLAY",),
                        terminal_mute_grace_mins=config.tracker_terminal_mute_grace_mins)
    print(f"Recorded {len(opportunities)} parlay signal(s) into the ledger (kind=PARLAY, REVIEW).")
    return 0


def run_scan(args: argparse.Namespace, config, store: Store) -> int:
    if not config.consensus_enabled:
        print("consensus_enabled is false; scan is a no-op and no Odds API credits were spent.")
        return 0
    fixture_dir = Path("tests/fixtures") if args.fixtures else None
    adapter = OddsApiAdapter(config, fixture_dir)
    sports = args.sport or config.consensus_sports
    if fixture_dir is None:
        # SKIP_DEAD_SPORTS credit saver: a dormant sport must never trigger an Odds API
        # call. Activity comes from the venue quotes watch already persists every poll
        # (fixture runs skip the gate — no live listings to derive activity from).
        venue_active = store.sports_with_upcoming(
            datetime.now(timezone.utc), config.activity_lookahead_hours)
        dormant = [sport for sport in sports
                   if config.odds_api_sport_aliases.get(sport, sport) not in venue_active]
        for name in dormant:
            print(f"skip-dormant-sport: {name} (no venue fixtures within "
                  f"{config.activity_lookahead_hours}h)")
        sports = [sport for sport in sports if sport not in dormant]
    quotes: list[Quote] = []
    aborted = False
    for sport in sports:
        try:
            venue_quotes = adapter.fetch_quotes(sport)
        except FileNotFoundError:
            continue
        except OddsApiCreditsLow as exc:
            # CLAUDE.md rule 2: abort the whole scan, do not continue to other sports.
            print(f"{adapter.name}: {exc}")
            aborted = True
            break
        except requests.RequestException as exc:
            # CLAUDE.md rule 3: a 4xx means stop and report, never guess alternatives.
            print(f"{adapter.name} ({sport}): {exc}")
            aborted = True
            break
        quotes.extend(venue_quotes)
        print(f"{sport} {adapter.name}: {len(venue_quotes)} quotes")

    computed_at = datetime.now(timezone.utc)
    pregame, in_play = split_pregame(quotes, computed_at, config.unreliable_start_venues)
    quotes = [*pregame, *in_play]
    # In-play sportsbook odds must never become "pregame consensus".
    # D19: one shared de-vig pass yields both consensus and dispersion (was two passes).
    consensus, dispersion = compute_consensus_and_dispersion(
        pregame, config.min_books_for_consensus, config.min_books_for_consensus_totals,
        method=config.devig_method, outlier_points=config.consensus_outlier_points,
        # EV#2: sharp-book weighting is opt-in (parallel/measurement) until it beats the median on CLV.
        sharp_books=config.sharp_books if config.weight_sharp_books else None,
        sharp_weight=config.sharp_book_weight,
        book_correlation=float(config.ev_dispersion_book_correlation))  # EV#3/B6
    # EV#6: stamp each consensus row with the books' median line-move time, not the scan clock.
    line_times = compute_line_times(pregame, config.min_books_for_consensus,
                                    config.min_books_for_consensus_totals,
                                    require_move_time=config.ev_require_move_time)  # C16
    print(f"Consensus outcomes computed: {len(consensus)} (from {len(pregame)} pregame quotes)")

    if args.dry_run:
        print("Scan dry run complete; quotes and consensus were not persisted.")
        return 1 if aborted else 0

    poll_id = store.start_poll("scan")
    if aborted:
        # E4: an aborted/4xx scan has a partial-coverage quote set. `save_consensus` upserts with
        # ON CONFLICT DO UPDATE, so writing this poll's consensus would OVERWRITE complete prior
        # consensus for those events with a thinner one, and EV would then price against a biased
        # p̂. Persist the raw quotes (rule #7) but SKIP the consensus write entirely — the prior
        # complete consensus stays intact and the next clean scan refreshes it.
        store.save_scan_results(poll_id, quotes, {}, computed_at, None,
                                adapter.credits_remaining, len(quotes), 0)
        print(f"Persisted scan poll {poll_id}: {len(quotes)} quotes; consensus write SKIPPED "
              f"(aborted poll — E4 guard, prior consensus preserved).")
        return 1
    store.save_scan_results(poll_id, quotes, consensus, computed_at, dispersion,
                            adapter.credits_remaining, len(quotes), 0,  # pass3#7 atomic
                            outcome_times=line_times)  # EV#6
    print(f"Persisted scan poll {poll_id}: {len(quotes)} quotes, {len(consensus)} consensus outcomes.")
    return 0


def cross_venue_overlap(quotes: list[Quote]) -> tuple[int, int]:
    """(events priced on >=2 venues, total events). Zero overlap with non-trivial quote
    volume means matching/canonicalization broke — the silent failure mode that makes
    cross-venue arbs invisible (the '0 soccer overlap' audit finding)."""
    venues_by_event: dict[str, set[str]] = defaultdict(set)
    for q in quotes:
        venues_by_event[q.event_key].add(q.venue)
    overlap = sum(1 for venues in venues_by_event.values() if len(venues) >= 2)
    return overlap, len(venues_by_event)


def _warn_if_no_cross_venue_overlap(quotes: list[Quote], min_quotes_to_alert: int = 50) -> None:
    overlap, events = cross_venue_overlap(quotes)
    if overlap == 0 and len(quotes) >= min_quotes_to_alert:
        print(f"ALERT: 0 cross-venue overlap across {events} events / {len(quotes)} quotes "
              f"— matching/aliases likely broken; no cross-venue arb can be found this cycle.")


def fetch_all_venues(adapters: list, sports: list[str]) -> tuple[list[Quote], set[str]]:
    """Fetch venues CONCURRENTLY (independent rate limits → sweep time = slowest venue,
    and cross-venue leg skew shrinks). Within one venue stay serial: each venue's
    throttle is a shared budget that parallel workers would only split or forfeit.

    Returns (quotes, failed_venues). K3: a venue that errored or returned ZERO quotes did
    not answer — the tracker must not absent-close fingerprints whose legs touch it
    ("venue outage" is not "gone from the book")."""

    def venue_fetch(adapter) -> tuple[list[Quote], bool]:
        collected: list[Quote] = []
        errored = False
        for sport in sports:
            try:
                venue_quotes = adapter.fetch_quotes(sport)
            except FileNotFoundError:
                continue
            except (RuntimeError, requests.RequestException) as exc:
                # One venue/sport failing must not kill the loop; the cycle continues
                # with whatever quotes the other venues produced.
                print(f"{adapter.name} ({sport}): {exc}")
                errored = True
                continue
            collected.extend(venue_quotes)
            print(f"{sport} {adapter.name}: {len(venue_quotes)} quotes")
        return collected, errored

    with ThreadPoolExecutor(max_workers=len(adapters)) as pool:
        results = list(pool.map(venue_fetch, adapters))
    failed = {adapter.name for adapter, (venue_quotes, errored) in zip(adapters, results)
              if errored or not venue_quotes}
    return [quote for venue_quotes, _ in results for quote in venue_quotes], failed


def split_pregame(quotes: list[Quote], now: datetime,
                  unreliable_start_venues: dict[str, list[str]] | None = None) -> tuple[list[Quote], list[Quote]]:
    """SPEC §1: pregame always. Started events are persisted live=1, never detected on.

    In-play books (and in-play sportsbook odds) priced against pregame consensus are the
    fastest source of absurd fake edges; break-window re-scans come later and stay live=1.

    Part 1.0: routes on the EVENT-level start (earliest reliable venue), not each quote's own
    start — so a nominal/late Polymarket tennis slot can't keep an already-live event in the
    pregame lane when Kalshi/SX already show it started.
    """
    starts = event_start_map(quotes, unreliable_start_venues)
    pregame: list[Quote] = []
    in_play: list[Quote] = []
    for quote in quotes:
        event_start = starts.get(quote.event_key)
        # Pregame requires BOTH: the event's earliest reliable start is still future (so no venue
        # already shows it live — the tennis fix), AND this quote's own start is known (E5: a
        # null-start quote can never be verified against a time guard, so it stays non-actionable).
        if event_start is not None and event_start > now and quote.start_utc is not None:
            pregame.append(quote)
        else:
            # E5: `start_utc is None` means the start is unknown — no time guard can protect it,
            # so it must NOT be actionable. Treat it exactly like an already-started event:
            # route to the non-actionable lane (live=1, persisted for research, never detected
            # on, never enters consensus). Only a known future start is pregame.
            in_play.append(quote if quote.live else replace(quote, live=True))
    return pregame, in_play


def split_depth_lanes(quotes: list[Quote], config) -> tuple[list[Quote], list[Quote], list[Quote]]:
    """Return normal-depth, micro-depth, and too-thin quotes.

    Unknown depth remains in the normal lane because some adapters cannot expose book
    depth reliably. Known thin depth is treated conservatively: only EV can use the
    micro lane, and only above the configured minimum.
    """
    normal: list[Quote] = []
    micro: list[Quote] = []
    too_thin: list[Quote] = []
    assume_thin = config.micro_assume_thin_venues
    for quote in quotes:
        if quote.depth_usd is None:
            # M1: a venue that can't report depth (SX; sometimes Polymarket) is genuinely thin —
            # route its unknown-depth quotes to micro (conservative, depth-capped stake) instead
            # of the normal lane, where they'd be priced as if executable at full size.
            (micro if config.micro_enabled and quote.venue in assume_thin else normal).append(quote)
        elif quote.depth_usd >= config.min_executable_usd:
            normal.append(quote)
        elif config.micro_enabled and quote.depth_usd >= config.micro_min_depth_usd:
            micro.append(quote)
        else:
            too_thin.append(quote)
    return normal, micro, too_thin


def compute_consensus(quotes: list[Quote], min_books: int, min_books_totals: int | None = None,
                      method: str = "proportional", outlier_points: Decimal | None = None,
                      sharp_books=None, sharp_weight: int = 2) -> dict[tuple[str, str], Decimal]:
    by_event: dict[str, list[Quote]] = defaultdict(list)
    for quote in quotes:
        by_event[quote.event_key].append(quote)
    consensus: dict[tuple[str, str], Decimal] = {}
    for event_key, event_quotes in by_event.items():
        for outcome, p_hat in consensus_probabilities(event_quotes, min_books, min_books_totals,
                                                       method=method, outlier_points=outlier_points,
                                                       sharp_books=sharp_books, sharp_weight=sharp_weight).items():
            consensus[(event_key, outcome)] = p_hat
    return consensus


def _cheapest_quote_per_outcome(quotes: list[Quote]) -> list[Quote]:
    """Keep the single cheapest-effective-cost quote per (event_key, outcome), so two venues
    under fair value on the same opinion become one EV bet, not two (#2)."""
    best: dict[tuple[str, str], Quote] = {}
    passthrough: list[Quote] = []
    for q in quotes:
        if q.venue == "oddsapi":
            passthrough.append(q)
            continue
        key = (q.event_key, consensus_outcome_key(q.market_type, q.line, q.outcome_label))
        current = best.get(key)
        if current is None or q.effective_cost < current.effective_cost:
            best[key] = q
    return [*best.values(), *passthrough]


def compute_dispersion(quotes: list[Quote], min_books: int, method: str = "proportional",
                       book_correlation: float = 0.0) -> dict[tuple[str, str], Decimal]:
    """EV#3: per (event, outcome) cross-book dispersion of de-vigged probs, stored beside p_hat.
    B6: `book_correlation` widens the SE for non-independent books (n_eff correction)."""
    by_event: dict[str, list[Quote]] = defaultdict(list)
    for quote in quotes:
        by_event[quote.event_key].append(quote)
    out: dict[tuple[str, str], Decimal] = {}
    for event_key, eq in by_event.items():
        for outcome, disp in consensus_dispersion(eq, min_books, method=method,
                                                   book_correlation=book_correlation).items():
            out[(event_key, outcome)] = disp
    return out


def compute_consensus_and_dispersion(quotes: list[Quote], min_books: int, min_books_totals: int | None,
                                     method: str, outlier_points: Decimal | None, sharp_books,
                                     sharp_weight: int, book_correlation: float
                                     ) -> tuple[dict[tuple[str, str], Decimal], dict[tuple[str, str], Decimal]]:
    """D19: compute consensus probabilities and dispersion together, sharing ONE de-vig pass per
    event (the scan used to de-vig every book twice). Output equals compute_consensus +
    compute_dispersion called separately (pricing.consensus_all is equivalence-tested)."""
    by_event: dict[str, list[Quote]] = defaultdict(list)
    for quote in quotes:
        by_event[quote.event_key].append(quote)
    consensus: dict[tuple[str, str], Decimal] = {}
    dispersion: dict[tuple[str, str], Decimal] = {}
    for event_key, eq in by_event.items():
        probs, disp = consensus_all(eq, min_books, min_books_totals, method=method,
                                    outlier_points=outlier_points, sharp_books=sharp_books,
                                    sharp_weight=sharp_weight, book_correlation=book_correlation)
        for outcome, p_hat in probs.items():
            consensus[(event_key, outcome)] = p_hat
        for outcome, d in disp.items():
            dispersion[(event_key, outcome)] = d
    return consensus, dispersion


def compute_line_times(quotes: list[Quote], min_books: int,
                       min_books_totals: int | None = None,
                       require_move_time: bool = False) -> dict[tuple[str, str], datetime]:
    """EV#6: per (event, outcome) median book line-move time, used to stamp consensus age.
    C16: `require_move_time` excludes books without a real move time (don't mix clocks)."""
    by_event: dict[str, list[Quote]] = defaultdict(list)
    for quote in quotes:
        by_event[quote.event_key].append(quote)
    out: dict[tuple[str, str], datetime] = {}
    for event_key, eq in by_event.items():
        for outcome, ts in consensus_outcome_times(eq, min_books, min_books_totals,
                                                    require_move_time=require_move_time).items():
            out[(event_key, outcome)] = ts
    return out


def _left_on_table(sink: list[dict] | None, reason: str, quote: Quote,
                   outcome: str, edge: Decimal | None) -> None:
    """golf_slate_tennis_fix Part 1: record an EV candidate that was deliberately NOT booked so
    it is never a silent discard — appended to the poll's left-on-table sink (surfaced in the
    daily report by reason) and logged to logs/unmatched.log."""
    if sink is not None:
        sink.append({"reason": reason, "sport": quote.sport, "event_key": quote.event_key,
                     "venue": quote.venue, "outcome": outcome or quote.outcome_label,
                     "edge": edge})
    try:
        Path("logs").mkdir(exist_ok=True)
        with open("logs/unmatched.log", "a") as handle:
            handle.write(f"{datetime.now(timezone.utc).isoformat()}\t{reason}\t{quote.sport}\t"
                         f"{quote.event_key}\t{quote.venue}\t{outcome or quote.outcome_label!r}\t"
                         f"edge={edge}\n")
    except OSError:  # logging must never break a scan cycle
        pass


def detect_ev_from_consensus(
    quotes: list[Quote],
    consensus: dict[tuple[str, str], tuple[Decimal, datetime]],
    max_age_hours: int,
    trusted_pairs: set[str],
    e_min: Decimal | None = None,
    config=None,
    record_below_e_min: bool = True,
    pregame_max_age_hours: int | None = None,
    calibrator: tuple[list[float], list[float]] | None = None,
    dispersion: dict[tuple[str, str], Decimal] | None = None,
    left_on_table: list[dict] | None = None,
    enforce_max_edge: bool = True,
) -> list[Opportunity]:
    now = datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(hours=max_age_hours)
    # golf_slate_tennis_fix Part 1: EV correctness gates. Suppressed candidates are logged and
    # pushed to the `left_on_table` sink (append-only, surfaced in the daily report by reason).
    require_exact = set(getattr(config, "ev_require_exact_match", []) or [])
    # The edge ceiling gates the NORMAL executable lane only: the micro lane deliberately trades
    # larger edges on thin, depth-capped books (its e_min_thin floor is ~the ceiling), so applying
    # it there would erase the whole lane. Micro keeps its own floors + edge_suspect_above.
    max_edge = (getattr(config, "ev_max_edge", None) if config is not None else None) if enforce_max_edge else None
    aliases = load_aliases()
    known_labels = {sport: set(m.keys()) | set(m.values()) for sport, m in aliases.items()}
    # #3 Pregame lines move well inside the 8h tag window, so a watch flag against an
    # hours-old consensus is "venue vs stale line", not "venue vs sharp line". Use a tighter
    # staleness window for pregame (in-play quotes keep the looser one).
    pregame_cutoff = now - timedelta(hours=pregame_max_age_hours) if pregame_max_age_hours else stale_cutoff
    # #2 One stake per opinion: if several venues are under fair value on the SAME outcome,
    # keep only the cheapest (best-EV) quote, mirroring the closing recorder. Otherwise the
    # high-frequency path books N $1 bets on one view and over-counts edge/exposure.
    quotes = _cheapest_quote_per_outcome(quotes)
    opportunities: list[Opportunity] = []
    for quote in quotes:
        if quote.venue == "oddsapi":
            continue
        # Part 1.1: an outcome that never normalized to a known label can't be joined to consensus
        # safely — persist the quote (done elsewhere) but never make it an EV bet.
        if not (quote.outcome_label or "").strip():
            _left_on_table(left_on_table, "outcome-unnormalized", quote, "", None)
            continue
        outcome = consensus_outcome_key(quote.market_type, quote.line, quote.outcome_label)
        entry = consensus.get((quote.event_key, outcome))
        if entry is None:
            continue
        # Part 1.2: surname/player-matched sports (tennis/mma/golf) only book EV when the outcome
        # resolves through an EXPLICIT alias entry — a fuzzy/unregistered WINNER label may still be
        # a DIVERGENCE (separate path) but never an EV bet. Totals (OVER/UNDER) are always exact.
        if quote.market_type == "WINNER" and quote.sport in require_exact \
                and quote.outcome_label not in known_labels.get(quote.sport, set()):
            _left_on_table(left_on_table, "fuzzy-no-exact-alias", quote, outcome, None)
            continue
        p_hat, computed_at = entry
        # EV#4: a zero/negative effective cost (malformed or fee-credited quote) makes the EV
        # division blow up and would raise out of the whole scan/watch cycle. Skip it instead.
        if quote.effective_cost <= 0:
            continue
        # EV#5: apply the measured FLB calibration to p_hat before EV (the curve IS a p_hat
        # correction). Off unless a calibrator is supplied; identity otherwise.
        calibrated = False
        calib_slope = Decimal("1")  # EV#7: local slope of the calibration curve at p_hat
        if calibrator is not None:
            from .bias import calibrate_probability
            p_raw = float(p_hat)
            adj = q6(Decimal(str(calibrate_probability(p_raw, calibrator))))
            # EV#7: dispersion is measured in RAW prob space; the calibration is a monotone map,
            # so a spread around p_hat transforms by the curve's local slope. Carry that slope so
            # the edge (calibrated) and the SE (below) live in the same units.
            h = 1e-4
            lo = calibrate_probability(max(0.0, p_raw - h), calibrator)
            hi = calibrate_probability(min(1.0, p_raw + h), calibrator)
            calib_slope = q6(Decimal(str(abs(hi - lo) / (2 * h))))
            calibrated = adj != p_hat
            p_hat = adj
        # B7/K7: shrink a thin-consensus p_hat toward the prior. Two explicit knobs (prior sd
        # vs pseudo-count); config load guarantees at most one is set. Both 0 = identity.
        shrunk = False
        shrink_sd = getattr(config, "ev_shrink_prior_sd", Decimal("0")) if config is not None else Decimal("0")
        shrink_pseudo = getattr(config, "ev_shrink_prior_pseudocount", Decimal("0")) if config is not None else Decimal("0")
        if shrink_sd > 0 or shrink_pseudo > 0:
            from .pricing import shrink_probability
            raw_disp = (dispersion or {}).get((quote.event_key, outcome), Decimal("0"))
            p_shrunk = shrink_probability(p_hat, 0, raw_disp, config.ev_shrink_prior,
                                          prior_sd=float(shrink_sd),
                                          prior_pseudocount=float(shrink_pseudo))
            shrunk = p_shrunk != p_hat
            p_hat = p_shrunk
        ev = expected_value(p_hat, quote.effective_cost)
        # SPEC §1: every EV > 0 is recorded; thresholds gate display/betting, not recording.
        if ev <= 0:
            continue
        status, notes = status_for_legs([quote], trusted_pairs)
        edge = edge_points(p_hat, quote.effective_cost)
        # Part 1.3: an EV edge above the ceiling is a data error (stale/nominal start, mis-joined
        # consensus), not an opportunity (SPEC §12 trap). Record it (left-on-table) but book no bet.
        if max_edge is not None and edge > max_edge:
            _left_on_table(left_on_table, "edge-implausible", quote, outcome, edge)
            continue
        # B11: integer totals can PUSH (stake returned) — scale the edge by (1-push_prob) instead
        # of pricing it as a clean binary. Off unless push_pricing is on; non-integer lines no-op.
        if config is not None and getattr(config, "ev_push_pricing", False) \
                and quote.market_type == "TOTAL" and quote.line is not None:
            from .pricing import push_adjusted_edge, push_probability_poisson
            push_prob = push_probability_poisson(quote.line)
            if push_prob > 0:
                edge = push_adjusted_edge(edge, push_prob)
                ev = q6(edge / quote.effective_cost)
                notes = (*notes, f"push-priced:{push_prob}")
        notes = (*notes, f"edge_pts:{edge}")
        if calibrated:
            notes = (*notes, "p_hat-calibrated")
        if shrunk:
            notes = (*notes, "p_hat-shrunk")
        # EV#7: decay edge continuously by consensus age (pregame info decays smoothly, not as
        # a step). The decayed edge — not the raw one — is what the e_min gate sees.
        gate_edge = edge
        decay_rate = getattr(config, "ev_edge_decay_per_hour", Decimal("0")) if config is not None else Decimal("0")
        if decay_rate > 0:
            age_hours = Decimal(str(max(0.0, (datetime.now(timezone.utc) - computed_at).total_seconds() / 3600.0)))
            gate_edge = max(Decimal("0"), q6(edge - decay_rate * age_hours))
            if gate_edge < edge:
                notes = (*notes, f"edge_decayed:{gate_edge}")
        # EV#7 (decay consistency): the decayed edge is the EFFECTIVE edge for everything
        # downstream — stored edge, ratio, capturable, and Kelly — not just the e_min gate.
        # Under the default edge_decay_per_hour=0, eff_edge == edge and behavior is unchanged.
        eff_edge = gate_edge
        eff_ev = q6(eff_edge / quote.effective_cost)
        below_e_min = e_min is not None and gate_edge < e_min
        if below_e_min and not record_below_e_min:
            continue
        if below_e_min:
            notes = (*notes, "below-e-min")
        # EV_SPEC §4: EV's own killer — consensus event vs contract event may differ.
        status = "REVIEW"
        notes = (*notes, "event-match-review")
        # EV#5 (window): stored consensus is always pregame (scan never lets in-play odds
        # become consensus), so an in-play quote compared to it should be flagged stale
        # SOONER, not later. Use the tighter pregame window regardless of quote.live.
        is_stale = computed_at < pregame_cutoff
        if is_stale:
            notes = (*notes, "stale-consensus")
        # Phase F: stamp the consensus age (secs) on every EV row, so the edge-bucket report can
        # add an age dimension and consensus_max_age_hours stops being a guess.
        consensus_age_secs = int(max(0.0, (now - computed_at).total_seconds()))
        notes = (*notes, f"consensus_age_secs:{consensus_age_secs}")
        if quote.depth_usd is not None:
            notes = (*notes, f"capturable:{q6(quote.depth_usd * eff_edge)}")
        # EV#3: consensus uncertainty. The cross-book dispersion is an SE proxy. Gate on
        # edge/SE (a 5-pt edge from disagreeing books is weaker than from tight ones) and size
        # Kelly off the LOWER bound of fair value (p_hat - haircut*dispersion). Off by default.
        disp = (dispersion or {}).get((quote.event_key, outcome), Decimal("0"))
        # EV#7: map the raw-space SE into calibrated space so edge/SE is dimensionally honest.
        if calibrator is not None and disp > 0:
            disp = q6(disp * calib_slope)
        # Size Kelly off the decayed fair value (cost + eff_edge), so staleness shrinks the
        # stake too. Under no decay this equals p_hat; capped at p_hat so it never inflates.
        p_for_kelly = min(p_hat, q6(quote.effective_cost + eff_edge))
        low_edge_se = False
        if config is not None and getattr(config, "ev_dispersion_gate", False) and disp > 0:
            ratio = gate_edge / disp
            notes = (*notes, f"edge_se:{q6(ratio)}")
            if ratio < config.ev_edge_se_min:
                low_edge_se = True
            haircut = config.ev_dispersion_kelly_haircut * disp
            p_for_kelly = max(Decimal("0"), q6(p_for_kelly - haircut))
        opp = Opportunity(
            kind="EV",
            category="sports",
            event=build_event(quote.event_key, [quote]),
            legs=[quote],
            edge=eff_ev,
            edge_points=eff_edge,  # #9 p-c (decay-adjusted), comparable across kinds
            p_hat=p_hat,  # K14: the RAW model estimate — calibration judges this, not a
                          # p̂ reconstructed from the push/decay-haircut edge
            stakes={quote.outcome_label: Decimal("1.00")},
            status=status,  # type: ignore[arg-type]
            notes=notes,
        )
        if config is not None:
            opp = _annotate_quality_and_kelly(opp, p_hat, quote.effective_cost, config, p_for_kelly)
        # #5/#3 First-class quarantine: sub-threshold and stale-line EV must not sit in the
        # CLEAN aggregate keyed on quality_flag IS NULL. Don't override a more specific flag.
        if opp.quality_flag is None:
            if below_e_min:
                opp = replace(opp, quality_flag="below-e-min")
            elif is_stale:
                opp = replace(opp, quality_flag="stale-consensus")
            elif low_edge_se:
                opp = replace(opp, quality_flag="low-edge-se")
        opportunities.append(opp)
    return opportunities


def detect_micro_ev_from_consensus(
    quotes: list[Quote],
    consensus: dict[tuple[str, str], tuple[Decimal, datetime]],
    config,
) -> list[Opportunity]:
    """Detect practical micro EV on thin books without admitting thin ARB risk.

    Micro EV has three extra constraints:
    1. quote depth already passed the micro floor in split_depth_lanes;
    2. edge must clear the stricter micro_e_min, not merely be positive;
    3. recorded stake is capped to visible depth, configured micro cap, and bankroll cap.
    """
    if not config.micro_enabled or not quotes:
        return []
    raw = detect_ev_from_consensus(
        quotes,
        consensus,
        max_age_hours=config.consensus_max_age_hours,
        trusted_pairs=config.trusted_pairs,
        e_min=config.micro_e_min,
        config=config,
        record_below_e_min=False,
        enforce_max_edge=False,  # micro floors overlap the ceiling; micro has its own controls
    )
    # M2: require the micro edge to survive the WALKED fill cost — drop the artifacts.
    marked = [m for opp in raw if (m := _mark_micro_ev(opp, config)) is not None]
    return _cap_micro_exposure(marked, config)


def _cap_micro_exposure(opps: list[Opportunity], config) -> list[Opportunity]:
    """M4 — bound the micro lane's exposure. Many micro quotes are correlated thin books on the
    same game, so without a cap the lane could book ten $25 bets on one opinion. Keep the
    highest-edge micro bets first, then drop any that would push a single event_key or venue past
    its configured ceiling (the dropped ones are simply not recorded as micro bets)."""
    event_cap = config.micro_max_event_exposure_usd
    venue_cap = config.micro_max_venue_exposure_usd
    event_used: dict[str, Decimal] = defaultdict(Decimal)
    venue_used: dict[str, Decimal] = defaultdict(Decimal)
    kept: list[Opportunity] = []
    for opp in sorted(opps, key=lambda o: o.edge_points or Decimal("0"), reverse=True):
        leg = opp.legs[0]
        stake = opp.stakes.get(leg.outcome_label, Decimal("0"))
        if event_used[leg.event_key] + stake > event_cap or venue_used[leg.venue] + stake > venue_cap:
            continue
        event_used[leg.event_key] += stake
        venue_used[leg.venue] += stake
        kept.append(opp)
    return kept


def _micro_e_min_for_depth(depth: Decimal | None, config) -> Decimal:
    """M6 — depth-tied micro edge floor. e_min at the TOP of the band (depth >= min_executable),
    e_min_thin at the bottom (depth <= micro_min_depth), linear between. Unknown depth → the
    conservative thin floor."""
    top, thin = config.micro_e_min, config.micro_e_min_thin
    lo, hi = config.micro_min_depth_usd, config.min_executable_usd
    if depth is None:
        return thin
    if depth >= hi:
        return top
    if depth <= lo or hi <= lo:
        return thin
    frac = (Decimal(str(depth)) - lo) / (hi - lo)   # 0 at lo, 1 at hi
    return q6(thin - (thin - top) * frac)


def _mark_micro_ev(opp: Opportunity, config) -> Opportunity | None:
    quote = opp.legs[0]
    bankroll_cap = config.bankroll_usd * config.max_stake_pct
    depth_cap = quote.depth_usd if quote.depth_usd is not None else config.micro_max_stake_usd
    stake = money(min(depth_cap, config.micro_max_stake_usd, bankroll_cap))
    # All micro floors are in PROBABILITY POINTS (p - c), matching the e_min gate in
    # detect_ev_from_consensus; opp.edge is the EV ratio kept only for display.
    top_eff = quote.effective_cost
    edge_pts = opp.edge_points if opp.edge_points is not None else q6(top_eff * opp.edge)
    p_hat = q6(top_eff + edge_pts)
    edge_ratio = opp.edge
    extra_notes: tuple[str, ...] = ()
    # M2/M3: a $5–$50 book cannot fill a $25 stake at the top tick. Walk the captured ladder for
    # the intended micro stake to get the volume-weighted fill cost, add a depth-scaled micro
    # buffer (slippage that grows with stake/depth, floored at the latency buffer), and recompute
    # the edge at THAT cost. A micro "edge" priced at the $1-of-depth top tick is usually a mirage.
    ladder = list(quote.ladder) if quote.ladder else []
    if ladder and stake > 0:
        base_buffer = config.taker_price_buffer
        vwap, _filled = walk_ladder(ladder, stake)
        micro_buffer = depth_scaled_buffer(ladder, stake, base_buffer)
        fee_only = max(Decimal("0"), quote.fee_cost - base_buffer)  # fee minus the baked flat buffer
        micro_fill_cost = q6(vwap + fee_only + micro_buffer)
        edge_pts = q6(p_hat - micro_fill_cost)
        if micro_fill_cost > 0:
            edge_ratio = q6(edge_pts / micro_fill_cost)
        extra_notes = (
            f"micro-fill-cost:{micro_fill_cost}",
            f"micro-walked-edge-pts:{edge_pts}",
            f"micro-buffer:{micro_buffer}",
        )
    # M6: tie the edge floor to depth — thinner = riskier = needs more cushion. e_min(points) is
    # the floor at the top of the band; e_min_thin at $5. The (walked) edge must clear it or drop.
    floor = _micro_e_min_for_depth(quote.depth_usd, config)
    if edge_pts < floor:
        return None
    extra_notes = (*extra_notes, f"micro-e-min-pts:{floor}")
    notes = (
        *opp.notes,
        "micro-depth-sized",
        "micro-ev-only",
        f"micro-depth-usd:{q6(depth_cap)}",
        f"micro-stake-usd:{stake}",
        *extra_notes,
    )
    return replace(
        opp,
        edge=edge_ratio,
        edge_points=edge_pts,
        stakes={quote.outcome_label: stake},
        notes=notes,
        quality_flag="micro",
        kelly_stake=stake if opp.kelly_stake is None else min(opp.kelly_stake, stake),
    )


def _annotate_quality_and_kelly(opp: Opportunity, p_hat: Decimal, effective_cost: Decimal, config,
                                p_for_kelly: Decimal | None = None) -> Opportunity:
    """Attach Phase 1 quality flag and Phase 3 Kelly projection to an EV opportunity. Kelly is
    sized off p_for_kelly (the lower bound of fair value when EV#3 dispersion sizing is on),
    defaulting to the point estimate p_hat."""
    quality = classify_quality(opp, config.min_executable_usd, config.ev_edge_suspect_above)
    kelly_p = p_for_kelly if p_for_kelly is not None else p_hat
    f_star = kelly_f_star(kelly_p, effective_cost)
    stake = kelly_stake(kelly_p, effective_cost, config.sizing_kelly_fraction,
                        config.bankroll_usd, config.sizing_max_fraction)
    notes = opp.notes
    # pass3#4: EV = (p-c)/c assumes a clean binary, but an integer-line total can PUSH (total
    # == line -> stake returned, no payout), which lowers true EV. We have no run-distribution
    # model to price the push, so flag it rather than overstate EV.
    leg0 = opp.legs[0] if opp.legs else None
    if leg0 is not None and leg0.market_type == "TOTAL" and leg0.line is not None and leg0.line == leg0.line.to_integral_value():
        notes = (*notes, "push-risk-integer-total")
        quality = quality or "push-risk"
    # pass3#6/weakness#8: a stale/unverified venue fee silently shifts every edge, and fees
    # are first-order at sub-4% edges. Quarantine bets whose venue fee is past fee_stale_days.
    if any(leg.venue in config.stale_fee_venues for leg in opp.legs):
        notes = (*notes, "stale-fee")
        quality = quality or "stale-fee"
    # pass3#3: EV ties up the $1 until settlement (up to max_days_to_settlement), unlike an
    # arb that locks at entry. Gate on edge-PER-DAY-held: if the expected return doesn't beat
    # the capital charge over the holding period, quarantine it (uneconomic-horizon).
    if opp.event.start_utc is not None:
        # B12: capital is locked until SETTLEMENT, not first pitch. Add the configured settlement
        # lag so the holding-period charge reflects when the money actually frees up.
        lag_hours = float(getattr(config, "ev_settlement_lag_hours", Decimal("0")))
        settle_at = opp.event.start_utc + timedelta(hours=lag_hours)
        days = max(0.0, (settle_at - datetime.now(timezone.utc)).total_seconds() / 86400.0)
        if not is_economic(opp.edge, days, config.capital_charge_annual):
            notes = (*notes, f"uneconomic-horizon:{days:.1f}d")
            quality = quality or "uneconomic-horizon"
    # EV#3 (ratio hazard): the headline EV is a per-dollar ROI, so the uneconomic-horizon gate
    # above can pass a tiny-notional longshot on rate alone. Add an ABSOLUTE expected-profit
    # floor: stake * edge_points must clear min_expected_profit_usd. 0 = off (default).
    floor = getattr(config, "ev_min_expected_profit_usd", Decimal("0"))
    if floor > 0:
        expected_profit = q6(stake * opp.edge_points)
        notes = (*notes, f"exp-profit-usd:{expected_profit}")
        if expected_profit < floor:
            quality = quality or "below-min-profit"
    return replace(opp, quality_flag=quality, kelly_f_star=f_star, kelly_stake=stake, notes=notes)


def apply_segment_gates(opportunities: list[Opportunity], store: Store, config) -> list[Opportunity]:
    """Phase 2/3 gate: blocked segments stay logged, but not eligible for headline/Kelly.

    The scorer is intentionally conservative. Missing score data is INSUFFICIENT_DATA:
    the row still enters the measurement ledger, but directional Kelly allocation is zero.
    """
    if not opportunities:
        return []
    from .scoring import BLOCKED, INSUFFICIENT, score_segments

    scores = {score.segment: score for score in score_segments(store, config.scoring)}
    gated: list[Opportunity] = []
    for opp in opportunities:
        segment = f"{opp.event.sport or 'unknown'}:{opp.kind}"
        score = scores.get(segment)
        tier = score.tier if score is not None else INSUFFICIENT
        notes = list(opp.notes)
        quality = opp.quality_flag
        status = opp.status
        kelly_stake_value = opp.kelly_stake
        if tier == BLOCKED:
            if "segment-blocked" not in notes:
                notes.append("segment-blocked")
            quality = quality or "segment-blocked"
            status = "REVIEW"
            if kelly_stake_value is not None:
                kelly_stake_value = Decimal("0.00")
        elif tier == INSUFFICIENT:
            if "segment-insufficient-data" not in notes:
                notes.append("segment-insufficient-data")
            if kelly_stake_value is not None:
                kelly_stake_value = Decimal("0.00")
        gated.append(replace(opp, status=status, notes=tuple(notes), quality_flag=quality, kelly_stake=kelly_stake_value))
    return gated


def apply_correlation_sizing(opportunities: list[Opportunity], config) -> list[Opportunity]:
    """B8: correlation-aware Kelly across same-event EV bets. Independent per-bet Kelly over-bets
    correlated outcomes (moneyline + total on one game, both sides of a shared driver); this
    haircuts and caps the joint stake within each event group. Off (rho 0 / cap 0) -> unchanged."""
    rho = getattr(config, "ev_correlation_rho", Decimal("0"))
    cap = getattr(config, "ev_correlation_group_cap_fraction", Decimal("0"))
    if (rho <= 0 and cap <= 0) or not opportunities:
        return opportunities
    from .sizing import group_correlated_stakes
    ev_opps = [o for o in opportunities if o.kind == "EV" and o.kelly_stake is not None]
    if len(ev_opps) < 2:
        return opportunities
    adjusted = group_correlated_stakes(ev_opps, rho, cap, config.bankroll_usd)
    out: list[Opportunity] = []
    for opp in opportunities:
        new_stake = adjusted.get(id(opp))
        if new_stake is not None and new_stake != opp.kelly_stake:
            out.append(replace(opp, kelly_stake=new_stake,
                               notes=(*opp.notes, f"corr-sized:{new_stake}")))
        else:
            out.append(opp)
    return out


def apply_opportunity_filters(opportunities: list[Opportunity], config) -> tuple[list[Opportunity], list[Opportunity]]:
    """SPEC §1 step 3: depth and settlement-horizon filters; filtered flags are counted, not bet."""
    horizon = datetime.now(timezone.utc) + timedelta(days=config.max_days_to_settlement)
    kept: list[Opportunity] = []
    left: list[Opportunity] = []
    for opportunity in opportunities:
        ok = True
        is_micro = opportunity.kind == "EV" and opportunity.quality_flag == "micro"
        for leg in opportunity.legs:
            if leg.depth_usd is not None and leg.depth_usd < config.min_executable_usd and not is_micro:
                ok = False
            if is_micro and (
                leg.depth_usd is None
                or leg.depth_usd < config.micro_min_depth_usd
                or leg.depth_usd >= config.min_executable_usd
            ):
                ok = False
            if leg.start_utc is not None and leg.start_utc > horizon:
                ok = False
        (kept if ok else left).append(opportunity)
    return kept, left


def run_annotate(args: argparse.Namespace, store: Store) -> int:
    """Void bad flat_bets rows without editing them, via the existing append-only `annotations`
    table (which every honest-variant report reader already excludes). Previews the matched set;
    writes only with --yes. Idempotent: rows already annotated are skipped (stop_inplay_betting_fix)."""
    from .experimental.annotations import annotate, annotated_bet_ids

    try:
        rows = store.select_bets_where(args.where)
    except Exception as exc:  # noqa: BLE001 - a bad predicate must report, not crash.
        print(f"annotate: invalid --where predicate: {exc}")
        return 1
    print(f"annotate --reason {args.reason} matched {len(rows)} flat_bets row(s):")
    for row in rows:
        print(f"  id={row['id']} {row['sport']}/{row['kind']} {row['status']} "
              f"placed={row['placed_at']} edge={row['edge']}")
    if not rows:
        return 0
    if not args.yes:
        print(f"(dry run — pass --yes to write {len(rows)} annotation(s))")
        return 0
    already = annotated_bet_ids(store)
    reason = args.notes or f"voided via `arb annotate --reason {args.reason}`"
    written = 0
    for row in rows:
        if int(row["id"]) in already:
            continue
        annotate(store, int(row["id"]), tag=args.reason, reason=reason)
        written += 1
    print(f"Voided {written} new bet(s) (reason={args.reason}); {len(rows) - written} already "
          f"annotated. They now report only under 'voided — data error', excluded from all P&L.")
    return 0


def run_settle_job(config, store: Store) -> None:
    # Each source is isolated so one failing never blocks the other (the old failure mode:
    # a silent settle error let bets age past the Odds API window and orphan forever).
    try:
        settled = settle_sports(store, config)
        print(f"Daily settlement (Odds API): {settled} flat bets resolved.")
    except Exception as exc:  # noqa: BLE001
        print(f"settle: Odds API settlement failed: {exc}")
    try:
        backfilled = settle_backfill_mlb(store, tripwire_secs=config.settle_tripwire_secs)
        print(f"Daily settlement (MLB Stats backfill): {backfilled} flat bets resolved.")
    except Exception as exc:  # noqa: BLE001
        print(f"settle: MLB Stats backfill failed: {exc}")
    try:
        from .reconcile import default_resolvers, reconcile_settlements

        counts = reconcile_settlements(store, default_resolvers())
        print(f"Daily reconciliation (venue-actual vs lock): {counts}")
    except Exception as exc:  # noqa: BLE001
        print(f"settle: reconciliation failed: {exc}")
    # 02: run the invariant scan at the end of every settle cycle. CRITICAL log, never blocks.
    try:
        from .invariants import check_invariants, format_invariants
        print(format_invariants(check_invariants(
            store, config.settle_tripwire_secs, config.tracker_max_reappearances)))
    except Exception as exc:  # noqa: BLE001
        print(f"settle: check-invariants failed (settle still stands): {exc}")


def run_daemon(config, store: Store) -> int:
    try:
        import schedule
    except ModuleNotFoundError:
        raise RuntimeError("schedule is required for daemon mode. Run `pip install -r requirements.txt`.")
    scan_args = argparse.Namespace(dry_run=False, fixtures=False, sport=None)
    watch_args = argparse.Namespace(once=True, dry_run=False, fixtures=False, sport=None)
    # schedule fires on process-local time; the host is assumed to run in config.timezone.
    for scan_time in config.scan_times:
        schedule.every().day.at(scan_time).do(run_scan, args=scan_args, config=config, store=store)
    schedule.every().day.at(config.settle_time).do(run_settle_job, config=config, store=store)
    schedule.every().day.at(config.daily_report_time).do(write_daily_report, store=store)
    start_state = write_daemon_meta("daemon")
    warned: set[tuple] = set()
    print(
        f"Daemon: watch every {config.watch_interval_secs}s, scans at {', '.join(config.scan_times)}, "
        f"settle at {config.settle_time}, daily report at {config.daily_report_time} ({config.timezone})."
    )
    while True:
        # The daemon must outlive any single failed cycle (network blips, venue outages).
        try:
            warning = code_change_warning(start_state)
            if warning and warning not in warned:
                print(f"WARNING: {warning}")
                warned.add(warning)
            schedule.run_pending()
            run_watch_once(watch_args, config, store)
            # MLB Stats settlement is free and window-less, so settle finals every cycle —
            # this is what stops bets aging out and orphaning between daily settle runs.
            try:
                n = settle_backfill_mlb(store, tripwire_secs=config.settle_tripwire_secs)
                if n:
                    print(f"daemon: settled {n} MLB bets via MLB Stats backfill.")
            except Exception as exc:  # noqa: BLE001
                print(f"daemon: MLB settle failed this cycle: {exc}")
        except Exception as exc:  # noqa: BLE001
            print(f"daemon: cycle failed, retrying next interval: {exc}")
        if stop_requested("daemon"):
            print("daemon: stop flag found, exiting cleanly.")
            return 0
        time.sleep(config.watch_interval_secs)
