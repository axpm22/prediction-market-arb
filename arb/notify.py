"""Outbound webhook notifier (CLAUDE.md rule 1): POSTs to the operator's OWN
`notify.webhook_url`, never a market-data endpoint, and never fetches or
influences prices. Every call is defensive — a dead/misconfigured webhook must
never break the caller that triggered it.
"""
from __future__ import annotations

import json
import logging
import socket
import time
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# per-event throttle: event -> monotonic time of the last attempt (rule: a
# flapping alarm cannot spam the endpoint). Module-level by design — one
# process, one notifier.
_last_sent: dict[str, float] = {}


def _default_poster(url: str, body: bytes, timeout: float) -> None:
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=timeout).close()


def _json_default(o):
    if isinstance(o, Decimal):
        return str(o)
    raise TypeError(f"not JSON serializable: {o!r}")


def post(cfg, event: str, payload: dict, poster=None) -> bool:
    """JSON POST `{event, at, host, head, payload}` to `cfg["notify"]["webhook_url"]`.
    Never raises: any failure (config, serialization, network) logs one WARNING
    naming only the target host — the URL itself is treated as a secret — and
    returns False."""
    n = cfg["notify"]
    url = str(n["webhook_url"])
    if not n["enabled"] or not url:
        return False
    now = time.monotonic()
    interval = float(n["min_interval_secs"])
    last = _last_sent.get(event)
    if last is not None and now - last < interval:
        return False
    _last_sent[event] = now
    try:
        from arb.cli import head_sha  # local import: cli.py is the entrypoint module
        body = json.dumps({
            "event": event, "at": datetime.now(timezone.utc).isoformat(),
            "host": socket.gethostname(), "head": head_sha(), "payload": payload,
        }, default=_json_default).encode()
        (poster or _default_poster)(url, body, 10.0)
        return True
    except Exception as e:  # noqa: BLE001 — a notifier failure must never raise
        logger.warning("notify: POST to %s failed: %s",
                       urlsplit(url).hostname or "?", e)
        return False
