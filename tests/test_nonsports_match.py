"""Phase 1.2 — deterministic blocking-key matcher (rule 11 stage 0).
No fuzzy text matching: concept map + date window + strike equality only.
Fixtures recorded 2026-07-11; synthetic rows for the logic vectors."""
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from arb.config import load
from arb.nonsports.match import (block_candidates, concepts_of, extract_strikes,
                                 kalshi_rows, pm_rows)

ROOT = Path(__file__).resolve().parent.parent
CFG = load(ROOT / "config.yaml")


def _fx(name):
    return json.loads((ROOT / "tests/fixtures" / name).read_text())


CONCEPTS = {
    "unemployment": [["unemployment"]],
    "us_recession": [["recession"]],
    "high_temp_las_vegas": [["maximum", "temperature", "vegas"],
                            ["highest", "temperature", "vegas"]],
    "high_temp_nyc": [["maximum", "temperature", "nyc"]],
}


def test_extract_strikes_vectors():
    assert extract_strikes("Will the unemployment rate (U-3) be above 5.0% "
                           "in November?") == {Decimal("5.0"), Decimal("3")}
    assert Decimal("112") in extract_strikes("maximum temperature be >112° on Jul 12")
    assert Decimal("3.1") in extract_strikes("3.1% or higher")
    assert Decimal("100000") in extract_strikes("reach $100,000 by")
    assert extract_strikes("no numbers here") == set()


def test_concepts_require_all_tokens_of_a_phrase():
    assert concepts_of("Las Vegas Max Daily Temperature | maximum temperature "
                       "be >112", CONCEPTS) == {"high_temp_las_vegas"}
    # a phrase must match wholly — 'temperature' alone never crosses cities
    assert concepts_of("Highest temperature in Austin", CONCEPTS) == set()
    assert concepts_of("US recession by end of 2026?", CONCEPTS) == {"us_recession"}


def test_kalshi_rows_from_fixture_carry_rules_and_series_context():
    mkts = _fx("kalshi_markets_nonsports.json")
    rows = kalshi_rows(mkts["KXU3"] + mkts["KXHIGHTLV"],
                       {"KXU3": "Unemployment",
                        "KXHIGHTLV": "Las Vegas Max Daily Temperature"})
    u3 = [r for r in rows if r["id"].startswith("KXU3")][0]
    assert "unemployment" in u3["text"].lower()
    assert u3["strikes"] and u3["date"] is not None
    assert "Bureau of Labor Statistics" in u3["rules"]
    lv = [r for r in rows if r["id"].startswith("KXHIGHTLV")][0]
    assert "vegas" in lv["text"].lower()  # series title reaches the match text


def test_block_requires_concept_date_and_strike_agreement():
    k = [{"venue": "kalshi", "id": "KXU3-26NOV-T5.0",
          "text": "Unemployment | Will the unemployment rate (U-3) be above "
                  "5.0% in November?",
          "rules": "BLS", "strikes": {Decimal("5.0")},
          "date": datetime(2026, 12, 4, tzinfo=timezone.utc),
          "source": "BLS"}]

    def pm(question, end, strikes):
        return {"venue": "polymarket", "id": f"0x{question[:8]}",
                "text": question, "rules": "", "strikes": strikes,
                "date": end, "source": "", "neg_risk": False}

    good = pm("Will US unemployment be above 5% in November?",
              datetime(2026, 12, 5, tzinfo=timezone.utc), {Decimal("5")})
    wrong_strike = pm("Will US unemployment be above 4% in November?",
                      datetime(2026, 12, 5, tzinfo=timezone.utc), {Decimal("4")})
    wrong_date = pm("Will US unemployment be above 5% in March?",
                    datetime(2027, 4, 5, tzinfo=timezone.utc), {Decimal("5")})
    off_topic = pm("Will it rain in November?",
                   datetime(2026, 12, 5, tzinfo=timezone.utc), set())

    cands = block_candidates(CFG, k, [good, wrong_strike, wrong_date, off_topic],
                             {"unemployment": [["unemployment"]]})
    assert len(cands) == 1
    c = cands[0]
    assert c["kalshi_id"] == "KXU3-26NOV-T5.0" and c["pm_id"] == good["id"]
    assert c["concept"] == "unemployment"
    assert "verdict" not in c  # curation appends verdicts; the blocker never does


def test_block_strike_5_matches_5point0():
    # Decimal normalization: '5' and '5.0' are the same strike
    assert Decimal("5") in {Decimal("5.0")}


def test_merge_never_touches_curated_entries(tmp_path):
    # verdicts are append-only truth: a re-run must not clobber them
    import yaml

    from arb.nonsports.scan import merge_candidates
    p = tmp_path / "cands.yaml"
    p.write_text(yaml.safe_dump({"K1|P1": {"pair_id": "K1|P1",
                                           "verdict": "same",
                                           "verdict_note": "checked 7 points"}}))
    merged, added = merge_candidates(
        p, [{"pair_id": "K1|P1", "concept": "x"},          # already curated
            {"pair_id": "K2|P2", "concept": "y"}])          # genuinely new
    assert added == 1
    assert merged["K1|P1"]["verdict"] == "same"             # untouched
    assert merged["K1|P1"]["verdict_note"] == "checked 7 points"
    assert merged["K2|P2"]["concept"] == "y"
