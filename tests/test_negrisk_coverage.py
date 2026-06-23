from datetime import datetime, timedelta, timezone

from arb.experimental.negrisk_sweep import fetch_negrisk_events, lifetime_distribution
from arb.store import Store


def test_fetch_paginates_and_dedups(monkeypatch) -> None:
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(params["offset"])

        class R:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                # same event id on every page -> must be deduped to ONE.
                return [{"id": "E1", "negRisk": True, "markets": [{}, {}]},
                        {"id": f"E{params['offset']}", "negRisk": True, "markets": [{}, {}]},
                        {"id": "skip", "negRisk": False, "markets": [{}, {}]}]
        return R()

    import arb.experimental.negrisk_sweep as mod
    monkeypatch.setattr(mod.requests, "get", fake_get)
    events = fetch_negrisk_events(limit=90, pages=3)
    assert calls == [0, 30, 60]                       # paginated
    ids = sorted(e["id"] for e in events)
    assert ids == ["E0", "E1", "E30", "E60"]          # deduped E1, dropped non-negRisk


def _negrisk_open(con, fp, placed):
    cur = con.execute(
        """INSERT INTO flat_bets(fingerprint, opened_poll_id, category, sport, kind, trusted, live,
               edge, stake_usd, legs_json, status, placed_at, notes)
           VALUES (?, 1, 'nonsports', 'negrisk', 'NEGRISK', 1, 0, '0.01', '1.00', '[]', 'OPEN', ?, '')""",
        (fp, placed))
    return int(cur.lastrowid)


def test_lifetime_distribution(tmp_path) -> None:
    store = Store(tmp_path / "arb.sqlite")
    t0 = datetime(2026, 6, 20, tzinfo=timezone.utc)
    with store.connect() as con:
        a = _negrisk_open(con, "negrisk:E1:convert", t0.isoformat())
        b = _negrisk_open(con, "negrisk:E2:convert", t0.isoformat())
        _negrisk_open(con, "negrisk:E3:convert", t0.isoformat())   # never closes
        # close a after 30s, b after 300s
        con.execute("""INSERT INTO flat_bets(fingerprint, reappearance_of, opened_poll_id, category,
                       sport, kind, trusted, live, edge, stake_usd, legs_json, status, placed_at, notes)
                       VALUES ('negrisk:E1:convert', ?, 1, 'nonsports', 'negrisk', 'NEGRISK', 1, 0,
                       '0', '1.00', '[]', 'CLOSED', ?, '')""", (a, (t0 + timedelta(seconds=30)).isoformat()))
        con.execute("""INSERT INTO flat_bets(fingerprint, reappearance_of, opened_poll_id, category,
                       sport, kind, trusted, live, edge, stake_usd, legs_json, status, placed_at, notes)
                       VALUES ('negrisk:E2:convert', ?, 1, 'nonsports', 'negrisk', 'NEGRISK', 1, 0,
                       '0', '1.00', '[]', 'CLOSED', ?, '')""", (b, (t0 + timedelta(seconds=300)).isoformat()))
    d = lifetime_distribution(store)
    assert d["opened"] == 3 and d["closed"] == 2
    assert d["max_secs"] == 300.0
