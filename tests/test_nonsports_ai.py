"""Haiku-decided non-sports matching (spec amendment): no live Anthropic or venue calls —
canned verdicts + fixture payloads. Pins: high-conf true => ai_matched event with REVIEW
opportunities recorded outside the headline; low-conf true => not matched; garbage =>
equivalent=null after one retry; cached pair => zero API calls on the second poll."""
import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import arb.nonsports_ai as nai
from arb.experimental.nonsports import ai_matched_entries, detect_nonsports
from arb.nonsports_ai import classify_pair, pair_key, resolve_verdict, verdict_matches
from arb.store import Store
from arb.tracker import record_tracker_poll, write_daily_report

from tests.test_tracker import quote

NOW = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)
AI_CFG = {"enabled": True, "model": "claude-haiku-4-5", "min_confidence": 0.85,
          "max_new_pairs_per_day": 100, "max_rules_chars": 8000}
CONTRACT_A = {"venue": "kalshi", "id": "KXCPI", "title": "CPI above 3% in June?",
              "rules": "Resolves YES if BLS CPI YoY for June 2026 exceeds 3.0%."}
CONTRACT_B = {"venue": "polymarket", "id": "77001", "title": "June CPI over 3%?",
              "rules": "Resolves to Yes if the BLS June 2026 CPI YoY print is above 3.0%."}


def canned(verdict_json: str, calls: list | None = None):
    """Stub Anthropic client returning a fixed response; counts calls."""
    def create(**kwargs):
        (calls if calls is not None else []).append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=verdict_json)])
    return SimpleNamespace(messages=SimpleNamespace(create=create))


def good_verdict(confidence: float) -> str:
    return json.dumps({
        "a": {"entity": "June 2026 CPI", "metric": "YoY > 3.0%", "deadline": "2026-07-15",
              "source": "BLS", "edge_cases": ""},
        "b": {"entity": "June 2026 CPI", "metric": "YoY > 3.0%", "deadline": "2026-07-15",
              "source": "BLS", "edge_cases": ""},
        "equivalent": True, "confidence": confidence, "differences": [],
    })


def test_high_conf_true_matches_and_low_conf_does_not(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    row = resolve_verdict(store, AI_CFG, CONTRACT_A, CONTRACT_B, NOW,
                          client_factory=lambda cfg: canned(good_verdict(0.93)))
    assert verdict_matches(row, AI_CFG["min_confidence"])

    low = {**CONTRACT_B, "id": "77002"}
    row = resolve_verdict(store, AI_CFG, CONTRACT_A, low, NOW,
                          client_factory=lambda cfg: canned(good_verdict(0.60)))
    assert row["equivalent"] == 1 and not verdict_matches(row, AI_CFG["min_confidence"])


def test_garbage_records_null_after_one_retry(tmp_path) -> None:
    calls: list = []
    verdict = classify_pair(canned("not json at all", calls), "claude-haiku-4-5",
                            CONTRACT_A, CONTRACT_B, 8000)
    assert len(calls) == 2                       # retried exactly once
    assert verdict["equivalent"] is None and verdict["confidence"] == 0


def test_cached_pair_makes_zero_api_calls_on_second_poll(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    calls: list = []
    factory = lambda cfg: canned(good_verdict(0.93), calls)  # noqa: E731
    resolve_verdict(store, AI_CFG, CONTRACT_A, CONTRACT_B, NOW, factory)
    assert len(calls) == 1
    row = resolve_verdict(store, AI_CFG, CONTRACT_A, CONTRACT_B, NOW, factory)  # poll 2
    assert len(calls) == 1                       # cache hit, no new call
    assert verdict_matches(row, AI_CFG["min_confidence"])


def test_api_errors_give_up_permanently_after_three_attempts(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    nai._attempts.clear()

    def exploding(cfg):
        raise TimeoutError("api down")

    a, b = CONTRACT_A, {**CONTRACT_B, "id": "77003"}
    assert resolve_verdict(store, AI_CFG, a, b, NOW, exploding) is None   # attempt 1
    assert resolve_verdict(store, AI_CFG, a, b, NOW, exploding) is None   # attempt 2
    row = resolve_verdict(store, AI_CFG, a, b, NOW, exploding)            # attempt 3: null verdict
    assert row is not None and row["equivalent"] is None


def test_daily_budget_blocks_new_pairs(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    cfg = {**AI_CFG, "max_new_pairs_per_day": 1}
    resolve_verdict(store, cfg, CONTRACT_A, CONTRACT_B, NOW,
                    lambda c: canned(good_verdict(0.93)))
    over = resolve_verdict(store, cfg, CONTRACT_A, {**CONTRACT_B, "id": "77004"}, NOW,
                           lambda c: canned(good_verdict(0.93)))
    assert over is None                          # budget reached, no verdict recorded


def _pm_event(pm_id: str = "77001"):
    return {"id": pm_id, "title": "CPI above 3% in June 2026?", "description": CONTRACT_B["rules"],
            "markets": [{"outcomes": '["Yes", "No"]', "clobTokenIds": '["tokY", "tokN"]'}]}


def test_two_poll_pipeline_matches_and_caches(tmp_path, monkeypatch) -> None:
    store = Store(tmp_path / "arb.sqlite")
    close = (NOW + timedelta(days=3)).date().isoformat()
    market = {"ticker": "KXCPI-26JUL", "close_time": f"{close}T14:00:00Z",
              "rules_primary": CONTRACT_A["rules"]}
    # Single open market on each venue -> the one-shot path (not the strike stage).
    monkeypatch.setattr("arb.experimental.nonsports._kalshi_open_markets", lambda s: [dict(market)])
    calls: list = []
    kwargs = dict(
        ncfg={"candidate_threshold": 85, "kalshi_categories": [], "polymarket_tags": []},
        human_entries=[], max_days=7,
        kalshi_titles=[("KXCPI", "CPI above 3% in June?")],
        polymarket_events=[_pm_event()],
        client_factory=lambda cfg: canned(good_verdict(0.93), calls),
    )
    entries = ai_matched_entries(store, AI_CFG, **kwargs)
    assert len(calls) == 1 and len(entries) == 1
    assert entries[0]["ai_matched"] and entries[0]["polymarket_tokens"] == {"YES": "tokY", "NO": "tokN"}

    entries2 = ai_matched_entries(store, AI_CFG, **kwargs)   # poll 2
    assert len(calls) == 1                                    # zero matcher API calls (cache)
    assert entries2 == entries


def test_ai_matched_flows_review_ledger_and_report_not_headline(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    key = "nonsports:ai:KXCPI-26JUL:77001"
    legs = [replace(quote("YES", "0.44"), event_key=key, notes=("nonsports", "ai-matched")),
            replace(quote("NO", "0.44"), event_key=key, venue="polymarket", notes=("nonsports", "ai-matched"))]
    config = SimpleNamespace(bankroll_usd=Decimal("1000"), max_stake_pct=Decimal("0.01"),
                             arb_stale_warn=Decimal("0.10"), divergence_threshold=Decimal("0.05"))
    opps = detect_nonsports(legs, config, ai_confidence={key: "0.93"})
    assert opps, "arb pair below $1 must flag"
    assert all(o.status == "REVIEW" for o in opps)
    assert all(o.event.ai_matched for o in opps)
    assert any(n.startswith("ai-matched conf=0.93") for n in opps[0].notes)

    record_tracker_poll(store, store.start_poll("nonsports"), opps, now=NOW)
    with store.connect() as con:
        row = con.execute("SELECT ai_matched, status, category FROM flat_bets WHERE kind != 'CLOSE'").fetchone()
    assert row["ai_matched"] == 1 and row["category"] == "nonsports"

    _csv, md_path = write_daily_report(store, report_date=date(2026, 7, 2), output_dir=tmp_path)
    report = md_path.read_text()
    assert "## AI-Matched (Haiku)" in report
    headline = report.split("## Class x Fill x Trust")[0]
    assert "$0.00" in headline or "0 bets" in headline.lower() or "Optimistic" in headline


def test_billing_error_defers_without_burning_attempts(tmp_path) -> None:
    import pytest
    from arb.nonsports_ai import MatcherUnavailable

    store = Store(tmp_path / "arb.sqlite")
    nai._attempts.clear()

    def broke(cfg):
        exc = RuntimeError("credit balance too low")
        exc.status_code = 400
        return SimpleNamespace(messages=SimpleNamespace(create=lambda **k: (_ for _ in ()).throw(exc)))

    with pytest.raises(MatcherUnavailable):
        resolve_verdict(store, AI_CFG, CONTRACT_A, CONTRACT_B, NOW, broke)
    assert not nai._attempts                       # no attempts burned
    with store.connect() as con:                   # and no verdict recorded
        assert con.execute("SELECT COUNT(*) FROM nonsports_verdicts").fetchone()[0] == 0
