import copy
from pathlib import Path

from arb.experimental.niche import (
    enabled_validated_entries,
    load_niche_map,
    validate_entry,
)


def weather_entry() -> dict:
    rb = {"metric": "high_temperature", "city": "austin", "date": "2026-06-24",
          "unit": "fahrenheit", "threshold": 100, "comparator": ">=",
          "source": "NWS Camp Mabry station", "void_rules": "void if no reading"}
    return {
        "id": "w1", "vertical": "weather", "enabled": True, "reviewed": True,
        "required_match": {"metric": "high_temperature", "city": "austin", "unit": "fahrenheit",
                           "threshold": 100, "comparator": ">=", "source": "SAME_REQUIRED"},
        "kalshi": {"rulebook": dict(rb)},
        "polymarket": {"rulebook": dict(rb)},
    }


def worldcup_entry() -> dict:
    rb = {"competition": "fifa world cup 2026", "teams": "france|iraq", "market_type": "total_goals",
          "line": 2.5, "period": "regulation_plus_stoppage", "extra_time_counts": "false",
          "penalties_count": "false", "draw_handling": "n/a", "void_rules": "void if abandoned"}
    return {
        "id": "wc1", "vertical": "worldcup", "enabled": True, "reviewed": True,
        "required_match": {"market_type": "total_goals", "line": 2.5,
                           "period": "regulation_plus_stoppage", "teams": "SAME_REQUIRED"},
        "kalshi": {"rulebook": dict(rb)},
        "polymarket": {"rulebook": dict(rb)},
    }


# ---- accepts when rulebooks truly match ----

def test_weather_matching_rulebooks_approved() -> None:
    assert validate_entry(weather_entry()).approved


def test_worldcup_matching_rulebooks_approved() -> None:
    assert validate_entry(worldcup_entry()).approved


# ---- the safety rule: any mismatch is refused ----

def test_weather_different_source_refused() -> None:
    e = weather_entry()
    e["polymarket"]["rulebook"]["source"] = "Austin city official station"
    v = validate_entry(e)
    assert not v.approved and v.field == "source"


def test_weather_threshold_literal_mismatch_refused() -> None:
    e = weather_entry()
    e["kalshi"]["rulebook"]["threshold"] = 99   # one venue uses a different line
    v = validate_entry(e)
    assert not v.approved and v.field == "threshold"


def test_void_rules_mismatch_always_refused() -> None:
    e = weather_entry()
    e["polymarket"]["rulebook"]["void_rules"] = "no void"
    v = validate_entry(e)
    assert not v.approved and v.field == "void_rules"


def test_worldcup_extra_time_mismatch_refused() -> None:
    e = worldcup_entry()
    e["polymarket"]["rulebook"]["extra_time_counts"] = "true"  # one counts ET, one doesn't
    v = validate_entry(e)
    assert not v.approved and v.field == "extra_time_counts"


def test_missing_rulebook_field_refused() -> None:
    e = weather_entry()
    del e["kalshi"]["rulebook"]["source"]
    v = validate_entry(e)
    assert not v.approved and v.field == "source"


def test_unknown_vertical_refused() -> None:
    e = weather_entry()
    e["vertical"] = "politics"
    assert not validate_entry(e).approved


# ---- enable gating: only enabled+reviewed+approved entries go live ----

def test_enabled_but_invalid_is_refused_not_quoted(tmp_path) -> None:
    import yaml
    e = weather_entry()
    e["polymarket"]["rulebook"]["source"] = "different"   # invalid
    p = tmp_path / "map.yaml"
    p.write_text(yaml.safe_dump([e]))
    assert enabled_validated_entries(p) == []   # refused despite enabled+reviewed


def test_shipped_templates_are_all_disabled() -> None:
    # The committed map ships templates that must never be live until a human fills + reviews.
    entries = load_niche_map(Path("data/nonsports_map.yaml"))
    assert entries, "expected template entries"
    assert all(not e.get("enabled") for e in entries)
    assert {e["vertical"] for e in entries} == {"weather", "worldcup", "macro"}
    assert enabled_validated_entries(Path("data/nonsports_map.yaml")) == []
