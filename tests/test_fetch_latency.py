"""H7 — fetch latency: thread-local Session reuse, per-venue deadline truncation
to partial (ok:false), and watch keeping partial quotes. Fixtures only; NO live
endpoints (rule 2)."""
import threading

import pytest


def test_request_reuses_one_session_per_thread(monkeypatch):
    import arb.venues.base as base
    made = []

    class FakeResp:
        status_code = 200

        def json(self):
            return {"data": []}

    class FakeSession:
        def __init__(self):
            made.append(self)
            self.calls = 0

        def request(self, *a, **k):
            self.calls += 1
            return FakeResp()

    monkeypatch.setattr(base, "_local", threading.local())  # isolate from prior tests
    monkeypatch.setattr(base.requests, "Session", FakeSession)
    cfg = {"execution": {"venue_fetch_timeout_secs": 5, "venue_max_retries": 0}}
    base.get_json("http://a", cfg)
    base.get_json("http://b", cfg)
    assert len(made) == 1          # ONE session created, not one per call
    assert made[0].calls == 2      # both requests went through it


def test_venue_deadline_stops_pagination_and_raises_partial(monkeypatch):
    import arb.venues.sx as sx
    from arb.venues.base import PartialFetch

    class FakeDeadline:
        def __init__(self, secs):
            self.checks = 0

        def expired(self):
            self.checks += 1
            return self.checks > 2  # expire on the 3rd check

    calls = {"n": 0}

    def fake_get_json(url, cfg, deadline=None):
        calls["n"] += 1
        if "leagues" in url:
            return {"data": [{"leagueId": 1}]}
        # a page that WOULD paginate forever (nextKey never clears)
        return {"data": {"markets": [{"marketHash": "0xabc"}], "nextKey": "MORE"}}

    monkeypatch.setattr(sx, "Deadline", FakeDeadline)
    monkeypatch.setattr(sx, "get_json", fake_get_json)
    cfg = {"execution": {"venue_deadline_secs": 1}, "sports": ["baseball_mlb"]}
    with pytest.raises(PartialFetch):
        sx.fetch_markets(cfg)
    assert calls["n"] <= 3  # stopped early — did NOT paginate "MORE" indefinitely


def test_partial_fetch_keeps_quotes_but_marks_venue_not_ok(monkeypatch):
    import arb.watch as watch
    from arb.venues.base import PartialFetch

    def truncated(cfg):
        raise PartialFetch(["q1", "q2"])

    monkeypatch.setattr(watch, "VENUES", {"sx": truncated})
    quotes, status = watch.fetch_all_venues({})
    assert quotes["sx"] == ["q1", "q2"]                    # persisted (rule 7)
    assert status["sx"]["ok"] is False                     # not a full-coverage poll
    assert status["sx"]["partial"] is True                 # §10.2 lifetime-carry applies


def test_request_deadline_trims_timeout_and_skips_retries(monkeypatch):
    # H7 follow-up: retries never consulted the deadline — one slow request
    # could stretch a 30s venue budget to ~77s (3 x 15s + backoff)
    import arb.venues.base as base
    seen = []

    class FakeSession:
        def request(self, method, url, timeout=None, **k):
            seen.append(timeout)
            raise base.requests.RequestException("boom")

    monkeypatch.setattr(base, "_local", threading.local())
    monkeypatch.setattr(base.requests, "Session", FakeSession)
    cfg = {"execution": {"venue_fetch_timeout_secs": 15, "venue_max_retries": 2}}

    class OneShotDeadline:  # 2s left before the first attempt, expired after it
        def __init__(self):
            self.values = [2.0]

        def remaining(self):
            return self.values.pop(0) if self.values else -1.0

        def expired(self):
            return not self.values

    with pytest.raises(base.VenueError):
        base._request("GET", "http://x", cfg, deadline=OneShotDeadline())
    assert seen == [2.0]  # timeout trimmed to the remaining budget; NO retries

    class ExpiredDeadline:
        def remaining(self):
            return -1.0

        def expired(self):
            return True

    seen.clear()
    with pytest.raises(base.VenueError):
        base._request("GET", "http://x", cfg, deadline=ExpiredDeadline())
    assert seen == []  # budget spent: no request fired at all


def test_gamma_pagination_cap_is_loud(monkeypatch, caplog):
    # hitting the page cap with more pages pending used to end the loop
    # silently — venue status stayed ok:true on truncated coverage
    import arb.venues.polymarket as pm
    monkeypatch.setattr(pm, "get_json",
                        lambda url, cfg, deadline=None: [{"id": i} for i in range(100)])
    with caplog.at_level("WARNING"):
        events = pm.fetch_gamma_events({}, "mlb", max_pages=3)
    assert len(events) == 300
    assert any("pagination cap" in r.message for r in caplog.records)


def test_kalshi_deadline_and_pagination_cap(monkeypatch, caplog):
    # kalshi was the only venue with no deadline at all (H7 applied to 2 of 3)
    import arb.venues.kalshi as k
    from arb.venues.base import PartialFetch
    calls = {"n": 0}

    def fake_get(url, cfg, deadline=None):
        calls["n"] += 1
        return {"markets": [], "cursor": "MORE"}

    monkeypatch.setattr(k, "get_json", fake_get)
    cfg0 = {"execution": {"venue_deadline_secs": 0, "venue_fetch_timeout_secs": 1,
                          "venue_max_retries": 0}, "sports": ["baseball_mlb"]}
    with pytest.raises(PartialFetch):
        k.fetch_markets(cfg0)
    assert calls["n"] == 0  # budget spent before the first request

    cfg1 = {**cfg0, "execution": {**cfg0["execution"], "venue_deadline_secs": 60}}
    with caplog.at_level("WARNING"):
        k.fetch_markets(cfg1)  # endless cursor: cap must end the loop LOUDLY
    assert any("pagination cap" in r.message for r in caplog.records)
