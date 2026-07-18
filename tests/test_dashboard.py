"""Dashboard (§14.3): read-only, serves the page + JSON state, 404s the rest."""
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from arb.config import load
from arb.dashboard import make_handler
from arb.store import Store

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def server(tmp_path):
    cfg = load(ROOT / "config.yaml")
    Store(tmp_path / "t.sqlite")  # create schema
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cfg, tmp_path / "t.sqlite"))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def test_page_and_state(server):
    html = urllib.request.urlopen(f"{server}/").read().decode()
    assert "instrument panel" in html.lower()
    assert "gross of carry" in html
    state = json.loads(urllib.request.urlopen(f"{server}/api/state").read())
    assert state["supervisor"] in ("ALIVE", "STALE", "DEAD")
    assert state["headline_pnl"] == "0"
    assert state["ev_enabled"] is False
    assert state["trusted_pairs"] == []


def test_unknown_path_404(server):
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(f"{server}/etc/passwd")
    assert e.value.code == 404


def test_state_lanes_errors_schedule(tmp_path, monkeypatch):
    # UI pass 2026-07-18: per-lane timing, error feed, daily schedule
    import os
    from datetime import datetime, timezone

    from arb.dashboard import state
    cfg = load(ROOT / "config.yaml")
    store = Store(tmp_path / "s.sqlite")
    now = datetime.now(timezone.utc).isoformat()
    store.insert("polls", {"started_at": now, "kind": "watch",
                           "n_quotes": 7, "n_opps": 0})
    logdir = Path(os.environ["ARB_LOG_DIR"]) / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    (logdir / "scan.log").write_text(
        "2026-07-18 00:01:02,003 WARNING arb.venues clob.polymarket.com: "
        "non-retryable TLS failure\n"
        "2026-07-18 00:01:03,000 INFO arb.venues fine\n")
    s = state(cfg, store)
    watch = [ln for ln in s["lanes"] if ln["name"] == "watch"][0]
    assert watch["interval_secs"] == float(cfg["watch_interval_secs"])
    assert watch["last_poll"] == now and watch["quotes_today"] == 7
    assert {ln["name"] for ln in s["lanes"]} >= \
        {k for k, v in cfg["supervisor"]["processes"].items() if v}
    errs = s["errors"]
    assert any("non-retryable TLS failure" in e["msg"] for e in errs)
    assert all(e["level"] != "INFO" for e in errs)
    keys = {sl["key"] for sl in s["schedule"]}
    assert {"settle", "report", "backup"} <= keys
    assert all(0 <= sl["due_in_secs"] <= 86400 for sl in s["schedule"])
