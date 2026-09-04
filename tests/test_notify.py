"""Outbound webhook notifier (arb/notify.py) and its hook points: `_notify_daily`
(arb/commands.py, cmd_report) and the stale-lane alarm (arb/heartbeat.py,
run_supervisor). No network: every test injects the poster or monkeypatches
`arb.notify._default_poster`."""
import json
import logging
import socket
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from arb import notify
from arb.config import ConfigError, load
from arb.store import Store

from tests.test_whale_lane import cfg  # noqa: F401 — full loaded config, whale-tuned
from tests.test_whale_report import DAY, _whale_row

ROOT = Path(__file__).resolve().parent.parent
SHIPPED = ROOT / "config.yaml"


@pytest.fixture(autouse=True)
def _fresh_throttle():
    """The throttle is module-level by design (rule: one process, one
    notifier) — reset it around each test so tests don't bleed into each other."""
    notify._last_sent.clear()
    yield
    notify._last_sent.clear()


def _ncfg(enabled=True, url="http://example.test/hook", interval=300, events=None,
         timeout=10):
    return {"notify": {"enabled": enabled, "webhook_url": url,
                       "min_interval_secs": interval, "timeout_secs": timeout,
                       "events": events if events is not None else
                       ["daily_report", "kill_rule_fired"]}}


# ------------------------------------------------------------------- post()

def test_disabled_makes_no_call():
    calls = []
    ok = notify.post(_ncfg(enabled=False), "daily_report", {},
                     poster=lambda *a: calls.append(a))
    assert ok is False and calls == []


def test_empty_url_makes_no_call():
    calls = []
    ok = notify.post(_ncfg(url=""), "daily_report", {},
                     poster=lambda *a: calls.append(a))
    assert ok is False and calls == []


def test_enabled_posts_the_documented_body_shape_with_decimals_as_strings():
    calls = []
    ok = notify.post(_ncfg(), "daily_report", {"pnl": Decimal("12.340000")},
                     poster=lambda url, body, timeout: calls.append((url, body, timeout)))
    assert ok is True
    url, raw, timeout = calls[0]
    assert url == "http://example.test/hook" and timeout == 10.0
    body = json.loads(raw)
    assert set(body) == {"event", "at", "host", "head", "payload"}
    assert body["event"] == "daily_report"
    assert body["host"] == socket.gethostname()
    assert isinstance(body["head"], str) and body["head"]
    assert body["payload"] == {"pnl": "12.340000"}      # Decimal -> str
    datetime.fromisoformat(body["at"])                   # ISO UTC, parses clean


def test_poster_raising_returns_false_and_does_not_raise():
    def boom(url, body, timeout):
        raise RuntimeError("connection refused")
    assert notify.post(_ncfg(), "daily_report", {}, poster=boom) is False


def test_throttle_skips_a_repeat_of_the_same_event_but_not_a_different_one():
    calls = []
    cfg = _ncfg(interval=300)
    poster = lambda url, body, timeout: calls.append(json.loads(body)["event"])
    notify.post(cfg, "daily_report", {}, poster=poster)
    notify.post(cfg, "daily_report", {}, poster=poster)   # within the window: skipped
    notify.post(cfg, "kill_rule_fired", {}, poster=poster)  # different event: goes through
    assert calls == ["daily_report", "kill_rule_fired"]


def test_throttle_keys_on_caller_supplied_entity_not_just_event():
    # I2: two distinct entities sharing one event (two silent lanes, two fired
    # rules) must not collapse into a single throttled send
    calls = []
    cfg = _ncfg(interval=300)
    poster = lambda url, body, timeout: calls.append(json.loads(body)["payload"]["lane"])
    notify.post(cfg, "lane_silent", {"lane": "negrisk"}, key="negrisk", poster=poster)
    notify.post(cfg, "lane_silent", {"lane": "watch"}, key="watch", poster=poster)
    # same entity again within the window: skipped
    notify.post(cfg, "lane_silent", {"lane": "negrisk"}, key="negrisk", poster=poster)
    assert calls == ["negrisk", "watch"]


def test_post_uses_the_configured_timeout_secs():
    calls = []
    notify.post(_ncfg(timeout=7), "daily_report", {},
               poster=lambda url, body, timeout: calls.append(timeout))
    assert calls == [7.0]


def test_the_url_never_appears_in_a_log_record():
    def boom(url, body, timeout):
        raise RuntimeError("boom")
    logger = logging.getLogger("arb.notify")
    records = []
    handler = logging.Handler()
    handler.emit = lambda r: records.append(r)
    logger.addHandler(handler)
    try:
        notify.post(_ncfg(url="http://example.test/super-secret-path"), "daily_report",
                   {}, poster=boom)
    finally:
        logger.removeHandler(handler)
    assert records, "expected a warning to be logged"
    for r in records:
        msg = r.getMessage()
        assert "super-secret-path" not in msg
        assert "http://" not in msg


def test_a_url_embedded_in_the_exception_message_is_scrubbed():
    # I1: urlopen's own ValueError for a malformed URL quotes the full URL
    # inside its message ("unknown url type: '<url>'") — that raw message must
    # never be logged verbatim
    def boom(url, body, timeout):
        raise ValueError(f"unknown url type: {url!r}")
    logger = logging.getLogger("arb.notify")
    records = []
    handler = logging.Handler()
    handler.emit = lambda r: records.append(r)
    logger.addHandler(handler)
    try:
        ok = notify.post(_ncfg(url="http://example.test/super-secret-path"),
                         "daily_report", {}, poster=boom)
    finally:
        logger.removeHandler(handler)
    assert ok is False
    assert records, "expected a warning to be logged"
    for r in records:
        msg = r.getMessage()
        assert "super-secret-path" not in msg
        assert "ValueError" in msg   # type(e).__name__ still surfaces


def test_malformed_webhook_url_is_rejected_before_any_network_call():
    # I1(c): validated up front — no urlopen crash, no raw URL in the log
    logger = logging.getLogger("arb.notify")
    records = []
    handler = logging.Handler()
    handler.emit = lambda r: records.append(r)
    logger.addHandler(handler)
    calls = []
    try:
        ok = notify.post(_ncfg(url="not-a-url-with-secret-token-9f8e"), "daily_report",
                         {}, poster=lambda *a: calls.append(a))
    finally:
        logger.removeHandler(handler)
    assert ok is False
    assert calls == []          # never reaches the poster
    assert records, "expected a warning to be logged"
    for r in records:
        assert "secret-token-9f8e" not in r.getMessage()


def test_notify_logger_uses_the_shared_file_logging_setup():
    # I1(a): arb.notify.get_logger("arb.notify", "logs/scan.log"), same as
    # every other module — file handler + the repo's redacting filter, not a
    # bare logging.getLogger() with no handlers
    from logging.handlers import TimedRotatingFileHandler
    logger = logging.getLogger("arb.notify")
    assert logger.name == "arb.notify"
    assert any(isinstance(h, TimedRotatingFileHandler) for h in logger.handlers)


# ------------------------------------------------------------------- config

def test_config_loads_with_the_notify_block():
    c = load(SHIPPED)
    assert c["notify"] == {
        "enabled": False, "webhook_url": "", "min_interval_secs": 300,
        "timeout_secs": 10,
        "events": ["daily_report", "kill_rule_fired", "lane_silent",
                  "supervisor_started", "supervisor_stopped"]}


def test_notify_unknown_key_is_startup_error(tmp_path):
    text = SHIPPED.read_text().replace(
        'webhook_url: ""', 'webhook_url: ""\n  bogus_notify_key: 1')
    bad = tmp_path / "config.yaml"
    bad.write_text(text)
    with pytest.raises(ConfigError, match="bogus_notify_key"):
        load(bad)


# --------------------------------------------------------- _notify_daily()

def test_notify_daily_posts_the_summary(cfg, tmp_path, monkeypatch):
    from arb.commands import _notify_daily
    from arb.report import write_daily
    calls = []
    monkeypatch.setattr(notify, "_default_poster",
                        lambda url, body, timeout: calls.append(json.loads(body)))
    cfg["notify"]["enabled"] = True
    cfg["notify"]["webhook_url"] = "http://example.test/hook"
    store = Store(tmp_path / "t.sqlite")
    _whale_row(store, "V2/flat", 1, "pm:a")

    frame = write_daily(cfg, store, DAY, outdir=tmp_path / "out")
    _notify_daily(cfg, frame, frame["whale"])

    assert [c["event"] for c in calls] == ["daily_report"]
    payload = calls[0]["payload"]
    assert payload["day"] == DAY
    assert payload["headline_pnl"] == "0"
    assert payload["whale"]["bets"] == {"V2/flat": 1}


def test_notify_daily_posts_kill_rule_fired_when_a_rule_fires(cfg, tmp_path, monkeypatch):
    from arb.commands import _notify_daily
    from arb.report import write_daily
    calls = []
    monkeypatch.setattr(notify, "_default_poster",
                        lambda url, body, timeout: calls.append(json.loads(body)))
    cfg["notify"]["enabled"] = True
    cfg["notify"]["webhook_url"] = "http://example.test/hook"
    cfg["whale"]["kill"]["decision_n"] = 3
    store = Store(tmp_path / "t.sqlite")
    for sid in range(1, 4):
        _whale_row(store, "V2/flat", sid, f"pm:{sid}", pnl="-0.20")

    frame = write_daily(cfg, store, DAY, outdir=tmp_path / "out")
    _notify_daily(cfg, frame, frame["whale"])

    fired = [c["payload"] for c in calls if c["event"] == "kill_rule_fired"]
    assert len(fired) == 1
    assert fired[0]["rule"] == 1 and fired[0]["state"] == "FIRED"


# --------------------------------------------------------- supervisor hooks

def test_stale_lane_alarm_posts_lane_silent_once(cfg, tmp_path, monkeypatch):
    """Reuses the run_supervisor(once=True) harness from test_ops.py's
    test_supervisor_children_inherit_db_and_config: Popen faked so nothing
    real spawns, a stale `negrisk` poll row seeded so the alarm fires."""
    import arb.heartbeat as hb
    from datetime import timedelta, timezone

    class FakeProc:
        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    calls = []
    monkeypatch.setattr(notify, "_default_poster",
                        lambda url, body, timeout: calls.append(json.loads(body)))
    cfg["notify"]["enabled"] = True
    cfg["notify"]["webhook_url"] = "http://example.test/hook"
    monkeypatch.setattr(hb, "LOCK_DIR", tmp_path / "lock")
    monkeypatch.setattr(hb, "STOP_FLAG", tmp_path / "absent-stop")
    monkeypatch.setattr(hb.subprocess, "Popen", lambda argv: FakeProc())
    db_path = tmp_path / "t.sqlite"
    store = Store(db_path)
    old = datetime.now(timezone.utc) - timedelta(days=30)
    store.insert("polls", {"started_at": old.isoformat(), "kind": "negrisk",
                           "n_quotes": 1, "n_opps": 0})

    hb.run_supervisor(cfg, once=True, db=str(db_path))

    lane_calls = [c["payload"] for c in calls if c["event"] == "lane_silent"]
    assert len(lane_calls) == 1
    assert lane_calls[0]["lane"] == "negrisk"
    # started/stopped fire too — same run, same fake poster, no extra plumbing
    assert "supervisor_started" in [c["event"] for c in calls]
    assert "supervisor_stopped" in [c["event"] for c in calls]


def test_stale_lane_alarm_posts_lane_silent_for_each_distinct_lane(cfg, tmp_path, monkeypatch):
    """I2: the throttle used to key on event alone, so two lanes going silent
    in the same check collapsed into a single lane_silent POST — only
    whichever lane's alarm fired first ever reached the webhook."""
    import arb.heartbeat as hb
    from datetime import timedelta, timezone

    class FakeProc:
        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    calls = []
    monkeypatch.setattr(notify, "_default_poster",
                        lambda url, body, timeout: calls.append(json.loads(body)))
    cfg["notify"]["enabled"] = True
    cfg["notify"]["webhook_url"] = "http://example.test/hook"
    monkeypatch.setattr(hb, "LOCK_DIR", tmp_path / "lock")
    monkeypatch.setattr(hb, "STOP_FLAG", tmp_path / "absent-stop")
    monkeypatch.setattr(hb.subprocess, "Popen", lambda argv: FakeProc())
    db_path = tmp_path / "t.sqlite"
    store = Store(db_path)
    old = datetime.now(timezone.utc) - timedelta(days=30)
    for kind in ("negrisk", "political"):
        store.insert("polls", {"started_at": old.isoformat(), "kind": kind,
                               "n_quotes": 1, "n_opps": 0})

    hb.run_supervisor(cfg, once=True, db=str(db_path))

    lane_calls = [c["payload"]["lane"] for c in calls if c["event"] == "lane_silent"]
    assert sorted(lane_calls) == ["negrisk", "political"]
