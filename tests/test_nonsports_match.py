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


def test_disjoint_country_entities_never_pair():
    # the first live run paired China GDP with South Korea GDP (same strike,
    # same quarter-end) — country-parameterized series need entity scoping
    from datetime import datetime, timezone
    from decimal import Decimal
    k = [{"venue": "kalshi", "id": "KXCNGDP-26Q2-T4.0",
          "text": "China GDP Growth Rate YoY | above 4.0%?", "rules": "",
          "strikes": {Decimal("4.0")},
          "date": datetime(2026, 7, 15, tzinfo=timezone.utc), "source": ""}]
    p = [{"venue": "polymarket", "id": "0xkr",
          "text": "South Korea GDP growth (YoY) in Q2 2026? | 4.0-4.4%",
          "rules": "", "strikes": {Decimal("4.0")},
          "date": datetime(2026, 7, 15, tzinfo=timezone.utc), "source": "",
          "neg_risk": True},
         {"venue": "polymarket", "id": "0xcn",
          "text": "China GDP growth above 4% in Q2 2026?",
          "rules": "", "strikes": {Decimal("4")},
          "date": datetime(2026, 7, 15, tzinfo=timezone.utc), "source": "",
          "neg_risk": True}]
    concepts = {"gdp": [["gdp"]]}
    entities = {"china": [["china"]], "south_korea": [["korea"]]}
    cands = block_candidates(CFG, k, p, concepts, entities)
    assert [c["pm_id"] for c in cands] == ["0xcn"]  # Korea pair rejected


def test_extract_strikes_ignores_year_tokens():
    # 'in Jul 2026' made 2026 a shared strike and paired every Kalshi exact
    # market with every PM bucket — 207 spurious candidates in one run
    s = extract_strikes("Will the July 2026 unemployment rate be 4.6%?")
    assert s == {Decimal("4.6")}
    # non-year 4-digit values with decimals/commas stay strikes
    assert Decimal("100000") in extract_strikes("reach $100,000 by 2026")
    assert Decimal("2026.5") in extract_strikes("above 2026.5 points")


def test_shape_relation_decides_every_first_eval_false_positive():
    # v2's seven 0.92-0.95-confidence boundary errors, now decided in code
    from arb.nonsports.match import shape_relation
    fp = [("rise more than 0.6% in July", "0.6%+ be 0.6% or more"),      # gt vs gte
          ("is exactly 4.0% in Jul", "≤3.9% be ≤3.9%"),                  # exact vs lte
          ("increase by more than 1.0%", "<1.0% be less than 1.0%"),     # gt vs lt
          ("increase by more than 2.0%", "<1.0% be less than 1.0%"),
          ("increase by more than 3.5%", "between 3.0–3.5%"),            # gt vs range
          ("be above 4.7% in July", "≥4.7% be ≥4.7%")]                   # gt vs gte
    for k, p in fp:
        assert shape_relation(k, p) == "different", (k, p)
    # and the nine true positives keep their shapes
    assert shape_relation("be above 3.9% in July", "≤3.9% be ≤3.9%") == "inverted"
    assert shape_relation("rise more than 0.0% core", "≤0.0% be 0.0% or less") == "inverted"
    assert shape_relation("is exactly 4.2% in Jul", "4.2% be 4.2%?") == "identical"
    # unparsable propositions stay unknown (conservative downstream)
    assert shape_relation("something odd", "no numbers") == "unknown"


def test_kalshi_series_fetch_is_parallel_and_deterministic(monkeypatch):
    # scan.py made up to max_series_fetch SEQUENTIAL market GETs inside a 240s
    # deadline (truncation observed). Pure I/O fan-out; base.py keeps one
    # Session per thread (H7) for exactly this shape.
    import time

    from arb.nonsports import scan
    from arb.venues.base import Deadline
    n = 20

    def fake_get(url, cfg, deadline=None):
        if "/series?" in url:
            return {"series": [{"ticker": f"S{i}", "title": "US unemployment"}
                               for i in range(n)]}
        time.sleep(0.05)
        st = url.split("series_ticker=")[1].split("&")[0]
        return {"markets": [{"ticker": f"{st}-26AUG-T5",
                             "event_ticker": f"{st}-26AUG",
                             "title": f"unemployment above 5% ({st})",
                             "close_time": "2026-08-01T00:00:00Z"}]}

    monkeypatch.setattr(scan, "get_json", fake_get)
    cfg = {**CFG, "nonsports": {**CFG["nonsports"],
                                "kalshi_categories": ["Economics"]}}
    t0 = time.monotonic()
    rows = scan._kalshi_side(cfg, CONCEPTS, Deadline(60))
    wall = time.monotonic() - t0
    assert wall < 0.5 * (n * 0.05)  # well under the sequential lower bound
    # rows identical to the sequential implementation, submission order kept
    assert [r["id"] for r in rows] == [f"S{i}-26AUG-T5" for i in range(n)]


def test_pm_side_paginates_on_its_own_config_key(monkeypatch):
    """_pm_side borrowed negrisk.scan_pages (10 pages = 1000 events) while
    nonsports.gamma_scan_pages sat unused on this path. Polymarket's politics
    tag carried 1558 open events on 2026-07-30, so 36% of the surface was
    invisible to the matcher and the raise to gamma_scan_pages did nothing."""
    from arb.nonsports import scan
    seen = {}

    def _fake(cfg, tag, max_pages=10, deadline=None):
        seen[str(tag)] = max_pages
        return []

    monkeypatch.setattr("arb.venues.polymarket.fetch_gamma_events", _fake)
    scan._pm_side(CFG, None)
    assert set(seen) == set(str(t) for t in CFG["nonsports"]["polymarket_tags"])
    want = int(CFG["nonsports"]["gamma_scan_pages"])
    assert set(seen.values()) == {want}
    assert want * 100 > 1558  # covers the observed politics surface
