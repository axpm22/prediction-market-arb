"""B3 — BTC oracle-divergence tracker (MicroEVBitcoin.md): the REVIEW -> whitelist evidence path."""
from datetime import datetime, timezone

from arb.experimental.btc_oracle import (
    analyze_oracle_divergence,
    oracle_divergence,
    record_btc_print,
)
from arb.store import Store

W = datetime(2026, 6, 21, 11, 0, tzinfo=timezone.utc)


def _agree(n):
    # Both venues call the same binary result, prints within a tick.
    return [{"kalshi_result": 1, "poly_result": 1, "kalshi_print": "72010.5", "poly_print": "72010.4"}
            for _ in range(n)]


def test_zero_disagreement_over_enough_windows_is_whitelist_eligible():
    d = analyze_oracle_divergence(_agree(30), min_windows=30)
    assert d.decided == 30 and d.disagreements == 0
    assert d.whitelist_eligible is True
    assert d.max_price_gap <= 0.2


def test_too_few_windows_is_not_eligible_even_with_zero_disagreement():
    assert analyze_oracle_divergence(_agree(5), min_windows=30).whitelist_eligible is False


def test_any_disagreement_blocks_eligibility():
    records = _agree(40) + [{"kalshi_result": 1, "poly_result": 0,
                             "kalshi_print": "72000", "poly_print": "71990"}]
    d = analyze_oracle_divergence(records, min_windows=30)
    assert d.disagreements == 1 and d.disagree_rate == round(1 / 41, 4)
    assert d.whitelist_eligible is False


def test_undecided_windows_do_not_count_as_decided():
    # A window with only one venue's result (the other not yet settled) is not "decided".
    d = analyze_oracle_divergence([{"kalshi_result": 1, "poly_result": None,
                                    "kalshi_print": "72000", "poly_print": None}], min_windows=1)
    assert d.n == 1 and d.decided == 0 and d.whitelist_eligible is False


def test_record_and_query_roundtrip_is_append_only(tmp_path):
    store = Store(tmp_path / "arb.sqlite")
    for _ in range(3):
        record_btc_print(store, "BTC@72000@202606211100", W,
                         kalshi_print="72010", poly_print="72009",
                         kalshi_result=1, poly_result=1)
    d = oracle_divergence(store, min_windows=3)
    assert d.n == 3 and d.decided == 3 and d.disagreements == 0
    assert d.whitelist_eligible is True
