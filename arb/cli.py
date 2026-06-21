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
from .pricing import consensus_outcome_key, consensus_probabilities, edge_points, expected_value, q6
from .quality import classify_quality
from .sizing import kelly_f_star, kelly_stake
from .report import print_opportunities, print_positions, print_report
from .settlement import settle_manual, settle_sports
from .store import Store
from .tracker import record_tracker_poll, write_daily_report
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
    sub.add_parser("scores", help="per-segment edge scores from settled clean bets (Phase 2)")
    sub.add_parser("dashboard", help="regenerate reports/dashboard.json from the ledger (Phase 6)")
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
    if args.command == "dashboard":
        from .experimental.dashboard import write_dashboard

        path = write_dashboard(store, config)
        print(f"Wrote {path}")
        return 0
    if args.command == "settle":
        if args.manual:
            settled = settle_manual(store, args.manual[0], args.manual[1])
        else:
            fixture_dir = Path("tests/fixtures") if args.fixtures else None
            settled = settle_sports(store, config, fixture_dir)
        print(f"Settled {settled} flat bets.")
        return 0
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

    pregame, in_play = split_pregame(quotes, datetime.now(timezone.utc))
    if in_play:
        print(f"{len(in_play)} in-play quotes persisted with live=1 and excluded from detection (pregame only).")
    # Depth floor BEFORE detection: a derived 1-cent ask backed by $3 of resting size is
    # not a price. Thin quotes are persisted (data) but never participate in detection.
    detectable = [q for q in pregame if q.depth_usd is None or q.depth_usd >= config.min_executable_usd]
    if len(detectable) < len(pregame):
        print(f"{len(pregame) - len(detectable)} thin quotes (depth < ${config.min_executable_usd}) excluded from detection.")
    opportunities = detect_true_arbs(
        detectable,
        bankroll=config.bankroll_usd,
        max_stake_pct=config.max_stake_pct,
        trusted_pairs=config.trusted_pairs,
        stale_warn=config.arb_stale_warn,
        max_leg_skew_secs=config.max_leg_skew_secs,
        max_start_drift_mins=config.max_start_drift_mins,
        match_suspect_prob_diff=config.match_suspect_prob_diff,
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
        opportunities.extend(
            detect_ev_from_consensus(
                detectable,
                store.load_consensus(),
                max_age_hours=config.consensus_max_age_hours,
                trusted_pairs=config.trusted_pairs,
                e_min=config.ev_e_min,
                config=config,
            )
        )
    opportunities, suspects = split_match_suspects(opportunities)
    if suspects:
        print(f"{len(suspects)} match-suspect pairs quarantined for human review (not recorded as flags).")
    kept, left_on_table = apply_opportunity_filters(opportunities, config)
    if left_on_table:
        left_edge = sum((opp.edge for opp in left_on_table), Decimal("0"))
        print(f"Left on table: {len(left_on_table)} flags (summed edge {left_edge}) killed by liquidity/depth/horizon filters.")
    print_opportunities(kept, ev_display_threshold=config.ev_threshold)

    if args.dry_run:
        print("Watch dry run complete; quotes and tracker rows were not persisted.")
        return 0

    poll_id = store.start_poll("watch")
    store.save_poll_quotes(poll_id, [*pregame, *in_play])
    store.save_poll_opportunities(poll_id, kept)
    store.save_suspect_matches(poll_id, suspects)
    record_tracker_poll(store, poll_id, kept)
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
    consensus = compute_consensus(pregame, config.min_books_for_consensus)
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


def compute_consensus(quotes: list[Quote], min_books: int) -> dict[tuple[str, str], Decimal]:
    by_event: dict[str, list[Quote]] = defaultdict(list)
    for quote in quotes:
        by_event[quote.event_key].append(quote)
    consensus: dict[tuple[str, str], Decimal] = {}
    for event_key, event_quotes in by_event.items():
        for outcome, p_hat in consensus_probabilities(event_quotes, min_books).items():
            consensus[(event_key, outcome)] = p_hat
    return consensus


def detect_ev_from_consensus(
    quotes: list[Quote],
    consensus: dict[tuple[str, str], tuple[Decimal, datetime]],
    max_age_hours: int,
    trusted_pairs: set[str],
    e_min: Decimal | None = None,
    config=None,
) -> list[Opportunity]:
    stale_cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    opportunities: list[Opportunity] = []
    for quote in quotes:
        if quote.venue == "oddsapi":
            continue
        outcome = consensus_outcome_key(quote.market_type, quote.line, quote.outcome_label)
        entry = consensus.get((quote.event_key, outcome))
        if entry is None:
            continue
        p_hat, computed_at = entry
        ev = expected_value(p_hat, quote.effective_cost)
        # SPEC §1: every EV > 0 is recorded; thresholds gate display/betting, not recording.
        if ev <= 0:
            continue
        status, notes = status_for_legs([quote], trusted_pairs)
        edge = edge_points(p_hat, quote.effective_cost)
        notes = (*notes, f"edge_pts:{edge}")
        if e_min is not None and edge < e_min:
            notes = (*notes, "below-e-min")
        # EV_SPEC §4: EV's own killer — consensus event vs contract event may differ.
        status = "REVIEW"
        notes = (*notes, "event-match-review")
        if computed_at < stale_cutoff:
            notes = (*notes, "stale-consensus")
        if quote.depth_usd is not None:
            notes = (*notes, f"capturable:{q6(quote.depth_usd * edge)}")
        opp = Opportunity(
            kind="EV",
            category="sports",
            event=build_event(quote.event_key, [quote]),
            legs=[quote],
            edge=ev,
            stakes={quote.outcome_label: Decimal("1.00")},
            status=status,  # type: ignore[arg-type]
            notes=notes,
        )
        if config is not None:
            opp = _annotate_quality_and_kelly(opp, p_hat, quote.effective_cost, config)
        opportunities.append(opp)
    return opportunities


def _annotate_quality_and_kelly(opp: Opportunity, p_hat: Decimal, effective_cost: Decimal, config) -> Opportunity:
    """Attach Phase 1 quality flag and Phase 3 Kelly projection to an EV opportunity."""
    quality = classify_quality(opp, config.min_executable_usd, config.ev_edge_suspect_above)
    f_star = kelly_f_star(p_hat, effective_cost)
    stake = kelly_stake(p_hat, effective_cost, config.sizing_kelly_fraction,
                        config.bankroll_usd, config.sizing_max_fraction)
    return replace(opp, quality_flag=quality, kelly_f_star=f_star, kelly_stake=stake)


def apply_opportunity_filters(opportunities: list[Opportunity], config) -> tuple[list[Opportunity], list[Opportunity]]:
    """SPEC §1 step 3: depth and settlement-horizon filters; filtered flags are counted, not bet."""
    horizon = datetime.now(timezone.utc) + timedelta(days=config.max_days_to_settlement)
    kept: list[Opportunity] = []
    left: list[Opportunity] = []
    for opportunity in opportunities:
        ok = True
        for leg in opportunity.legs:
            if leg.depth_usd is not None and leg.depth_usd < config.min_executable_usd:
                ok = False
            if leg.start_utc is not None and leg.start_utc > horizon:
                ok = False
        (kept if ok else left).append(opportunity)
    return kept, left


def run_settle_job(config, store: Store) -> None:
    settled = settle_sports(store, config)
    print(f"Daily settlement: {settled} flat bets resolved.")


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
        except Exception as exc:  # noqa: BLE001
            print(f"daemon: cycle failed, retrying next interval: {exc}")
        if stop_requested("daemon"):
            print("daemon: stop flag found, exiting cleanly.")
            return 0
        time.sleep(config.watch_interval_secs)
