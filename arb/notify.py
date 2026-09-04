"""Outbound webhook notifier (CLAUDE.md rule 1): POSTs to the operator's OWN
`notify.webhook_url`, never a market-data endpoint, and never fetches or
influences prices. Every call is defensive — a dead/misconfigured webhook must
never break the caller that triggered it.
"""
from __future__ import annotations

import json
import socket
import time
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlsplit

from arb.logutil import get_logger

# same file-handler + redacting-filter setup every other module uses, not a
# bare logging.getLogger() with no handlers (I1)
logger = get_logger("arb.notify", "logs/scan.log")

# per-(event, key) throttle: a flapping alarm cannot spam the endpoint, but
# distinct entities sharing one event (two silent lanes, two fired rules) must
# not collapse into a single send (I2). Module-level by design — one process,
# one notifier.
_last_sent: dict[tuple[str, object], float] = {}


def _default_poster(url: str, body: bytes, timeout: float) -> None:
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=timeout).close()


def _json_default(o):
    if isinstance(o, Decimal):
        return str(o)
    raise TypeError(f"not JSON serializable: {o!r}")


def post(cfg, event: str, payload: dict, key=None, poster=None) -> bool:
    """JSON POST `{event, at, host, head, payload}` to `cfg["notify"]["webhook_url"]`.
    `key` names the specific entity this event is about (a lane, a rule id) so
    the throttle keys on (event, key) rather than collapsing every entity
    sharing an event into one send. Never raises: any failure (config,
    serialization, network) logs one WARNING naming only the target host and
    exception type — the URL itself is treated as a secret — and returns False."""
    n = cfg["notify"]
    url = str(n["webhook_url"])
    if not n["enabled"] or not url:
        return False
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        logger.warning("notify: webhook_url is malformed (scheme=%r) — "
                       "configure a full http(s) URL", parts.scheme or "")
        return False
    throttle_key = (event, key)
    now = time.monotonic()
    interval = float(n["min_interval_secs"])
    last = _last_sent.get(throttle_key)
    if last is not None and now - last < interval:
        return False
    _last_sent[throttle_key] = now
    try:
        from arb.cli import head_sha  # local import: cli.py is the entrypoint module
        body = json.dumps({
            "event": event, "at": datetime.now(timezone.utc).isoformat(),
            "host": socket.gethostname(), "head": head_sha(), "payload": payload,
        }, default=_json_default).encode()
        (poster or _default_poster)(url, body, float(n["timeout_secs"]))
        return True
    except Exception as e:  # noqa: BLE001 — a notifier failure must never raise
        # the exception's own message can embed the URL (e.g. urlopen's
        # "unknown url type: '<url>'") — scrub it before it ever reaches a log
        logger.warning("notify: POST failed (%s): %s [host=%s]", type(e).__name__,
                       str(e).replace(url, "<url>"), parts.hostname)
        return False
