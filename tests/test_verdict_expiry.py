"""MODEL_FOUNDATIONS Phase E — AI verdicts expire when the rules text changes (fixtures-free)."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace

from arb.nonsports_ai import resolve_verdict, rules_sha, verdict_is_current
from arb.store import Store

NOW = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)
CFG = {"enabled": True, "model": "claude-haiku-4-5", "min_confidence": 0.85,
       "max_new_pairs_per_day": 100, "max_rules_chars": 8000}
A = {"venue": "kalshi", "id": "KXCPI", "title": "CPI>3%?", "rules": "Resolves YES if CPI YoY > 3.0%."}
B = {"venue": "polymarket", "id": "77001", "title": "CPI>3%?", "rules": "Resolves Yes if CPI YoY above 3.0%."}


def _verdict(equiv=True):
    return json.dumps({"a": {}, "b": {}, "equivalent": equiv, "confidence": 0.95, "differences": []})


def _client(calls):
    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=_verdict())])
    return SimpleNamespace(messages=SimpleNamespace(create=create))


def test_rules_sha_is_whitespace_stable():
    assert rules_sha("a  b\n c") == rules_sha("a b c")
    assert rules_sha("x") != rules_sha("y")


def test_cached_verdict_reused_when_rules_unchanged(tmp_path):
    store = Store(tmp_path / "arb.sqlite")
    calls = []
    resolve_verdict(store, CFG, A, B, NOW, client_factory=lambda cfg: _client(calls))
    resolve_verdict(store, CFG, A, B, NOW, client_factory=lambda cfg: _client(calls))  # poll 2
    assert len(calls) == 1                       # unchanged rules -> zero new API calls


def test_rules_change_expires_cache_and_reverdicts(tmp_path):
    store = Store(tmp_path / "arb.sqlite")
    calls = []
    row = resolve_verdict(store, CFG, A, B, NOW, client_factory=lambda cfg: _client(calls))
    assert row["equivalent"] == 1 and len(calls) == 1
    # Venue B edits its rules text -> cached verdict is stale -> re-verdict (a new API call).
    B2 = {**B, "rules": "Resolves Yes ONLY on the FINAL revised CPI print (not the first release)."}
    assert not verdict_is_current(row, A["rules"], B2["rules"])
    resolve_verdict(store, CFG, A, B2, NOW, client_factory=lambda cfg: _client(calls))
    assert len(calls) == 2                       # rules changed -> cache miss -> re-verdict
