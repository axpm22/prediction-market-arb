import json
from pathlib import Path

from arb.config import load_config
from arb.experimental.btc_sweep import parse_kalshi_btc, parse_polymarket_btc, run_btc_pass
from arb.store import Store

FIXTURES = Path("tests/fixtures")


def _load(name):
    return json.loads((FIXTURES / name).read_text())


def test_parsers_handle_recorded_fixtures() -> None:
    # Real recorded responses parse into strike+window-keyed UP/DOWN legs without crashing.
    k = parse_kalshi_btc(_load("kalshi_btc.json"))
    p = parse_polymarket_btc(_load("polymarket_btc.json"))
    assert all(leg.asset.startswith("BTC@") for leg in k + p)
    assert {leg.direction for leg in k} <= {"UP", "DOWN"}
    # Strike is parsed from $-amounts / Kalshi T-segments, never bare numbers (dates).
    assert all("@" in leg.asset for leg in k + p)


def test_sweep_emits_review_arb_btc_flag_on_matching_strike_window(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    config = load_config()
    # Same strike + same window; still clears after Kalshi + Polymarket crypto fees.
    kalshi = {"markets": [{
        "ticker": "KXBTCD-26JUN2217-T73000", "yes_sub_title": "$73,000 or above",
        "close_time": "2026-06-22T21:00:00Z", "yes_ask_dollars": "0.40", "no_ask_dollars": "0.55",
    }]}
    poly = {"markets": [{
        "question": "Will Bitcoin reach $73,000?", "groupItemTitle": "↑ 73,000",
        "endDate": "2026-06-22T21:00:00Z", "bestAsk": 0.60, "bestBid": 0.60,  # DOWN = 1-0.60 = 0.40
    }]}
    n_legs, found = run_btc_pass(store, config, kalshi_fetch=lambda: kalshi, poly_fetch=lambda: poly)
    assert n_legs == 4 and found >= 1
    with store.connect() as con:
        rows = list(con.execute("SELECT * FROM flat_bets WHERE kind='ARB_BTC'"))
    assert rows

    def _venues(r):
        legs = json.loads(r["legs_json"])
        return {legs[0]["venue"], legs[1]["venue"]}
    # Cross-venue: oracles differ (CF Benchmarks vs UMA) -> REVIEW, never auto-filled.
    cross = [r for r in rows if len(_venues(r)) == 2]
    assert cross and all(r["status"] == "REVIEW" and r["trusted"] == 0 for r in cross)
    assert "same_oracle=False" in cross[0]["notes"]
    # B1: a single-venue Kalshi YES+NO lock (same oracle, no rulebook risk) IS fillable -> OK.
    assert all(r["status"] == "OK" and r["trusted"] == 1 for r in rows if len(_venues(r)) == 1)


def test_sweep_no_flag_when_strikes_differ(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    config = load_config()
    kalshi = {"markets": [{
        "ticker": "KXBTCD-26JUN2217-T73000", "yes_sub_title": "$73,000 or above",
        "close_time": "2026-06-22T21:00:00Z", "yes_ask_dollars": "0.48", "no_ask_dollars": "0.55",
    }]}
    poly = {"markets": [{  # different strike -> different asset key -> never pairs
        "question": "Will Bitcoin reach $90,000?", "groupItemTitle": "↑ 90,000",
        "endDate": "2026-06-22T21:00:00Z", "bestAsk": 0.40, "bestBid": 0.39,
    }]}
    _, found = run_btc_pass(store, config, kalshi_fetch=lambda: kalshi, poly_fetch=lambda: poly)
    assert found == 0


def test_b6_strike_parse_misses_are_counted_not_silent() -> None:
    # B6: a market whose ticker/subtitle carries no parseable strike is counted as a strike-miss
    # (the format-drift signal), separate from markets dropped for missing prices.
    stats = {}
    payload = {"markets": [
        {"ticker": "KXBTCD-26JUN2111-T72000", "yes_sub_title": "$72,000",
         "yes_ask_dollars": "0.40", "no_ask_dollars": "0.55", "close_time": "2026-06-21T11:00:00Z"},
        {"ticker": "KXBTCD-NOSTRIKE-HERE", "yes_sub_title": "bitcoin maybe",  # no $/T strike
         "yes_ask_dollars": "0.40", "no_ask_dollars": "0.55", "close_time": "2026-06-21T11:00:00Z"},
    ]}
    legs = parse_kalshi_btc(payload, stats=stats)
    assert len(legs) == 2                       # only the parseable market produced legs
    assert stats["kalshi_markets"] == 2 and stats["kalshi_strike_miss"] == 1


def test_b5_symbol_prefix_keeps_btc_and_eth_strikes_distinct() -> None:
    # B5: ETH widens the universe; a BTC strike must never match an ETH strike of the same number,
    # even when strike, window and cross-venue pairing would otherwise line up.
    from arb.btc_arb import find_btc_arb
    btc = parse_kalshi_btc({"markets": [{"ticker": "KXBTCD-26JUN2111-T72000", "yes_sub_title": "$72,000",
          "yes_ask_dollars": "0.40", "no_ask_dollars": "0.55", "close_time": "2026-06-21T11:00:00Z",
          "_symbol": "BTC"}]})
    eth = parse_polymarket_btc({"markets": [{"question": "Will ETH reach $72,000?",
          "endDate": "2026-06-21T11:00:00Z", "bestAsk": 0.40, "bestBid": 0.55, "_symbol": "ETH"}]})
    assert {l.asset for l in btc} == {"BTC@72000"} and {l.asset for l in eth} == {"ETH@72000"}
    # BTC-kalshi-UP (0.40) + ETH-poly-DOWN (1-0.55) is cross-venue and sums < 1, but the different
    # underlying → different asset → never paired.
    assert find_btc_arb(btc + eth, window_match_secs=5, trusted=False) == []
