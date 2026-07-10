"""Logging with query-string redaction (SPEC §6.4: the Odds API key rides as a
query param; every log sink redacts query strings — one regex, one test) and
daily rotation retained log_retention_days (§13.3)."""
from __future__ import annotations

import logging
import re
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

_QS = re.compile(r"\?\S*")


def redact(text: str) -> str:
    return _QS.sub("?<redacted>", text)


class _RedactingFilter(logging.Filter):
    def filter(self, record):
        record.msg = redact(str(record.msg))
        if record.args:
            record.args = tuple(redact(str(a)) for a in record.args)
        return True


# ONE rotating handler per logfile per process: a handler per LOGGER means
# several handlers rotating the same file at midnight, clobbering backups
_file_handlers: dict[str, logging.Handler] = {}


def _decorate(h: logging.Handler) -> logging.Handler:
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    h.addFilter(_RedactingFilter())
    return h


def get_logger(name: str, logfile: str | Path | None = None,
               retention_days: int = 90) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    logger.addHandler(_decorate(logging.StreamHandler()))
    if logfile:
        key = str(logfile)
        if key not in _file_handlers:
            Path(logfile).parent.mkdir(parents=True, exist_ok=True)
            _file_handlers[key] = _decorate(TimedRotatingFileHandler(
                logfile, when="midnight", backupCount=retention_days))
        logger.addHandler(_file_handlers[key])
    return logger
