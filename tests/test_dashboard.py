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
