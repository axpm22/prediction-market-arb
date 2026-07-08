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

from rich.console import Console

from arb.config import load as load_config
from arb.store import Store

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
    for lane in ("negrisk-scan", "verify", "hot", "btc", "political", "nonsports"):
        lp = sub.add_parser(lane, help=f"optional {lane} lane (supervisor-owned)")
        lp.add_argument("--once", action="store_true")
    sub.add_parser("canary", help="inject the monthly synthetic fixture through the live pipeline")
    sub.add_parser("shape-check", help="weekly live response schema validation vs fixtures")
    sub.add_parser("backup", help="sqlite VACUUM INTO db_backup.dest")

    args = parser.parse_args(argv)
    cfg = load_config(args.config)
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
