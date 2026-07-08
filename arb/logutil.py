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


def get_logger(name: str, logfile: str | Path | None = None,
               retention_days: int = 90) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if logfile:
        Path(logfile).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(TimedRotatingFileHandler(
            logfile, when="midnight", backupCount=retention_days))
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(_RedactingFilter())
        logger.addHandler(h)
    return logger
