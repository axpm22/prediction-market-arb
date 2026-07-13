"""Phase 1.4 — Haiku classifier stage (rule 11: manual-first, Haiku-second).
Faked client only (rule 2 ethos: no live API in tests); verdicts cache
append-only; per-day budget; eval scores against the human ground truth."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from arb.config import load
from arb.nonsports import ai
from arb.store import Store

ROOT = Path(__file__).resolve().parent.parent
CFG = load(ROOT / "config.yaml")
NOW = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)


class FakeResp:
    def __init__(self, payload):
        self.content = [type("B", (), {"text": json.dumps(payload)})()]
        self.usage = type("U", (), {"input_tokens": 500, "output_tokens": 40})()
        self.stop_reason = "end_turn"


class FakeClient:
    """Says 'same' iff both texts mention 'unemployment'."""

    def __init__(self):
        self.calls = 0

        class M:
            def create(_self, **kw):
                self.calls += 1
                text = kw["messages"][0]["content"]
                same = text.lower().count("unemployment") >= 2
                return FakeResp({"same_event": same, "confidence": 0.97,
                                 "reason": "fake"})
        self.messages = M()


def _cands(tmp_path, entries):
    p = tmp_path / "cands.yaml"
    p.write_text(yaml.safe_dump({e["pair_id"]: e for e in entries}))
    return p


PAIR_SAME = {"pair_id": "K1|P1", "concept": "unemployment",
             "kalshi_text": "US unemployment above 5%",
             "kalshi_rules": "BLS U-3", "pm_text": "US unemployment above 5%?",
             "pm_rules": "BLS", "verdict": "same"}
PAIR_DIFF = {"pair_id": "K2|P2", "concept": "cpi",
             "kalshi_text": "CPI MoM above 0.2%", "kalshi_rules": "headline CPI",
             "pm_text": "Core CPI 0.2%?", "pm_rules": "core CPI",
             "verdict": "different"}
PAIR_NEW = {"pair_id": "K3|P3", "concept": "unemployment",
            "kalshi_text": "US unemployment above 4%",
            "kalshi_rules": "BLS", "pm_text": "US unemployment above 4%?",
            "pm_rules": "BLS"}


def test_live_mode_refuses_while_disabled(tmp_path):
    store = Store(tmp_path / "a.sqlite")
    path = _cands(tmp_path, [PAIR_NEW])
    s = ai.run_ai(CFG, store, mode="live", client=FakeClient(),
                  candidates_path=path, now=NOW)
    assert s["status"] == "disabled"  # ai_matcher.enabled is false (§18.5 gate)
    assert not store.query("SELECT 1 FROM verdicts")


def test_eval_mode_scores_only_curated_pairs_and_caches(tmp_path):
    store = Store(tmp_path / "b.sqlite")
    path = _cands(tmp_path, [PAIR_SAME, PAIR_DIFF, PAIR_NEW])
    fc = FakeClient()
    s = ai.run_ai(CFG, store, mode="eval", client=fc,
                  candidates_path=path, now=NOW)
    assert s["classified"] == 2 and fc.calls == 2  # PAIR_NEW has no verdict
    rows = store.query("SELECT pair_key, verdict FROM verdicts ORDER BY pair_key")
    assert [r["pair_key"] for r in rows] == ["K1|P1", "K2|P2"]
    # cache: a second run makes ZERO api calls (append-only, budgeted)
    s2 = ai.run_ai(CFG, store, mode="eval", client=fc,
                   candidates_path=path, now=NOW)
    assert fc.calls == 2 and s2["classified"] == 0

    m = ai.evaluate(CFG, store, candidates_path=path)
    # fake says same iff 'unemployment' x2: SAME pair -> TP; DIFF pair -> TN
    assert m["n_scored"] == 2
    assert m["precision"] == 1.0 and m["false_positives"] == 0


def test_per_day_budget_caps_calls(tmp_path):
    cfg = {**CFG, "ai_matcher": {**CFG["ai_matcher"], "max_new_pairs_per_day": 1}}
    store = Store(tmp_path / "c.sqlite")
    path = _cands(tmp_path, [PAIR_SAME, PAIR_DIFF])
    fc = FakeClient()
    s = ai.run_ai(cfg, store, mode="eval", client=fc,
                  candidates_path=path, now=NOW)
    assert fc.calls == 1 and s["budget_exhausted"] is True


def test_ai_never_touches_human_verdict_fields(tmp_path):
    store = Store(tmp_path / "d.sqlite")
    path = _cands(tmp_path, [PAIR_SAME])
    ai.run_ai(CFG, store, mode="eval", client=FakeClient(),
              candidates_path=path, now=NOW)
    data = yaml.safe_load(path.read_text())["K1|P1"]
    assert data["verdict"] == "same"           # human field untouched
    assert data["ai_verdict"] in ("same", "different")  # convenience annotation


def test_sweep_mode_classifies_unverdicted_and_tolerates_errors(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    path = _cands(tmp_path, [PAIR_SAME, PAIR_NEW])

    class FlakyClient(FakeClient):
        def __init__(self):
            super().__init__()
            outer = self

            class M:
                def create(_self, **kw):
                    outer.calls += 1
                    if outer.calls == 1:
                        raise RuntimeError("transient api error")
                    return FakeResp({"same_event": True, "confidence": 0.9,
                                     "reason": "fake"})
            self.messages = M()

    s = ai.run_ai(CFG, store, mode="sweep", client=FlakyClient(),
                  candidates_path=path, now=NOW)
    # sweep covers BOTH pairs (curated + not); the one error is absorbed
    assert s["classified"] == 1 and s["errors"] == 1
