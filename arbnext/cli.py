from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone

import requests

from arb.cli import stop_requested
from arb.config import load_config
from arb.store import Store
from arb.venues.oddsapi import OddsApiCreditsLow

from arbnext.closing import record_closing_bets

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover
    yaml = None

CHECK_INTERVAL_SECS = 60  # schedule granularity; the window itself comes from config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m arbnext")
    sub = parser.add_subparsers(dest="command", required=True)
    closing = sub.add_parser("closing", help="closing-line EV recorder (one snapshot per event, minutes before start)")
    closing.add_argument("--once", action="store_true", help="run a single pass and exit")
    nonsports = sub.add_parser("nonsports", help="watch manually mapped non-sports events (politics/econ/weather)")
    nonsports.add_argument("--once", action="store_true", help="run a single pass and exit")
    sub.add_parser("nonsports-suggest", help="fuzzy-pair venue titles into logs/nonsports_candidates.log (suggestions only)")
    verify = sub.add_parser("verify", help="flash-verify fresh ARB flags by re-quoting their legs within seconds")
    verify.add_argument("--once", action="store_true", help="run a single pass and exit")
    focus = sub.add_parser("focus", help="bet-by-bet rotation: re-quote each event's books on all venues at once")
    focus.add_argument("--once", action="store_true", help="run a single rotation and exit")
    political = sub.add_parser("political", help="hunt intra-market arbs in politics/econ/weather (YES+NO < $1)")
    political.add_argument("--once", action="store_true", help="run a single pass and exit")
    hot = sub.add_parser("hot", help="per-bet watchers: re-quote each fresh arb's books every ~1s for its lifetime")
    hot.add_argument("--once", action="store_true", help="adopt current bets, watch to completion, exit")
    annotate = sub.add_parser("annotate", help="attach an immutable tag+reason to a bet (never edits the bet)")
    annotate.add_argument("bet_id", type=int)
    annotate.add_argument("tag", help="e.g. bug:in-play-pricing, settler-error, match-suspect")
    annotate.add_argument("reason", nargs="+", help="written reason (mandatory)")
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
    if args.command == "annotate":
        from arbnext.annotations import annotate as annotate_bet

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


def run_nonsports(args: argparse.Namespace, config) -> int:
    from arbnext.nonsports import run_nonsports_once

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
    from arbnext.verify import run_verify_once

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
    from arbnext.focus import run_focus_pass

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
    from arbnext.political.sweep import run_political_pass

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
    from arbnext.hot import run_hot_pass

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


def run_nonsports_suggest(config) -> int:
    from arbnext.nonsports import (
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
