"""06 — Phase B: startup warning, observed-log override, reconcile retro-check.

The section_green gate itself is covered by tests/test_settlement_matrix.py + test_trusted_gate.py;
these lock the prompt-06 additions.
"""
from decimal import Decimal

import arb.settlement_matrix as sm


def _base_matrix():
    return {"tennis": {"retirement": {"kalshi": "SETTLES", "polymarket": "SETTLES"}}}


def test_warn_ungreen_trusted_pairs_flags_non_green_and_is_silent_when_empty(capsys) -> None:
    sm.use_matrix({"baseball_mlb": {"suspended": {"kalshi": "UNKNOWN", "polymarket": "SETTLES"}}})
    try:
        # empty → no warnings
        assert sm.warn_ungreen_trusted_pairs(set()) == []
        # a whitelist entry on a non-green section → one warning
        warns = sm.warn_ungreen_trusted_pairs({"kalshi+polymarket:baseball_mlb"})
        assert len(warns) == 1 and "NON-GREEN" in warns[0]
        # a green section → no warning
        sm.use_matrix({"baseball_mlb": {"suspended": {"kalshi": "SETTLES", "polymarket": "SETTLES"}}})
        assert sm.warn_ungreen_trusted_pairs({"kalshi+polymarket:baseball_mlb"}) == []
    finally:
        sm.use_matrix(None)


def test_observed_log_overrides_base_and_reverts_section(tmp_path) -> None:
    observed = tmp_path / "observed.yaml"
    # base green section
    base = _base_matrix()
    assert sm._merge_observed(base, {}) == base
    assert not sm.section_issues("tennis", ["kalshi", "polymarket"], base)

    # record a lived mismatch → merged matrix now has a blocking scenario
    sm.record_observed_settlement("tennis", "observed_mismatch_bet_9", "kalshi",
                                  "CONDITIONAL: observed cross-venue mismatch", path=observed)
    sm.record_observed_settlement("tennis", "observed_mismatch_bet_9", "polymarket",
                                  "CONDITIONAL: observed cross-venue mismatch", path=observed)
    import yaml
    merged = sm._merge_observed(base, yaml.safe_load(observed.read_text()))
    issues = sm.section_issues("tennis", ["kalshi", "polymarket"], merged)
    assert "observed_mismatch_bet_9" in issues  # CONDITIONAL → blocks → pair reverts to REVIEW


def test_reconcile_records_observed_mismatch(tmp_path, monkeypatch) -> None:
    # A settled cross-venue ARB whose two venues resolve inconsistently (both legs LOST → no leg
    # won) must record an observed mismatch so the sport reverts.
    from arb.store import Store
    from arb.reconcile import reconcile_settlements

    observed = tmp_path / "observed.yaml"
    monkeypatch.setattr(sm, "OBSERVED_PATH", observed)
    store = Store(tmp_path / "arb.sqlite")
    legs = [{"venue": "kalshi", "outcome": "A", "cost": "0.50", "fee_cost": "0"},
            {"venue": "polymarket", "outcome": "B", "cost": "0.48", "fee_cost": "0"}]
    import json
    with store.connect() as con:
        con.execute(
            """INSERT INTO flat_bets(fingerprint, opened_poll_id, category, sport, kind, trusted,
                   live, edge, stake_usd, legs_json, optimistic_cost, pnl_optimistic, status,
                   placed_at, settled_at, result, notes, ai_matched, settlement_mismatch)
               VALUES ('fp', 1, 'sports', 'tennis', 'ARB', 0, 0, '0.02', '1.00', ?, '0.98', '0.02',
                       'SETTLED', '2026-06-09T10:00:00+00:00', '2026-06-09T14:00:00+00:00',
                       'RESOLVED', '', 0, 0)""",
            (json.dumps(legs),))
    # both legs resolve LOST → reconcile_arb sees "no leg won" → MISMATCH
    resolvers = {"kalshi": lambda leg: "LOST", "polymarket": lambda leg: "LOST"}
    counts = reconcile_settlements(store, resolvers)
    assert counts["mismatch"] == 1
    assert counts["observed_recorded"] == 1
    assert observed.exists()
    import yaml
    data = yaml.safe_load(observed.read_text())
    assert "tennis" in data  # the sport is now blocked by a lived observation
