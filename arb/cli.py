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

from .arbitrage import build_event, detect_divergences, detect_true_arbs, split_match_suspects, status_for_legs
from .config import load_config
from .models import Opportunity, Quote
from .paper import fill_opportunity
from .pricing import consensus_outcome_key, consensus_probabilities, edge_points, expected_value, is_economic, money, q6
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
    sub.add_parser("settle-health", help="report overdue open EV/ARB bets and last settlement time")
    sub.add_parser("reconcile", help="cross-check settled ARBs against venue-actual resolution (audit fix #1)")
    sub.add_parser("exposure", help="total + per-venue committed capital across active bets (audit fix #3)")
    sub.add_parser("scores", help="per-segment edge scores from settled clean bets (Phase 2)")
    sub.add_parser("backtest-flb", help="favorite-longshot calibration over settled clean bets (S2)")
    sub.add_parser("dashboard", help="regenerate reports/dashboard.json from the ledger (Phase 6)")
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
    if args.command == "reconcile":
        from .reconcile import reconcile_settlements
        from .venues.kalshi import kalshi_leg_result

        counts = reconcile_settlements(store, kalshi_leg_result)
        print(f"Reconciled settled ARBs vs venue-actual resolution: {counts}")
        if counts["corrected"]:
            print(f"  {counts['corrected']} SETTLER-ERROR(s) — realized P&L differed from the lock.")
        if counts["skipped"]:
            print(f"  {counts['skipped']} skipped (a leg's venue resolution not available yet).")
        return 0
    if args.command == "exposure":
        from .exposure import compute_exposure, format_exposure

        per_venue_cap = config.bankroll_usd * config.max_stake_pct
        print(format_exposure(compute_exposure(store, config.bankroll_usd, per_venue_cap)))
        return 0
    if args.command == "scores":
        from .scoring import persist_scores, score_segments

        scores = score_segments(store, config.scoring)
        persist_scores(store, scores)
        print(f"{'SEGMENT':28s} {'N':>4} {'WR':>6} {'ROI':>8} {'SCORE':>6}  TIER")
        for s in scores:
            print(f"{s.segment:28s} {s.n:>4} {s.win_rate:>6.1%} {s.roi:>8.2%} {s.score:>6.1f}  {s.tier}")
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
        return 0
    if args.command == "settle-backfill":
        fixture_dir = Path("tests/fixtures") if args.fixtures else None
        settled = settle_backfill_mlb(store, fixture_dir)
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


def run_watch(args: argparse.Namespace, config, store: Store) -> int:
    while True:
        code = run_watch_once(args, config, store)
        if args.once:
            return code
        if stop_requested("daemon"):
            print("daemon: stop flag found, exiting cleanly.")
            return 0
        time.sleep(config.watch_interval_secs)


def run_watch_once(args: argparse.Namespace, config, store: Store) -> int:
    fixture_dir = Path("tests/fixtures") if args.fixtures else None
    adapters = [
        KalshiAdapter(config, fixture_dir),
        PolymarketAdapter(config, fixture_dir),
        SxAdapter(config, fixture_dir),
    ]
    sports = args.sport or config.sports
    quotes = fetch_all_venues(adapters, sports)
    _warn_if_no_cross_venue_overlap(quotes)

    pregame, in_play = split_pregame(quotes, datetime.now(timezone.utc))
    if in_play:
        print(f"{len(in_play)} in-play quotes persisted with live=1 and excluded from detection (pregame only).")
    # Depth floor BEFORE detection: a derived 1-cent ask backed by $3 of resting size is
    # not a main-lane price. A separate EV-only micro lane can measure small real edges,
    # but thin ARB/divergence stays excluded because partial fills create leg risk.
    detectable, micro_detectable, too_thin = split_depth_lanes(pregame, config)
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
    )
    opportunities.extend(
        detect_divergences(
            detectable, config.divergence_threshold, config.trusted_pairs,
            config.max_leg_skew_secs, config.max_start_drift_mins, config.match_suspect_prob_diff,
        )
    )
    # SPEC §1: the +EV class exists only when consensus_enabled. watch never calls
    # The Odds API; EV uses consensus stored by the scan cadence.
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
            )
        )
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
    print_opportunities(kept, ev_display_threshold=config.ev_threshold)

    if args.dry_run:
        print("Watch dry run complete; quotes and tracker rows were not persisted.")
        return 0

    poll_id = store.start_poll("watch")
    store.save_poll_quotes(poll_id, [*pregame, *in_play])
    store.save_poll_opportunities(poll_id, kept)
    store.save_suspect_matches(poll_id, suspects)
    record_tracker_poll(store, poll_id, kept, load_validation_rule(config))
    if config.auto_fill:
        for opportunity in kept:
            fill_opportunity(store, poll_id, opportunity, config.bankroll_usd, config.max_stake_pct)
    store.finish_poll(poll_id, None, len(quotes), len(kept))
    print(f"Persisted watch poll {poll_id}: {len(quotes)} quotes, {len(kept)} opportunities.")
    return 0


def run_scan(args: argparse.Namespace, config, store: Store) -> int:
    if not config.consensus_enabled:
        print("consensus_enabled is false; scan is a no-op and no Odds API credits were spent.")
        return 0
    fixture_dir = Path("tests/fixtures") if args.fixtures else None
    adapter = OddsApiAdapter(config, fixture_dir)
    sports = args.sport or config.consensus_sports
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
    pregame, in_play = split_pregame(quotes, computed_at)
    quotes = [*pregame, *in_play]
    # In-play sportsbook odds must never become "pregame consensus".
    consensus = compute_consensus(pregame, config.min_books_for_consensus, config.min_books_for_consensus_totals, method=config.devig_method, outlier_points=config.consensus_outlier_points)
    print(f"Consensus outcomes computed: {len(consensus)} (from {len(pregame)} pregame quotes)")

    if args.dry_run:
        print("Scan dry run complete; quotes and consensus were not persisted.")
        return 1 if aborted else 0

    poll_id = store.start_poll("scan")
    store.save_poll_quotes(poll_id, quotes)
    store.save_consensus(consensus, computed_at)
    store.finish_poll(poll_id, adapter.credits_remaining, len(quotes), 0)
    print(f"Persisted scan poll {poll_id}: {len(quotes)} quotes, {len(consensus)} consensus outcomes.")
    return 1 if aborted else 0


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


def fetch_all_venues(adapters: list, sports: list[str]) -> list[Quote]:
    """Fetch venues CONCURRENTLY (independent rate limits → sweep time = slowest venue,
    and cross-venue leg skew shrinks). Within one venue stay serial: each venue's
    throttle is a shared budget that parallel workers would only split or forfeit."""

    def venue_fetch(adapter) -> list[Quote]:
        collected: list[Quote] = []
        for sport in sports:
            try:
                venue_quotes = adapter.fetch_quotes(sport)
            except FileNotFoundError:
                continue
            except (RuntimeError, requests.RequestException) as exc:
                # One venue/sport failing must not kill the loop; the cycle continues
                # with whatever quotes the other venues produced.
                print(f"{adapter.name} ({sport}): {exc}")
                continue
            collected.extend(venue_quotes)
            print(f"{sport} {adapter.name}: {len(venue_quotes)} quotes")
        return collected

    with ThreadPoolExecutor(max_workers=len(adapters)) as pool:
        results = list(pool.map(venue_fetch, adapters))
    return [quote for venue_quotes in results for quote in venue_quotes]


def split_pregame(quotes: list[Quote], now: datetime) -> tuple[list[Quote], list[Quote]]:
    """SPEC §1: pregame always. Started events are persisted live=1, never detected on.

    In-play books (and in-play sportsbook odds) priced against pregame consensus are the
    fastest source of absurd fake edges; break-window re-scans come later and stay live=1.
    """
    pregame: list[Quote] = []
    in_play: list[Quote] = []
    for quote in quotes:
        if quote.start_utc is None or quote.start_utc > now:
            pregame.append(quote)
        else:
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
    for quote in quotes:
        if quote.depth_usd is None or quote.depth_usd >= config.min_executable_usd:
            normal.append(quote)
        elif config.micro_enabled and quote.depth_usd >= config.micro_min_depth_usd:
            micro.append(quote)
        else:
            too_thin.append(quote)
    return normal, micro, too_thin


def compute_consensus(quotes: list[Quote], min_books: int, min_books_totals: int | None = None,
                      method: str = "proportional", outlier_points: Decimal | None = None) -> dict[tuple[str, str], Decimal]:
    by_event: dict[str, list[Quote]] = defaultdict(list)
    for quote in quotes:
        by_event[quote.event_key].append(quote)
    consensus: dict[tuple[str, str], Decimal] = {}
    for event_key, event_quotes in by_event.items():
        for outcome, p_hat in consensus_probabilities(event_quotes, min_books, min_books_totals,
                                                       method=method, outlier_points=outlier_points).items():
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
) -> list[Opportunity]:
    now = datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(hours=max_age_hours)
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
        outcome = consensus_outcome_key(quote.market_type, quote.line, quote.outcome_label)
        entry = consensus.get((quote.event_key, outcome))
        if entry is None:
            continue
        p_hat, computed_at = entry
        # EV#5: apply the measured FLB calibration to p_hat before EV (the curve IS a p_hat
        # correction). Off unless a calibrator is supplied; identity otherwise.
        calibrated = False
        if calibrator is not None:
            from .bias import calibrate_probability
            adj = q6(Decimal(str(calibrate_probability(float(p_hat), calibrator))))
            calibrated = adj != p_hat
            p_hat = adj
        ev = expected_value(p_hat, quote.effective_cost)
        # SPEC §1: every EV > 0 is recorded; thresholds gate display/betting, not recording.
        if ev <= 0:
            continue
        status, notes = status_for_legs([quote], trusted_pairs)
        edge = edge_points(p_hat, quote.effective_cost)
        notes = (*notes, f"edge_pts:{edge}")
        if calibrated:
            notes = (*notes, "p_hat-calibrated")
        below_e_min = e_min is not None and edge < e_min
        if below_e_min and not record_below_e_min:
            continue
        if below_e_min:
            notes = (*notes, "below-e-min")
        # EV_SPEC §4: EV's own killer — consensus event vs contract event may differ.
        status = "REVIEW"
        notes = (*notes, "event-match-review")
        is_stale = computed_at < (pregame_cutoff if not quote.live else stale_cutoff)
        if is_stale:
            notes = (*notes, "stale-consensus")
        if quote.depth_usd is not None:
            notes = (*notes, f"capturable:{q6(quote.depth_usd * edge)}")
        opp = Opportunity(
            kind="EV",
            category="sports",
            event=build_event(quote.event_key, [quote]),
            legs=[quote],
            edge=ev,
            edge_points=edge,  # #9 p-c, comparable across kinds (quality/ranking use this)
            stakes={quote.outcome_label: Decimal("1.00")},
            status=status,  # type: ignore[arg-type]
            notes=notes,
        )
        if config is not None:
            opp = _annotate_quality_and_kelly(opp, p_hat, quote.effective_cost, config)
        # #5/#3 First-class quarantine: sub-threshold and stale-line EV must not sit in the
        # CLEAN aggregate keyed on quality_flag IS NULL. Don't override a more specific flag.
        if opp.quality_flag is None:
            if below_e_min:
                opp = replace(opp, quality_flag="below-e-min")
            elif is_stale:
                opp = replace(opp, quality_flag="stale-consensus")
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
    )
    return [_mark_micro_ev(opp, config) for opp in raw]


def _mark_micro_ev(opp: Opportunity, config) -> Opportunity:
    quote = opp.legs[0]
    bankroll_cap = config.bankroll_usd * config.max_stake_pct
    depth_cap = quote.depth_usd if quote.depth_usd is not None else config.micro_max_stake_usd
    stake = money(min(depth_cap, config.micro_max_stake_usd, bankroll_cap))
    notes = (
        *opp.notes,
        "micro-depth-sized",
        "micro-ev-only",
        f"micro-depth-usd:{q6(depth_cap)}",
        f"micro-stake-usd:{stake}",
    )
    return replace(
        opp,
        stakes={quote.outcome_label: stake},
        notes=notes,
        quality_flag="micro",
        kelly_stake=stake if opp.kelly_stake is None else min(opp.kelly_stake, stake),
    )


def _annotate_quality_and_kelly(opp: Opportunity, p_hat: Decimal, effective_cost: Decimal, config) -> Opportunity:
    """Attach Phase 1 quality flag and Phase 3 Kelly projection to an EV opportunity."""
    quality = classify_quality(opp, config.min_executable_usd, config.ev_edge_suspect_above)
    f_star = kelly_f_star(p_hat, effective_cost)
    stake = kelly_stake(p_hat, effective_cost, config.sizing_kelly_fraction,
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
        days = max(0.0, (opp.event.start_utc - datetime.now(timezone.utc)).total_seconds() / 86400.0)
        if not is_economic(opp.edge, days, config.capital_charge_annual):
            notes = (*notes, f"uneconomic-horizon:{days:.1f}d")
            quality = quality or "uneconomic-horizon"
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


def run_settle_job(config, store: Store) -> None:
    # Each source is isolated so one failing never blocks the other (the old failure mode:
    # a silent settle error let bets age past the Odds API window and orphan forever).
    try:
        settled = settle_sports(store, config)
        print(f"Daily settlement (Odds API): {settled} flat bets resolved.")
    except Exception as exc:  # noqa: BLE001
        print(f"settle: Odds API settlement failed: {exc}")
    try:
        backfilled = settle_backfill_mlb(store)
        print(f"Daily settlement (MLB Stats backfill): {backfilled} flat bets resolved.")
    except Exception as exc:  # noqa: BLE001
        print(f"settle: MLB Stats backfill failed: {exc}")
    try:
        from .reconcile import reconcile_settlements
        from .venues.kalshi import kalshi_leg_result

        counts = reconcile_settlements(store, kalshi_leg_result)
        print(f"Daily reconciliation (venue-actual vs lock): {counts}")
    except Exception as exc:  # noqa: BLE001
        print(f"settle: reconciliation failed: {exc}")


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
    print(
        f"Daemon: watch every {config.watch_interval_secs}s, scans at {', '.join(config.scan_times)}, "
        f"settle at {config.settle_time}, daily report at {config.daily_report_time} ({config.timezone})."
    )
    while True:
        # The daemon must outlive any single failed cycle (network blips, venue outages).
        try:
            schedule.run_pending()
            run_watch_once(watch_args, config, store)
            # MLB Stats settlement is free and window-less, so settle finals every cycle —
            # this is what stops bets aging out and orphaning between daily settle runs.
            try:
                n = settle_backfill_mlb(store)
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
