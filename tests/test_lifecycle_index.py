"""Phase 6 / audit S1: the indexed active-set read must be byte-for-byte identical to the
old full-scan Python derivation (active_rows_by_fingerprint), just without scanning every row.
"""
import json
from datetime import datetime, timezone

from arb.lifecycle import active_rows_by_fingerprint, active_rows_via_index
from arb.store import Store


def _row(con, fingerprint, status, kind="EV", **extra):
    con.execute(
        """INSERT INTO flat_bets(fingerprint, opened_poll_id, category, sport, kind, trusted, live,
               edge, stake_usd, legs_json, status, placed_at, notes)
           VALUES (?, 1, 'sports', 'baseball_mlb', ?, 0, 0, '0.02', '1.00', '[]', ?, ?, '')""",
        (fingerprint, kind, status, datetime.now(timezone.utc).isoformat()),
    )


def _seed_varied(store: Store) -> None:
    with store.connect() as con:
        _row(con, "fp_open", "OPEN")                       # active
        _row(con, "fp_conf", "OPEN"); _row(con, "fp_conf", "CONFIRMED")   # active (latest CONFIRMED)
        _row(con, "fp_settled", "OPEN"); _row(con, "fp_settled", "SETTLED")  # terminal
        _row(con, "fp_closed", "OPEN"); _row(con, "fp_closed", "CLOSED")     # terminal
        _row(con, "fp_exited", "OPEN"); _row(con, "fp_exited", "EXITED")     # terminal
        # Reappearance: OPEN -> CLOSED -> OPEN again => latest is OPEN => active.
        _row(con, "fp_reappear", "OPEN"); _row(con, "fp_reappear", "CLOSED"); _row(con, "fp_reappear", "OPEN")
        # Annotation rows (not lifecycle transitions) must not change the verdict.
        _row(con, "fp_open", "VERIFIED")
        _row(con, "fp_settled", "LIFETIME")
        # A CLOSE-kind terminal row sharing an ARB fingerprint.
        _row(con, "fp_arb", "OPEN", kind="ARB"); _row(con, "fp_arb", "CLOSED", kind="CLOSE")


def test_index_active_set_matches_python_derivation(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    _seed_varied(store)
    with store.connect() as con:
        all_rows = list(con.execute("SELECT * FROM flat_bets ORDER BY id"))
        python_active = active_rows_by_fingerprint(all_rows)
        index_active = active_rows_via_index(con)

    # Same fingerprints, and the same chosen row id for each.
    assert set(index_active) == set(python_active)
    assert {fp: r["id"] for fp, r in index_active.items()} == {fp: r["id"] for fp, r in python_active.items()}
    # The expected active set, spelled out (fp_arb's latest row is CLOSED -> terminal).
    assert set(index_active) == {"fp_open", "fp_conf", "fp_reappear"}


def test_index_kind_filter_matches_python_derivation(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    _seed_varied(store)
    with store.connect() as con:
        kinds = ("EV", "ARB", "CLOSE")
        filtered = [r for r in con.execute("SELECT * FROM flat_bets ORDER BY id") if r["kind"] in kinds]
        python_active = active_rows_by_fingerprint(filtered)
        index_active = active_rows_via_index(con, kinds=kinds)
    assert {fp: r["id"] for fp, r in index_active.items()} == {fp: r["id"] for fp, r in python_active.items()}
