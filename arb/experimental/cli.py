from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone

import requests

from arb.cli import stop_requested
from arb.config import load_config
from arb.store import Store
from arb.venues.oddsapi import OddsApiCreditsLow

from arb.experimental.closing import record_closing_bets

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover
    yaml = None

CHECK_INTERVAL_SECS = 60  # schedule granularity; the window itself comes from config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m arb.experimental")
    sub = parser.add_subparsers(dest="command", required=True)
    closing = sub.add_parser("closing", help="closing-line EV recorder (one snapshot per event, minutes before start)")
    closing.add_argument("--once", action="store_true", help="run a single pass and exit")
    nonsports = sub.add_parser("nonsports", help="watch manually mapped non-sports events (politics/econ/weather)")
    nonsports.add_argument("--once", action="store_true", help="run a single pass and exit")
    sub.add_parser("nonsports-suggest", help="fuzzy-pair venue titles into logs/nonsports_candidates.log (suggestions only)")
    sub.add_parser("niche", help="show curated niche map (weather/worldcup/macro) with validation verdicts")
    verify = sub.add_parser("verify", help="flash-verify fresh ARB flags by re-quoting their legs within seconds")
    verify.add_argument("--once", action="store_true", help="run a single pass and exit")
    focus = sub.add_parser("focus", help="bet-by-bet rotation: re-quote each event's books on all venues at once")
    focus.add_argument("--once", action="store_true", help="run a single rotation and exit")
    political = sub.add_parser("political", help="hunt intra-market arbs in politics/econ/weather (YES+NO < $1)")
    political.add_argument("--once", action="store_true", help="run a single pass and exit")
    hot = sub.add_parser("hot", help="per-bet watchers: re-quote each fresh arb's books every ~1s for its lifetime")
    hot.add_argument("--once", action="store_true", help="adopt current bets, watch to completion, exit")
    ev_exit = sub.add_parser("ev-exit", help="paper-exit EV bets when sell bid locks profit and edge is gone")
    ev_exit.add_argument("--once", action="store_true", help="run a single pass and exit")
    negrisk = sub.add_parser("negrisk", help="intra-Polymarket negative-risk basket arbs (S1, no settlement risk)")
    negrisk.add_argument("--once", action="store_true", help="run a single pass and exit")
    negrisk.add_argument("--lifetime", action="store_true", help="report the basket flag-lifetime distribution and exit (Lever C)")
    btc = sub.add_parser("btc", help="BTC cross-venue arb sweep (Phase 4; REVIEW — oracles differ)")
    btc.add_argument("--once", action="store_true", help="run a single pass and exit")
    whales = sub.add_parser("whales", help="ingest Polymarket whale trades + qualify wallets (Phase 5)")
    whales.add_argument("--once", action="store_true", help="run a single pass and exit")
    whales.add_argument("--list", action="store_true", help="print qualified wallets and exit")
    sub.add_parser("devig-regrade", help="offline: re-grade stored EV flags under proportional/power/shin de-vig (Lever A.3)")
    sub.add_parser("divergence-reversion", help="offline: do large cross-venue spreads revert? (weakness#10)")
    sub.add_parser("arb-capacity", help="offline: per-arb $ before the lock goes negative, from the depth ladders")
    sub.add_parser("devig-brier", help="offline: rank de-vig methods by Brier/log-loss on settled outcomes (PREGAME_EV #1)")
    web = sub.add_parser("web", help="serve the read-only live dashboard")
    web.add_argument("--port", type=int, default=8787, help="local port to bind, default: 8787")
    annotate = sub.add_parser("annotate", help="attach an immutable tag+reason to a bet (never edits the bet)")
    annotate.add_argument("bet_id", type=int)
    annotate.add_argument("tag", help="e.g. bug:in-play-pricing, settler-error, match-suspect")
    annotate.add_argument("reason", nargs="+", help="written reason (mandatory)")
    probe = sub.add_parser("live-baseball-probe", help="LIVE_BASEBALL_EV Phase 0 feasibility probe (measurement only)")
    probe.add_argument("--games", type=int, default=2, help="number of live games to probe")
    probe.add_argument("--minutes", type=float, default=5.0, help="how long to probe")
    probe.add_argument("--report", action="store_true", help="print the freshness/depth verdict and exit")
    watch = sub.add_parser("live-baseball-watch", help="LIVE_BASEBALL_EV Phase 1 candidate recorder (measurement only, betting OFF)")
    watch.add_argument("--games", type=int, default=4, help="max live games to watch")
    watch.add_argument("--minutes", type=float, default=10.0, help="how long to watch")
    watch.add_argument("--report", action="store_true", help="print the rejection/no-consensus histogram and exit")
    return parser


def extras_config(block: str) -> dict:
    if yaml is None:
        raise RuntimeError("pyyaml is required. Run `pip install -r requirements.txt`.")
    raw = yaml.safe_load(open("config.yaml").read()) or {}
    if block not in raw:
        raise RuntimeError(f"config.yaml is missing the {block} block.")
    return raw[block]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config()
    if args.command == "live-baseball-probe":
        return run_live_baseball_probe(args, config)
    if args.command == "live-baseball-watch":
        return run_live_baseball_watch(args, config)
    if args.command == "niche":
        return run_niche(config)
    if args.command == "nonsports-suggest":
        return run_nonsports_suggest(config)
    if args.command == "nonsports":
        return run_nonsports(args, config)
    if args.command == "verify":
        return run_verify(args, config)
    if args.command == "focus":
        return run_focus(args, config)
    if args.command == "political":
        return run_political(args, config)
    if args.command == "hot":
        return run_hot(args, config)
    if args.command == "ev-exit":
        return run_ev_exit(args, config)
    if args.command == "negrisk":
        return run_negrisk(args, config)
    if args.command == "btc":
        return run_btc(args, config)
    if args.command == "whales":
        return run_whales(args, config)
    if args.command == "devig-regrade":
        from arb.experimental.analysis.devig_regrade import format_regrade, regrade

        results = regrade(Store(config.database_path),
                          config.min_books_for_consensus, config.min_books_for_consensus_totals)
        print(format_regrade(results))
        return 0
    if args.command == "divergence-reversion":
        from arb.experimental.analysis.divergence_reversion import format_reversion, run_reversion

        print(format_reversion(run_reversion(Store(config.database_path),
                                             float(config.divergence_threshold))))
        return 0
    if args.command == "arb-capacity":
        from arb.experimental.analysis.arb_capacity import format_report, run_arb_capacity

        print(format_report(run_arb_capacity(
            Store(config.database_path),
            min_ladder_fraction=float(config.arb_min_ladder_fraction),
            fillable_fraction=config.arb_fillable_fraction)))
        return 0
    if args.command == "devig-brier":
        from arb.experimental.analysis.devig_brier import format_methods, score_methods

        print(format_methods(score_methods(Store(config.database_path), config.min_books_for_consensus)))
        return 0
    if args.command == "web":
        from arb.experimental.webserver import serve

        serve(args.port)
        return 0
    if args.command == "annotate":
        from arb.experimental.annotations import annotate as annotate_bet

        annotation_id = annotate_bet(Store(config.database_path), args.bet_id, args.tag, " ".join(args.reason))
        print(f"Annotation {annotation_id} added to bet {args.bet_id} [{args.tag}].")
        return 0
    closing_cfg = extras_config("closing_ev")
    store = Store(config.database_path)
    print(
        f"Closing-line EV: window T-{closing_cfg['window_open_mins']}m..T-{closing_cfg['window_close_mins']}m, "
        f"sports {closing_cfg['sports']}, one snapshot per event."
    )
    while True:
        try:
            recorded = record_closing_bets(store, config, closing_cfg, datetime.now(timezone.utc))
            if recorded:
                print(f"{datetime.now(timezone.utc).isoformat()} recorded {recorded} closing-line EV bets.")
        except OddsApiCreditsLow as exc:
            # CLAUDE.md rule 2: stop spending entirely when credits run low.
            print(f"closing: {exc} — recorder stopping.")
            return 1
        except (RuntimeError, requests.RequestException) as exc:
            print(f"closing: pass failed, retrying next interval: {exc}")
        if args.once:
            return 0
        if stop_requested("closing"):
            print("closing: stop flag found, exiting cleanly.")
            return 0
        time.sleep(CHECK_INTERVAL_SECS)


def run_live_baseball_probe(args: argparse.Namespace, config) -> int:
    from arb.experimental.live_baseball_probe import probe_report, run_probe

    store = Store(config.database_path)
    if args.report:
        print(probe_report(store, config))
        return 0
    run_probe(config, store, n_games=args.games, minutes=args.minutes)
    return 0


def run_live_baseball_watch(args: argparse.Namespace, config) -> int:
    from arb.experimental.live_baseball_watch import run_watch, watch_report

    store = Store(config.database_path)
    if args.report:
        print(watch_report(store))
        return 0
    run_watch(config, store, minutes=args.minutes, n_games=args.games)
    return 0


def run_nonsports(args: argparse.Namespace, config) -> int:
    from arb.experimental.nonsports import run_nonsports_once

    ncfg = extras_config("nonsports")
    store = Store(config.database_path)
    print("Non-sports watcher: mapped entries only, every flag REVIEW, settlement manual.")
    while True:
        try:
            n_quotes, n_opps = run_nonsports_once(store, config, ncfg)
            print(f"{datetime.now(timezone.utc).isoformat()} nonsports: {n_quotes} quotes, {n_opps} flags.")
        except (RuntimeError, requests.RequestException) as exc:
            print(f"nonsports: pass failed, retrying next interval: {exc}")
        if args.once:
            return 0
        if stop_requested("nonsports"):
            print("nonsports: stop flag found, exiting cleanly.")
            return 0
        time.sleep(int(ncfg["watch_interval_secs"]))


def run_verify(args: argparse.Namespace, config) -> int:
    from arb.experimental.verify import run_verify_once

    vcfg = extras_config("verify")
    store = Store(config.database_path)
    print(f"Flash verifier: re-quoting fresh ARB legs every {vcfg['poll_secs']}s (bets younger than {vcfg['max_bet_age_secs']}s).")
    while True:
        try:
            checked = run_verify_once(store, config, vcfg)
            if checked:
                print(f"{datetime.now(timezone.utc).isoformat()} flash-verified {checked} arb bets.")
        except (RuntimeError, requests.RequestException) as exc:
            print(f"verify: pass failed, retrying: {exc}")
        if args.once:
            return 0
        if stop_requested("verify"):
            print("verify: stop flag found, exiting cleanly.")
            return 0
        time.sleep(int(vcfg["poll_secs"]))


def run_focus(args: argparse.Namespace, config) -> int:
    from arb.experimental.focus import run_focus_pass

    fcfg = extras_config("focus")
    store = Store(config.database_path)
    print(
        f"Focused rotation: bet-by-bet across {fcfg['sports']}, "
        f"{fcfg['rotation_pause_secs']}s between events, map from sweeps <= {fcfg['map_max_age_mins']}min old."
    )
    while True:
        try:
            n_events, n_opps = run_focus_pass(store, config, fcfg)
            print(f"{datetime.now(timezone.utc).isoformat()} focus: {n_events} events re-quoted, {n_opps} flags.")
        except (RuntimeError, requests.RequestException) as exc:
            print(f"focus: rotation failed, retrying: {exc}")
        if args.once:
            return 0
        if stop_requested("focus"):
            print("focus: stop flag found, exiting cleanly.")
            return 0
        time.sleep(CHECK_INTERVAL_SECS)


def run_political(args: argparse.Namespace, config) -> int:
    from arb.experimental.political.sweep import run_political_pass

    pcfg = extras_config("political")
    store = Store(config.database_path)
    print(f"Political hunter: intra-market arbs across {pcfg['categories']} (one rulebook, no cross-venue risk).")
    while True:
        try:
            scanned, found = run_political_pass(store, config, pcfg)
            print(f"{datetime.now(timezone.utc).isoformat()} political: {scanned} markets scanned, {found} intra-market arbs.")
        except (RuntimeError, requests.RequestException) as exc:
            print(f"political: pass failed, retrying: {exc}")
        if args.once:
            return 0
        if stop_requested("political"):
            print("political: stop flag found, exiting cleanly.")
            return 0
        time.sleep(int(pcfg["watch_interval_secs"]))


def run_hot(args: argparse.Namespace, config) -> int:
    from arb.experimental.hot import run_hot_pass

    hcfg = extras_config("hot")
    store = Store(config.database_path)
    print(
        f"Hot watchers: per-bet re-quotes every {hcfg['check_interval_secs']}s, "
        f"window {hcfg['watch_duration_secs']}s, max {hcfg['max_watchers']} concurrent."
    )
    while True:
        try:
            adopted = run_hot_pass(store, config, hcfg)
            if adopted:
                print(f"{datetime.now(timezone.utc).isoformat()} hot: watched {adopted} bets to completion.")
        except (RuntimeError, requests.RequestException) as exc:
            print(f"hot: pass failed, retrying: {exc}")
        if args.once:
            return 0
        if stop_requested("hot"):
            print("hot: stop flag found, exiting cleanly.")
            return 0
        time.sleep(int(hcfg["poll_secs"]))


def run_negrisk(args: argparse.Namespace, config) -> int:
    from arb.experimental.negrisk_sweep import lifetime_distribution, run_negrisk_pass

    store = Store(config.database_path)
    if getattr(args, "lifetime", False):
        d = lifetime_distribution(store)
        print(f"NEGRISK flag-lifetime: opened={d['opened']} closed={d['closed']} "
              f"p50={d['p50_secs']}s p90={d['p90_secs']}s max={d['max_secs']}s")
        return 0
    print("Negative-risk basket sweep: intra-Polymarket, single resolution, no cross-venue risk.")
    while True:
        try:
            n_events, found = run_negrisk_pass(store, config)
            print(f"{datetime.now(timezone.utc).isoformat()} negrisk: {n_events} events, {found} basket arbs.")
        except (RuntimeError, requests.RequestException) as exc:
            print(f"negrisk: pass failed, retrying: {exc}")
        if args.once:
            return 0
        if stop_requested("negrisk"):
            print("negrisk: stop flag found, exiting cleanly.")
            return 0
        time.sleep(180)


def run_ev_exit(args: argparse.Namespace, config) -> int:
    from arb.experimental.ev_exit import run_ev_exit_once

    ecfg = extras_config("ev_exit")
    store = Store(config.database_path)
    print(
        f"EV exit watcher: sell-bid take-profit, min ROI {ecfg['min_profit_roi']}, "
        f"remaining edge <= {ecfg['exit_edge_ceiling']}."
    )
    while True:
        try:
            poll_id = store.start_poll("ev-exit")
            exited = run_ev_exit_once(store, config, ecfg)
            store.finish_poll(poll_id, None, 0, exited)
            if exited:
                print(f"{datetime.now(timezone.utc).isoformat()} ev-exit: appended {exited} EXITED/SOLD rows.")
        except (RuntimeError, requests.RequestException) as exc:
            print(f"ev-exit: pass failed, retrying: {exc}")
        if args.once:
            return 0
        if stop_requested("ev-exit"):
            print("ev-exit: stop flag found, exiting cleanly.")
            return 0
        time.sleep(int(ecfg["poll_secs"]))


def run_btc(args: argparse.Namespace, config) -> int:
    from arb.experimental.btc_sweep import run_btc_pass

    store = Store(config.database_path)
    print("BTC cross-venue sweep: Kalshi (CF Benchmarks) vs Polymarket (UMA) — REVIEW, oracles differ.")
    while True:
        try:
            n_legs, found = run_btc_pass(store, config)
            print(f"{datetime.now(timezone.utc).isoformat()} btc: {n_legs} legs, {found} cross-venue flags (REVIEW).")
        except (RuntimeError, requests.RequestException) as exc:
            print(f"btc: pass failed, retrying: {exc}")
        if args.once:
            return 0
        if stop_requested("btc"):
            print("btc: stop flag found, exiting cleanly.")
            return 0
        time.sleep(120)


def run_whales(args: argparse.Namespace, config) -> int:
    from arb.experimental.whales import run_whale_pass

    wcfg = extras_config("whale_tracker")
    store = Store(config.database_path)
    if args.list:
        with store.connect() as con:
            rows = list(con.execute(
                "SELECT address, n_trades, win_rate, top_category, category_share, qualified, suspect FROM wallets WHERE qualified=1 ORDER BY n_trades DESC LIMIT 40"))
        print(f"{'WALLET':14} {'N':>6} {'WR':>6} {'CATEGORY':14} {'SHARE':>6} SUSPECT")
        for r in rows:
            print(f"{r['address'][:14]:14} {r['n_trades']:>6} {r['win_rate']:>6.0%} {str(r['top_category'])[:14]:14} {r['category_share']:>6.0%} {'yes' if r['suspect'] else ''}")
        if not rows:
            print("(no qualified wallets yet — run `whales --once` to ingest trades)")
        return 0
    print("Whale tracker: ingest Polymarket on-chain trades, qualify wallets (measurement/debunk).")
    while True:
        try:
            n_wallets, qualified = run_whale_pass(store, wcfg)
            print(f"{datetime.now(timezone.utc).isoformat()} whales: {n_wallets} wallets seen, {qualified} qualified.")
        except (RuntimeError, requests.RequestException) as exc:
            print(f"whales: pass failed, retrying: {exc}")
        if args.once:
            return 0
        if stop_requested("whales"):
            print("whales: stop flag found, exiting cleanly.")
            return 0
        time.sleep(300)


def run_niche(config) -> int:
    from arb.experimental.niche import load_niche_map, validate_entry

    entries = load_niche_map()
    if not entries:
        print("No niche map entries (data/nonsports_map.yaml).")
        return 0
    print(f"{'ID':44s} {'VERTICAL':9s} {'STATE':9s} VERDICT")
    for e in entries:
        v = validate_entry(e)
        if not (e.get("enabled") and e.get("reviewed")):
            state, verdict = "template", "disabled (enabled/reviewed false)"
        elif v.approved:
            state, verdict = "LIVE", "validated — rulebooks match"
        else:
            state, verdict = "REFUSED", f"{v.field}: {v.reason}"
        print(f"{str(e.get('id'))[:44]:44s} {str(e.get('vertical')):9s} {state:9s} {verdict}")
    print("\nNiche flags are cross-venue => REVIEW, never auto-filled; settlement is manual.")
    return 0


def run_nonsports_suggest(config) -> int:
    from arb.experimental.nonsports import (
        fetch_kalshi_series_titles,
        fetch_polymarket_titles,
        suggest_candidates,
        write_candidates,
    )

    ncfg = extras_config("nonsports")
    kalshi_titles = fetch_kalshi_series_titles(list(ncfg["kalshi_categories"]))
    polymarket_titles = fetch_polymarket_titles(list(ncfg["polymarket_tags"]))
    matches = suggest_candidates(kalshi_titles, polymarket_titles, int(ncfg["candidate_threshold"]))
    added = write_candidates(matches)
    print(
        f"Compared {len(kalshi_titles)} Kalshi series x {len(polymarket_titles)} Polymarket events; "
        f"{len(matches)} candidates >= {ncfg['candidate_threshold']}, {added} new lines in logs/nonsports_candidates.log."
    )
    print("Review both venues' resolution wording before promoting anything into data/nonsports_map.yaml.")
    return 0
