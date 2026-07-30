"""CLI: `python -m arb <command>` (SPEC §14.1).

Design rules (§14.1): every command is safe to run twice; every command prints
which DB and HEAD sha it ran against; nothing destructive exists.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console

from arb.config import load as load_config
from arb.store import Store

load_dotenv()  # ODDS_API_KEY / ANTHROPIC_API_KEY from git-ignored .env

DEFAULT_DB = "data/arb.sqlite"
console = Console()


def head_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return "no-commit"


def _startup_warnings(cfg, db_path):
    dest = Path(cfg["db_backup"]["dest"])
    try:
        if dest.exists() and Path(db_path).parent.exists() and \
                os.stat(dest).st_dev == os.stat(Path(db_path).parent).st_dev:
            console.print("[yellow]WARNING[/]: db_backup.dest is on the same volume as the "
                          "DB — a disk failure loses both (SPEC §13.3)")
    except OSError:
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(prog="arb", description="ArbitrageModel paper-trading scanner")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--db", default=DEFAULT_DB)
    sub = parser.add_subparsers(dest="command", required=True)

    p_watch = sub.add_parser("watch", help="primary loop: fetch, match, detect, track, persist")
    p_watch.add_argument("--once", action="store_true")
    p_watch.add_argument("--dry-run", action="store_true")
    sub.add_parser("scan", help="Odds API consensus refresh (no-op if disabled)")
    p_settle = sub.add_parser("settle", help="resolve pending bets; auto-runs reconcile + invariants")
    p_settle.add_argument("--manual", nargs=2, metavar=("EVENT", "RESULT"))
    p_settle.add_argument("-y", action="store_true", dest="yes")
    p_settle.add_argument("--dry-run", action="store_true")
    p_report = sub.add_parser("report", help="recompute any day idempotently from flat_bets")
    p_report.add_argument("--daily", metavar="DATE", default=None)
    sub.add_parser("status", help="process states, freshness alarm, credits, P&L snapshot")
    sub.add_parser("check-invariants", help="the six ledger invariants; silent when clean")
    p_regrade = sub.add_parser("regrade", help="re-score consensus vs venue mids (read-only)")
    p_regrade.add_argument("--regrade-db", metavar="PATH", default=None)
    sub.add_parser("supervisor", help="own all loops; the only sanctioned way to run them")
    for lane in ("negrisk-scan", "verify", "hot", "btc", "political", "mapped"):
        lp = sub.add_parser(lane, help=f"optional {lane} lane (supervisor-owned)")
        lp.add_argument("--once", action="store_true")
    p_ns = sub.add_parser("nonsports", help="blocking-key matcher -> candidates "
                                            "for curation; --ai/--eval gate §18.5")
    p_ns.add_argument("--once", action="store_true")
    p_ns.add_argument("--ai", action="store_true",
                      help="Haiku stage, live mode (requires ai_matcher.enabled)")
    p_ns.add_argument("--eval", action="store_true", dest="ai_eval",
                      help="classify curated pairs + score precision vs ground truth")
    p_ns.add_argument("--sweep", action="store_true", dest="ai_sweep",
                      help="classify ALL uncached candidates (second opinion)")
    p_live = sub.add_parser("live", help="in-play lane: shortlisted re-quotes at "
                                         "live cadence (review-lane forever)")
    p_live.add_argument("--once", action="store_true")
    p_live.add_argument("--dry-run", action="store_true")
    sub.add_parser("leadlag", help="lead-lag study over stored quotes (read-time)")
    p_replay = sub.add_parser("replay", help="re-run detect() over stored quotes; "
                                             "diff vs what was flagged (read-only)")
    p_replay.add_argument("--from", dest="from_date", required=True, metavar="DATE")
    p_replay.add_argument("--to", dest="to_date", default=None, metavar="DATE")
    p_replay.add_argument("--kind", choices=("watch", "live"), default="watch")
    p_cov = sub.add_parser("covers", help="cover-survival funnel over a window: "
                                          "what WOULD have paid (read-only)")
    p_cov.add_argument("--since", required=True, metavar="ISO",
                       help="only polls started at/after this timestamp")
    p_bench = sub.add_parser("bench", help="score and rank every STRATEGIES.md lane on "
                                           "the data we hold (read-only)")
    p_bench.add_argument("--pull", action="store_true",
                         help="refresh the Kalshi settled corpus first (keyless, 0 credits)")
    p_bench.add_argument("--probe-anj", action="store_true",
                         help="spend ~2 Odds API credits on the L9 fr+eu overround probe")
    p_bench.add_argument("--only", default=None, metavar="L1,L8",
                         help="score just these lanes")
    p_proj = sub.add_parser("projection", help="read-time capacity projection: what each "
                                                "locked flag WOULD support at each ceiling")
    p_proj.add_argument("--from", dest="from_date", required=True, metavar="DATE")
    p_proj.add_argument("--to", dest="to_date", default=None, metavar="DATE")
    p_bs = sub.add_parser("banksim", help="bankroll-constrained paper book: what a "
                                          "fixed pot would have caught (read-only)")
    p_bs.add_argument("--hours", type=float, default=24.0,
                      help="window ending now, in hours")
    p_bs.add_argument("--bankrolls", default="250,500,1000")
    p_bs.add_argument("--out", default=None, metavar="PATH",
                      help="append the snapshot as JSONL")
    sub.add_parser("dashboard", help="read-only local panel on dashboard.host:port (§14.3)")
    sub.add_parser("canary", help="inject the monthly synthetic fixture through the live pipeline")
    sub.add_parser("shape-check", help="weekly live response schema validation vs fixtures")
    sub.add_parser("tickets", help="open locked flags as MANUAL order tickets, "
                                   "persistence-ranked (read-only, rule 1)")
    sub.add_parser("backup", help="sqlite VACUUM INTO db_backup.dest; prune to keep_last")
    sub.add_parser("archive", help="move quotes older than archive_after_days to data/archive/")

    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    from arb import logutil
    logutil.RETENTION_DAYS = int(cfg["log_retention_days"])  # before module loggers exist
    from arb.settlement import validate_settlement_paths
    validate_settlement_paths(cfg)  # a kind without a settler is a startup error (§12)
    console.print(f"[dim]db={args.db} head={head_sha()}[/]")
    _startup_warnings(cfg, args.db)

    from arb import commands
    handler = getattr(commands, "cmd_" + args.command.replace("-", "_"), None)
    if handler is None:
        console.print(f"[red]{args.command}: not implemented yet (see SPEC §16 phases)[/]")
        return 1
    return handler(cfg, Store(args.db), args) or 0


if __name__ == "__main__":
    sys.exit(main())
