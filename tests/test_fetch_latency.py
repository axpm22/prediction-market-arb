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

    def fake_get_json(url, cfg):
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
