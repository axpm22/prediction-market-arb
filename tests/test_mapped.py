"""Mapped-pair lane: nonsports_map.yaml -> both venues' quotes under one
event_key, inversion normalized, locked cross-venue covers booked
XSPREAD/review. Fakes only (rule 2 ethos)."""
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from arb.config import load
from arb.nonsports import mapped
from arb.store import Store

ROOT = Path(__file__).resolve().parent.parent
CFG = load(ROOT / "config.yaml")
NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)

ENTRY_INV = {"kalshi": "KXU3-26JUL-T3.9", "polymarket": "0xabc",
             "event_key": "nonsports:map:kxu3-26jul-t3.9",
             "inverted": True, "concept": "unemployment"}


def _fake_kalshi(monkeypatch, yes_ask="0.44", yes_bid="0.41",
                 no_ask="0.59", no_bid="0.56"):
    def fake_get(url, cfg, deadline=None):
        assert "/markets/KXU3-26JUL-T3.9" in url
        return {"market": {"status": "open",
                           "yes_ask_dollars": yes_ask, "yes_bid_dollars": yes_bid,
                           "no_ask_dollars": no_ask, "no_bid_dollars": no_bid,
                           "yes_ask_size_fp": "100", "yes_bid_size_fp": "100"}}
    monkeypatch.setattr(mapped, "get_json", fake_get)


def _fake_pm(monkeypatch, yes_ask="0.52", no_ask="0.50"):
    def fake_get(url, cfg, deadline=None):
        return [{"closed": False, "clobTokenIds": json.dumps(["tY", "tN"])}]

    def fake_post(url, cfg, payload, deadline=None):
        return [{"asset_id": "tY",
                 "asks": [{"price": yes_ask, "size": "80"}],
                 "bids": [{"price": "0.49", "size": "80"}]},
                {"asset_id": "tN",
                 "asks": [{"price": no_ask, "size": "80"}],
                 "bids": [{"price": "0.47", "size": "80"}]}]
    return fake_get, fake_post


def test_inverted_pair_normalizes_sides_and_keeps_venue_side(monkeypatch):
    fake_get, fake_post = _fake_pm(monkeypatch)
    monkeypatch.setattr(mapped, "get_json", fake_get)
    monkeypatch.setattr(mapped, "post_json", fake_post)
    qs = mapped.pm_pair_quotes(CFG, ENTRY_INV, NOW)
    assert {(q.side, q.venue_side) for q in qs} == {("NO", "YES"), ("YES", "NO")}
    assert all(q.event_key == ENTRY_INV["event_key"] for q in qs)


def test_locked_cross_venue_cover_books_xspread_review(monkeypatch, tmp_path):
    # K YES 0.44 (+quadratic fee) with PM(normalized) NO 0.52: sum < 1 -> locked
    store = Store(tmp_path / "m.sqlite")

    def router_get(url, cfg, deadline=None):
        if "/markets/KXU3" in url:
            return {"market": {"status": "open",
                               "yes_ask_dollars": "0.44", "yes_bid_dollars": "0.41",
                               "no_ask_dollars": "0.59", "no_bid_dollars": "0.56",
                               "yes_ask_size_fp": "100", "yes_bid_size_fp": "100"}}
        return [{"closed": False, "clobTokenIds": json.dumps(["tY", "tN"])}]

    _, fake_post = _fake_pm(monkeypatch)
    monkeypatch.setattr(mapped, "get_json", router_get)
    monkeypatch.setattr(mapped, "post_json", fake_post)
    monkeypatch.setattr(mapped, "load_map", lambda path=None: [ENTRY_INV])
    s = mapped.run_mapped(CFG, store, now=NOW)
    assert s["pairs"] == 1 and s["quotes"] == 4 and s["errors"] == 0
    # quotes persisted under the shared key (rule 7)
    n = store.query("SELECT COUNT(*) c FROM quotes WHERE event_key=?",
                    (ENTRY_INV["event_key"],))[0]["c"]
    assert n == 4
    bets = [dict(r) for r in store.query("SELECT * FROM flat_bets")]
    assert bets, "locked cover must book"
    assert all(b["kind"] == "XSPREAD" and b["lane"] == "review" for b in bets)
    assert all(b["venue_pair"] == "kalshi:polymarket" for b in bets)


def test_no_lock_no_bet_but_quotes_still_persist(monkeypatch, tmp_path):
    store = Store(tmp_path / "n.sqlite")

    def router_get(url, cfg, deadline=None):
        if "/markets/KXU3" in url:
            return {"market": {"status": "open",
                               "yes_ask_dollars": "0.55", "yes_bid_dollars": "0.52",
                               "no_ask_dollars": "0.48", "no_bid_dollars": "0.45",
                               "yes_ask_size_fp": "100", "yes_bid_size_fp": "100"}}
        return [{"closed": False, "clobTokenIds": json.dumps(["tY", "tN"])}]

    _, fake_post = _fake_pm(monkeypatch, yes_ask="0.56", no_ask="0.54")
    monkeypatch.setattr(mapped, "get_json", router_get)
    monkeypatch.setattr(mapped, "post_json", fake_post)
    monkeypatch.setattr(mapped, "load_map", lambda path=None: [ENTRY_INV])
    s = mapped.run_mapped(CFG, store, now=NOW)
    assert s["quotes"] == 4
    assert not store.query("SELECT 1 FROM flat_bets")


def test_wide_spread_book_never_emits_divergence(monkeypatch, tmp_path):
    # a 20-point "divergence" against a near-empty book is fiction — suppress
    # when either side's spread crosses zombie_spread_min
    fake_get, _ = _fake_pm(monkeypatch)
    monkeypatch.setattr(mapped, "get_json", fake_get)

    def wide_post(url, cfg, payload, deadline=None):
        return [{"asset_id": "tY",
                 "asks": [{"price": "0.97", "size": "5"}],
                 "bids": [{"price": "0.02", "size": "5"}]},   # spread 0.95
                {"asset_id": "tN",
                 "asks": [{"price": "0.98", "size": "5"}],
                 "bids": [{"price": "0.03", "size": "5"}]}]
    monkeypatch.setattr(mapped, "post_json", wide_post)
    qs = mapped.pm_pair_quotes(CFG, ENTRY_INV, NOW)
    k = [mapped._quote(CFG, ENTRY_INV, "kalshi", s, s, Decimal("0.50"),
                       Decimal("0.48"), Decimal(100), "econ", NOW, "K1")
         for s in ("YES", "NO")]
    opps = mapped.detect_pair(CFG, ENTRY_INV, k + qs)
    assert not [o for o in opps if o.kind == "DIVERGENCE"]


_ENTRIES3 = [{"kalshi": f"KX{i}", "polymarket": f"0xc{i}",
              "event_key": f"nonsports:map:kx{i}", "inverted": False,
              "concept": "unemployment"} for i in range(3)]


def _router3(fail_gamma_for=()):
    def router_get(url, cfg, deadline=None):
        if "/markets/KX" in url:
            return {"market": {"status": "open",
                               "yes_ask_dollars": "0.44", "yes_bid_dollars": "0.41",
                               "no_ask_dollars": "0.59", "no_bid_dollars": "0.56",
                               "yes_ask_size_fp": "100", "yes_bid_size_fp": "100"}}
        cid = url.split("condition_ids=")[1]
        if cid in fail_gamma_for:
            raise RuntimeError("gamma down for this pair")
        return [{"closed": False,
                 "clobTokenIds": json.dumps([f"tY{cid[-1]}", f"tN{cid[-1]}"])}]
    return router_get


def _books_post(posts):
    def recording_post(url, cfg, payload, deadline=None):
        posts.append(payload)
        return [{"asset_id": p["token_id"],
                 "asks": [{"price": "0.52" if p["token_id"].startswith("tY")
                           else "0.50", "size": "80"}],
                 "bids": [{"price": "0.49" if p["token_id"].startswith("tY")
                           else "0.47", "size": "80"}]}
                for p in payload]
    return recording_post


def test_run_mapped_issues_one_books_post_with_all_tokens(monkeypatch, tmp_path):
    # 9 pairs => 18 sequential requests per poll; POST /books is the verified
    # batch endpoint (§6) and must be called ONCE with the token union
    store = Store(tmp_path / "b1.sqlite")
    posts = []
    monkeypatch.setattr(mapped, "get_json", _router3())
    monkeypatch.setattr(mapped, "post_json", _books_post(posts))
    monkeypatch.setattr(mapped, "load_map", lambda path=None: list(_ENTRIES3))
    s = mapped.run_mapped(CFG, store, now=NOW)
    assert len(posts) == 1
    assert {p["token_id"] for p in posts[0]} == \
        {f"t{sd}{i}" for sd in "YN" for i in "012"}
    assert s["pairs"] == 3 and s["quotes"] == 12 and s["errors"] == 0
    # golden: per-pair quotes identical to the per-pair implementation
    for i in range(3):
        rows = store.query(
            "SELECT side, cost, bid FROM quotes WHERE event_key=? AND venue=?",
            (f"nonsports:map:kx{i}", "polymarket"))
        assert {(r["side"], r["cost"], r["bid"]) for r in rows} == \
            {("YES", "0.520000", "0.490000"), ("NO", "0.500000", "0.470000")}


def test_gamma_failure_poisons_only_its_pair(monkeypatch, tmp_path):
    store = Store(tmp_path / "b2.sqlite")
    posts = []
    monkeypatch.setattr(mapped, "get_json", _router3(fail_gamma_for={"0xc1"}))
    monkeypatch.setattr(mapped, "post_json", _books_post(posts))
    monkeypatch.setattr(mapped, "load_map", lambda path=None: list(_ENTRIES3))
    s = mapped.run_mapped(CFG, store, now=NOW)
    assert s["errors"] == 1 and s["quotes"] == 8  # pair 1 poisoned, others whole
    assert len(posts) == 1 and len(posts[0]) == 4


def test_books_failure_errors_all_pairs(monkeypatch, tmp_path):
    # the books fetch is ONE request now: its failure is every pair's failure
    store = Store(tmp_path / "b3.sqlite")

    def failing_post(url, cfg, payload, deadline=None):
        raise RuntimeError("clob down")
    monkeypatch.setattr(mapped, "get_json", _router3())
    monkeypatch.setattr(mapped, "post_json", failing_post)
    monkeypatch.setattr(mapped, "load_map", lambda path=None: list(_ENTRIES3))
    s = mapped.run_mapped(CFG, store, now=NOW)
    assert s["errors"] == 3 and s["quotes"] == 0


def test_supervisor_stagger_offsets_lane_starts():
    """Lockstep fan-out is the bug this exists to prevent. The FLAT ladder it
    used to assert (i * stagger_secs) was itself an aliasing bug: 5 lanes x 15s
    = 60s = watch_interval_secs put lane 5 in exact lockstep with lane 1, and
    Task 4 made it 6 lanes. Offsets now spread inside each lane's OWN period,
    which is what actually guarantees separation."""
    from arb.heartbeat import initial_next_run, lane_interval
    nr = initial_next_run(CFG, 1000.0)
    enabled = [k for k, v in CFG["supervisor"]["processes"].items() if v]
    assert set(nr) == set(enabled)
    offsets = {k: nr[k] - 1000.0 for k in nr}
    # one lane starts immediately; none is delayed past its own cadence
    assert min(offsets.values()) == 0.0
    for name, off in offsets.items():
        assert 0 <= off < lane_interval(CFG, name), name
    # and no two lanes share a start instant
    assert len(set(offsets.values())) == len(enabled), offsets


def test_tickets_rank_by_persistence_and_close_on_lifetime_end(monkeypatch, tmp_path):
    from datetime import timedelta

    from arb.tickets import open_locked_positions
    store = Store(tmp_path / "t.sqlite")

    def router_get(url, cfg, deadline=None):
        if "/markets/KXU3" in url:
            return {"market": {"status": "open",
                               "yes_ask_dollars": "0.44", "yes_bid_dollars": "0.41",
                               "no_ask_dollars": "0.59", "no_bid_dollars": "0.56",
                               "yes_ask_size_fp": "100", "yes_bid_size_fp": "100"}}
        return [{"closed": False, "clobTokenIds": json.dumps(["tY", "tN"])}]

    _, fake_post = _fake_pm(monkeypatch)
    monkeypatch.setattr(mapped, "get_json", router_get)
    monkeypatch.setattr(mapped, "post_json", fake_post)
    monkeypatch.setattr(mapped, "load_map", lambda path=None: [ENTRY_INV])
    mapped.run_mapped(CFG, store, now=NOW)
    mapped.run_mapped(CFG, store, now=NOW + timedelta(minutes=5))  # re-sighted
    ts = open_locked_positions(CFG, store)
    assert len(ts) == 1
    t = ts[0]
    assert t["sightings"] == 2          # booking + one absorb
    assert t["kind"] == "XSPREAD" and t["trusted"] is False
    venues = {lg["venue"] for lg in t["legs"]}
    assert venues == {"kalshi", "polymarket"}
    assert all(lg.get("venue_market_id") for lg in t["legs"])
    # lifetime end removes the ticket
    bid = store.query("SELECT id FROM flat_bets ORDER BY id DESC LIMIT 1")[0]["id"]
    store.insert("bet_events", {"bet_id": bid, "kind": "lifetime_closed",
                                "payload_json": "{}"})
    assert open_locked_positions(CFG, store) == []


def test_settlement_mismatch_permanently_bans_a_mapped_pair(monkeypatch, tmp_path):
    # rule 11: "a settlement mismatch ... permanently bans the pair" — the
    # lane must stop touching a pair whose bet ever mismatch-annotated
    store = Store(tmp_path / "b.sqlite")
    bid = store.insert("flat_bets", {"kind": "XSPREAD",
                                     "event_key": ENTRY_INV["event_key"],
                                     "legs_json": "[]", "stake_usd": "1",
                                     "status": "OPEN"})
    store.insert("annotations", {"target_kind": "bet", "target_id": str(bid),
                                 "kind": "settlement_mismatch", "detail": "x",
                                 "created_at": NOW.isoformat()})
    calls = {"n": 0}

    def counting_get(url, cfg, deadline=None):
        calls["n"] += 1
        return {}
    monkeypatch.setattr(mapped, "get_json", counting_get)
    monkeypatch.setattr(mapped, "post_json", counting_get)
    monkeypatch.setattr(mapped, "load_map", lambda path=None: [ENTRY_INV])
    s = mapped.run_mapped(CFG, store, now=NOW)
    assert s["banned"] == 1 and calls["n"] == 0  # never fetched again


def test_mapped_runs_the_tracker_exactly_once_per_poll(tmp_path, monkeypatch):
    """A confirm is a SECOND sighting on a LATER poll. mapped called process_poll
    twice on one poll_id, so the bet booked by the first pass was seen as
    active-and-sighted by the second and got a confirmed event with zero elapsed
    time — confirmed_at PRECEDING created_at (bets 46, 52, 73, 94 in production).

    Counting the calls reproduces the defect deterministically without having to
    construct an Opportunity that actually books."""
    import arb.tracker
    from arb.nonsports import mapped as M
    store = Store(tmp_path / "t.sqlite")

    calls = []
    real = arb.tracker.process_poll

    def counting(cfg_, store_, poll_id, poll_kind, opps, venue_status, now=None):
        calls.append((poll_id, dict(venue_status)))
        return real(cfg_, store_, poll_id, poll_kind, opps, venue_status, now=now)

    monkeypatch.setattr(arb.tracker, "process_poll", counting)

    entry = {"event_key": "nonsports:test:pair", "kalshi": "KXTEST",
             "polymarket": "0xtest"}
    monkeypatch.setattr(M, "load_map", lambda: [entry])
    monkeypatch.setattr(M, "kalshi_pair_quotes", lambda cfg_, e, now: [])
    monkeypatch.setattr(M, "pm_pair_tokens", lambda cfg_, e: [])
    monkeypatch.setattr(M, "detect_pair", lambda cfg_, e, qs: [])

    M.run_mapped(CFG, store, now=NOW)

    assert len(calls) == 1, f"process_poll ran {len(calls)}x on one poll"
    # the surviving call must carry BOTH venues: the deleted second call passed
    # kalshi-only, so PM-legged mapped bets were carried by the venue-gap rule
    # instead of ever closing
    assert set(calls[0][1]) == {"kalshi", "polymarket"}
    assert len(store.query("SELECT id FROM polls")) == 1


def test_mapped_ledger_has_no_same_poll_confirms(tmp_path, monkeypatch):
    """The observable consequence, asserted directly against the ledger."""
    from arb.nonsports import mapped as M
    store = Store(tmp_path / "t.sqlite")
    monkeypatch.setattr(M, "load_map", lambda: [])
    M.run_mapped(CFG, store, now=NOW)
    assert store.query(
        """SELECT be.id FROM bet_events be JOIN flat_bets fb ON fb.id = be.bet_id
           WHERE be.kind='confirmed'
             AND json_extract(be.payload_json, '$.poll_id') = fb.poll_id""") == []


def test_mapped_lane_demotes_an_edge_inside_the_spread(monkeypatch):
    """This lane builds its own Opportunity instead of routing through
    arbitrage._locked_opp, so it does NOT inherit that function's guards. Real
    shape observed 2026-07-30: KXCPICORE was flagged 234 times as status OK with
    a ~1.5c edge inside an 11c Kalshi book, because the status line consulted
    only arb_stale_warn."""
    # kalshi YES 0.95 / bid 0.84 -> an 11c spread, exactly the observed book
    _fake_kalshi(monkeypatch, yes_ask="0.95", yes_bid="0.84",
                 no_ask="0.16", no_bid="0.05")
    quotes = mapped.kalshi_pair_quotes(CFG, ENTRY_INV, NOW)
    # polymarket NO cheap enough that kalshi.YES + pm.NO sums under 1
    quotes += [mapped._quote(CFG, ENTRY_INV, "polymarket", "NO", "YES",
                             Decimal("0.03"), Decimal("0.02"), Decimal("80"),
                             "econ", NOW, "0xabc:tY")]
    opps = mapped.detect_pair(CFG, ENTRY_INV, quotes)
    locked = [o for o in opps if o.kind == "XSPREAD" and "mapped-pair" in o.notes
              and "mids" not in o.notes]
    assert locked, "the cover should still be emitted (rule 7)"
    assert locked[0].status == "REVIEW"
    assert "spread>edge" in locked[0].notes
