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
                return FakeResp({"same_measurand": same, "confidence": 0.97,
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
# Both sides parse to a threshold that disagrees -> shape 'different', settled
# in code with no API call. (Until 2026-07-30 this fixture read "Core CPI 0.2%?"
# on the PM side, which does NOT parse — so it was shape 'unknown' and the tests
# below were really asserting the old "unparsed => no call" short-circuit.)
PAIR_DIFF = {"pair_id": "K2|P2", "concept": "cpi",
             "kalshi_text": "CPI MoM above 0.2%", "kalshi_rules": "headline CPI",
             "pm_text": "Will Core CPI MoM be 0.1% or less?",
             "pm_rules": "core CPI", "verdict": "different"}
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
    assert s["classified"] == 2  # PAIR_NEW has no verdict
    assert fc.calls == 1  # PAIR_DIFF is shape-unparsed: code answers, no API call
    rows = store.query("SELECT pair_key, verdict FROM verdicts ORDER BY pair_key")
    assert [r["pair_key"] for r in rows] == ["K1|P1", "K2|P2"]
    # cache: a second run makes ZERO api calls (append-only, budgeted)
    s2 = ai.run_ai(CFG, store, mode="eval", client=fc,
                   candidates_path=path, now=NOW)
    assert fc.calls == 1 and s2["classified"] == 0

    m = ai.evaluate(CFG, store, candidates_path=path)
    # fake says same iff 'unemployment' x2: SAME pair -> TP; DIFF pair -> TN
    assert m["n_scored"] == 2
    assert m["precision"] == 1.0 and m["false_positives"] == 0


def test_per_day_budget_caps_api_calls_not_shape_verdicts(tmp_path):
    # 4b: 248 free code-decided verdicts were burning API budget. The cap is
    # Anthropic spend: only pairs that actually call the API count.
    cfg = {**CFG, "ai_matcher": {**CFG["ai_matcher"], "max_new_pairs_per_day": 1}}
    store = Store(tmp_path / "c.sqlite")
    # sorted order: K1 (needs API), K2 (shape-decided, free), K3 (needs API)
    path = _cands(tmp_path, [PAIR_SAME, PAIR_DIFF, PAIR_NEW])
    fc = FakeClient()
    s = ai.run_ai(cfg, store, mode="sweep", client=fc,
                  candidates_path=path, now=NOW)
    assert fc.calls == 1 and s["budget_exhausted"] is True  # K3 skipped
    rows = {r["pair_key"] for r in store.query("SELECT pair_key FROM verdicts")}
    assert rows == {"K1|P1", "K2|P2"}  # free shape verdict still inserted


def test_spent_today_counts_only_api_spend(tmp_path):
    store = Store(tmp_path / "sp.sqlite")
    common = {"created_at": NOW.isoformat(), "verdict": "different",
              "confidence": "1", "rules_sha": ai._RULES_SHA,
              "model": str(CFG["ai_matcher"]["model"])}
    store.insert("verdicts", {**common, "pair_key": "F1", "cost_usd": "0"})
    store.insert("verdicts", {**common, "pair_key": "F2", "cost_usd": "0.000540"})
    assert ai._spent_today(store, NOW) == 1


def test_evaluate_accepts_inverted_human_verdicts(tmp_path):
    # 4a: a human 'same-inverted' verdict silently vanished from the §18.5
    # denominator; it must score as truth 'same'.
    store = Store(tmp_path / "e.sqlite")
    inv = {**PAIR_SAME, "pair_id": "K4|P4", "verdict": "same-inverted"}
    path = _cands(tmp_path, [inv])
    store.insert("verdicts", {
        "created_at": NOW.isoformat(), "pair_key": "K4|P4",
        "verdict": "same-inverted", "confidence": "0.97",
        "rules_sha": ai._RULES_SHA, "model": str(CFG["ai_matcher"]["model"]),
        "cost_usd": "0.000540"})
    m = ai.evaluate(CFG, store, candidates_path=path)
    assert m["n_scored"] == 1 and m["true_positives"] == 1


def test_cache_lookup_is_one_query_not_per_pair(tmp_path):
    # 4c: one SELECT per candidate into a 13.8 GB DB (~257 round trips/run)
    store = Store(tmp_path / "q.sqlite")
    path = _cands(tmp_path, [PAIR_SAME, PAIR_DIFF, PAIR_NEW])
    fc = FakeClient()
    ai.run_ai(CFG, store, mode="sweep", client=fc, candidates_path=path, now=NOW)
    seen = []
    orig = store.query
    store.query = lambda sql, params=(): (seen.append(sql), orig(sql, params))[1]
    s = ai.run_ai(CFG, store, mode="sweep", client=fc,
                  candidates_path=path, now=NOW)
    assert s["classified"] == 0  # same todo selection: everything cached
    assert sum("verdicts" in q and "pair_key" in q for q in seen) == 1


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
                    return FakeResp({"same_measurand": True,
                                     "confidence": 0.9, "reason": "fake"})
            self.messages = M()

    s = ai.run_ai(CFG, store, mode="sweep", client=FlakyClient(),
                  candidates_path=path, now=NOW)
    # sweep covers BOTH pairs (curated + not); the one error is absorbed
    assert s["classified"] == 1 and s["errors"] == 1


# An unparsed proposition: no threshold regex fires on either side, so
# shape_relation returns 'unknown'. 516 of 573 live candidates looked like this
# on 2026-07-30 — 90% of the surface.
PAIR_UNPARSED = {"pair_id": "K9|P9", "concept": "fed_decision",
                 "kalshi_text": "Fed decision in September",
                 "kalshi_rules": "FOMC target range unemployment",
                 "pm_text": "Will the Fed cut in September? unemployment",
                 "pm_rules": "FOMC"}


def test_unknown_shape_reaches_haiku_instead_of_silent_different(tmp_path):
    """'unknown' means the CODE has no opinion — exactly where the AI's
    judgment is the point. It used to short-circuit to ('different', 1.0) with
    no API call and then cache that, so the 'pending a human look' in
    system_verdict's docstring never happened: _cached_keys excludes the pair
    from every later run."""
    from arb.nonsports.match import shape_relation
    assert shape_relation(
        f"{PAIR_UNPARSED['kalshi_text']} {PAIR_UNPARSED['kalshi_rules']}",
        PAIR_UNPARSED["pm_text"]) == "unknown"
    store = Store(str(tmp_path / "t.sqlite"))
    client = FakeClient()
    out = ai.run_ai(CFG, store, mode="sweep", client=client,
                    candidates_path=_cands(tmp_path, [PAIR_UNPARSED]), now=NOW)
    assert out["classified"] == 1
    assert client.calls == 1, "unknown shape must consult the model"
    v = store.query("SELECT verdict, confidence FROM verdicts")[0]
    # the model says same-measurand, but shape is UNPARSED and boundary logic is
    # exactly where Haiku failed (2026-07-13) — never assert 'same' from here
    assert v["verdict"] == "needs-human"


def test_unknown_shape_with_different_measurand_stays_different(tmp_path):
    """Haiku ruling out the measurand is a safe, cheap 'different' — only the
    same-measurand case needs a human."""
    pair = dict(PAIR_UNPARSED, pair_id="K8|P8",
                kalshi_rules="FOMC target range", pm_text="Will the Fed cut?")
    store = Store(str(tmp_path / "t.sqlite"))
    out = ai.run_ai(CFG, store, mode="sweep", client=FakeClient(),
                    candidates_path=_cands(tmp_path, [pair]), now=NOW)
    assert out["classified"] == 1
    assert store.query("SELECT verdict FROM verdicts")[0]["verdict"] == "different"


def test_needs_human_is_never_scored_as_a_positive(tmp_path):
    """§18.5 precision must not be inflated by the new verdict: 'needs-human'
    is not a claim of 'same', so it can never be a false positive, and when the
    human said 'same' it registers as the miss it is."""
    v, conf, _ = ai.system_verdict("unknown", {"same_measurand": True,
                                               "confidence": 1, "reason": "x"})
    assert not v.startswith("same")
