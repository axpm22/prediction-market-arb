"""Logging with query-string redaction (SPEC §6.4: the Odds API key rides as a
query param; every log sink redacts query strings — one regex, one test) and
daily rotation retained log_retention_days (§13.3)."""
from __future__ import annotations

import logging
import os
import re
import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

_QS = re.compile(r"\?\S*")


def resolve_log_path(path: str | Path) -> Path:
    """Reroot a relative log path under ARB_LOG_DIR when set (test plumbing:
    conftest.py points this at a tmpdir so tests never touch production
    logs/). Shared by get_logger below and arb/child_log.py's raw per-child
    capture files, which bypass the logging module entirely."""
    root = os.environ.get("ARB_LOG_DIR")
    p = Path(path)
    if root and not p.is_absolute():
        p = Path(root) / p
    return p


def purge_stale(directory: str | Path, retention_days: int, now: float | None = None) -> int:
    """Delete files directly under `directory` last modified more than
    retention_days ago; returns the count removed. Extends log_retention_days
    (already enforced per-logfile by get_logger's TimedRotatingFileHandler
    backupCount) to arb/child_log.py's per-process files, which never go
    through logging.emit() — a child's stdout/stderr is redirected straight
    to the fd, so there is no rollover hook to piggyback on. Unlike a rotated
    logger backup, one process's log is touched on every spawn while that
    lane is enabled, so this never trims an ACTIVE lane's file — it only
    reclaims logs left behind by a lane disabled or removed more than
    retention_days ago."""
    d = Path(directory)
    if not d.is_dir():
        return 0
    cutoff = (now if now is not None else time.time()) - retention_days * 86400
    n = 0
    for p in d.iterdir():
        if p.is_file() and p.stat().st_mtime < cutoff:
            p.unlink()
            n += 1
    return n


def redact(text: str) -> str:
    return _QS.sub("?<redacted>", text)


def _scrub(a):
    """Numbers pass through UNCONVERTED: str()ing them turns every '%d'/'%f' in
    the codebase into a TypeError at emit time, which is a crash in the logging
    call rather than a log line (observed live 2026-07-29 on matching.py's
    start-unknown counter). An int/float cannot carry a query-string secret, so
    there is nothing to redact."""
    return a if isinstance(a, (int, float)) and not isinstance(a, bool) else redact(str(a))


class _RedactingFilter(logging.Filter):
    def filter(self, record):
        record.msg = redact(str(record.msg))
        if record.args:
            # logging special-cases a single dict arg (record.args IS the
            # dict); tuple() over it would yield its KEYS and break %-format
            record.args = (redact(str(record.args)),) \
                if isinstance(record.args, dict) \
                else tuple(_scrub(a) for a in record.args)
        return True


# ONE rotating handler per logfile per process: a handler per LOGGER means
# several handlers rotating the same file at midnight, clobbering backups
_file_handlers: dict[str, logging.Handler] = {}

# cli.main() overwrites this from cfg["log_retention_days"] BEFORE the command
# modules import (module-level loggers are created at import time)
RETENTION_DAYS = 90


def _decorate(h: logging.Handler) -> logging.Handler:
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    h.addFilter(_RedactingFilter())
    return h


def get_logger(name: str, logfile: str | Path | None = None,
               retention_days: int | None = None) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    logger.addHandler(_decorate(logging.StreamHandler()))
    if logfile:
        key = str(logfile)
        if key not in _file_handlers:
            # ARB_LOG_DIR reroots relative logfile paths (read at handler-
            # creation time). Test plumbing: tests/conftest.py sets it so
            # pytest never appends into production logs/. Default unchanged.
            path = resolve_log_path(logfile)
            path.parent.mkdir(parents=True, exist_ok=True)
            _file_handlers[key] = _decorate(TimedRotatingFileHandler(
                path, when="midnight",
                backupCount=retention_days if retention_days is not None
                else RETENTION_DAYS))
        logger.addHandler(_file_handlers[key])
    return logger
