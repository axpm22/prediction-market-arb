"""L14 non-sports roots (amendment 2026-09-04): the Gamma-by-tag universe.

No network: every Gamma answer here is a fixture built in this file, shaped
from the live probes dated in data/verified_facts.md (2026-09-04) — an event
carries `tags`, `negRisk`, `startDate`, `endDate` and nested `markets[]` whose
own `closed`, `conditionId`, `slug`, `outcomes`, `clobTokenIds`, `endDate`,
`startDate`, `createdAt` and `volume24hr` are what the builder filters on.
"""
import copy
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from arb import whale_nonsports, whale_ns_lane, whale_tape
from arb.whale_universe import Market
from arb.config import load

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def cfg():
    return copy.deepcopy(load(ROOT / "config.yaml"))


@pytest.fixture
def con(tmp_path):
    return whale_tape.open_tape(tmp_path / "tape.sqlite")


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def _market(cid, *, closed=False, volume="25000", ends_in_days=3.0,
            lifetime_hours=48.0, slug="m", outcomes='["Yes", "No"]',
            tokens='["t0", "t1"]'):
    end = NOW + timedelta(days=ends_in_days)
    return {"conditionId": cid, "slug": slug, "closed": closed,
            "volume24hr": volume, "outcomes": outcomes, "clobTokenIds": tokens,
            "endDate": _iso(end),
            "startDate": _iso(end - timedelta(hours=lifetime_hours)),
            "createdAt": _iso(end - timedelta(hours=lifetime_hours + 1))}


def _event(slug="e", tags=("crypto",), neg_risk=False, markets=(),
           start=NOW - timedelta(days=30), end=NOW + timedelta(days=365)):
    return {"slug": slug, "title": slug, "negRisk": neg_risk,
            "tags": [{"slug": t, "label": t} for t in tags],
            "startDate": _iso(start), "endDate": _iso(end),
            "markets": list(markets)}


# ------------------------------------------------------------------ filters

def test_the_builder_keeps_exactly_the_open_liquid_long_lived_market(cfg):
    """The four filters, one event: open, `volume24hr` over the floor, a
    lifetime over `min_lifetime_hours`, and resolving inside
    `nonsports_max_days`."""
    ev = _event(tags=("crypto",), markets=[
        _market("0xkeep", slug="keep"),
        _market("0xclosed", slug="closed", closed=True),
        _market("0xthin", slug="thin", volume="999"),
        _market("0xnovol", slug="novol", volume=None),
        _market("0xshort", slug="short", lifetime_hours=0.25),
        _market("0xfar", slug="far", ends_in_days=120),
        _market("0xpast", slug="past", ends_in_days=-1),
    ])
    got = whale_nonsports.markets_of(cfg, ev, NOW)
    assert [m.condition_id for m in got] == ["0xkeep"]
    m = got[0]
    assert m.sport == "crypto" and m.market_type == "BINARY"
    assert m.start_utc == (NOW + timedelta(days=3)).isoformat()   # == Gamma endDate
    assert m.event_key == "pm:e"
    assert m.tokens == (("t0", "Yes"), ("t1", "No"))
    assert m.volume_24h == "25000"


def test_the_root_comes_from_the_events_own_tags_in_configured_priority(cfg):
    """An event is multi-tagged (measured 2026-09-04: crypto AND politics AND
    trump-presidency), so the root must not depend on which tag's page
    surfaced it."""
    both = _event(tags=("trump-presidency", "crypto", "politics"),
                  markets=[_market("0xa")])
    assert whale_nonsports.markets_of(cfg, both, NOW)[0].sport == "politics"
    geo = _event(tags=("geopolitics", "crypto"), markets=[_market("0xb")])
    assert whale_nonsports.markets_of(cfg, geo, NOW)[0].sport == "geopolitics"
    unmapped = _event(tags=("weather", "science"), markets=[_market("0xc")])
    assert whale_nonsports.markets_of(cfg, unmapped, NOW) == []


def test_a_negrisk_event_is_a_ladder(cfg):
    ev = _event(neg_risk=True, markets=[_market("0xl")])
    assert whale_nonsports.markets_of(cfg, ev, NOW)[0].market_type == "LADDER"


def test_a_market_whose_tokens_cannot_be_told_apart_is_skipped(cfg):
    ev = _event(markets=[_market("0xdup", outcomes='["Yes", "Yes"]'),
                         _market("0x3way", outcomes='["A", "B", "C"]',
                                 tokens='["t0", "t1", "t2"]')])
    assert whale_nonsports.markets_of(cfg, ev, NOW) == []


# ------------------------------------------------------------------- budget

def test_the_refresh_is_bounded_to_pages_times_tags_calls(cfg, con):
    calls = []

    def get(url, _cfg, **kw):
        calls.append(url)
        return [_event(slug=f"e{len(calls)}", tags=("politics",),
                       markets=[_market(f"0x{len(calls)}")])] * 100

    cfg["whale"]["nonsports"]["pages"] = 2
    res = whale_nonsports.nonsports_universe(cfg, con, NOW, get=get)
    assert res["status"] == "ok"
    assert len(calls) == 2 * len(cfg["whale"]["nonsports"]["tags"]) == res["calls"]
    assert all("offset=0" in u or "offset=100" in u for u in calls)
    assert all("closed=false" in u and "limit=100" in u for u in calls)
    # a short page ends that tag early
    calls.clear()

    def short(url, _cfg, **kw):
        calls.append(url)
        return [_event(markets=[_market("0xs")])]

    assert whale_nonsports.nonsports_universe(cfg, con, NOW, get=short)["calls"] \
        == len(cfg["whale"]["nonsports"]["tags"])


def test_disabled_makes_no_call_at_all(cfg, con):
    def get(*a, **k):
        raise AssertionError("no Gamma call when whale.nonsports.enabled is false")

    cfg["whale"]["nonsports"]["enabled"] = False
    assert whale_nonsports.nonsports_universe(cfg, con, NOW, get=get)["status"] \
        == "disabled"


# ------------------------------------------------------------- persistence

def _persist(cfg, con, events, now=NOW):
    def get(url, _cfg, **kw):
        return events if "offset=0" in url and "tag_slug=politics" in url else []
    return whale_nonsports.nonsports_universe(cfg, con, now, get=get)


def test_a_kept_market_lands_in_the_tape_and_the_lane_reads_it_back(cfg, con):
    _persist(cfg, con, [_event(tags=("politics",), markets=[_market("0xp")])])
    row = con.execute("SELECT * FROM markets WHERE condition_id='0xp'").fetchone()
    assert (row["sport"], row["market_type"]) == ("politics", "BINARY")
    assert row["volume_24h"] == "25000"
    lane = whale_ns_lane.tape_universe(con, cfg, NOW)
    assert [m.condition_id for m in lane] == ["0xp"]
    assert lane[0].tokens == (("t0", "Yes"), ("t1", "No"))
    assert lane[0].volume_24h == "25000"


def test_the_lane_universe_is_bounded_by_the_nonsports_horizon(cfg, con):
    """Both ends of `[now, now + max_days]`. The market resolves at NOW+3d, so
    the LOWER bound is tested from a `now` past its own deadline — not from a
    `now` before it, which the 90-day window (amendment 2026-09-05) admits."""
    _persist(cfg, con, [_event(tags=("politics",), markets=[_market("0xp")])])
    late = NOW + timedelta(days=int(cfg["whale"]["nonsports"]["max_days"]) + 1)
    assert whale_ns_lane.tape_universe(con, cfg, late) == []
    assert whale_ns_lane.tape_universe(con, cfg, NOW + timedelta(days=5)) == []


def test_the_universe_reads_the_lanes_own_horizon_key(cfg, con):
    """`whale.nonsports.max_days` is the lane's OWN booking horizon, amended
    7 -> 90 on 2026-09-05. A politics market 20 days out — a 2026-midterm
    market, the class the amendment was measured on — is admitted at 90 and
    excluded at 7, in the builder and in the tape read alike."""
    ev = _event(tags=("politics",), markets=[_market("0xfar", ends_in_days=20)])
    assert [m.condition_id for m in whale_nonsports.markets_of(cfg, ev, NOW)] == ["0xfar"]
    _persist(cfg, con, [ev])
    assert [m.condition_id for m in whale_ns_lane.tape_universe(con, cfg, NOW)] \
        == ["0xfar"]
    cfg["whale"]["nonsports"]["max_days"] = 7
    assert whale_nonsports.markets_of(cfg, ev, NOW) == []
    assert whale_ns_lane.tape_universe(con, cfg, NOW) == []


def test_a_moved_end_date_never_rewrites_a_stored_start(cfg, con):
    """Look-ahead guard (proposal §1 Risks): Gamma can edit an endDate, and a
    moving `start_utc` changes which of a whale's positions were pregame."""
    _persist(cfg, con, [_event(tags=("politics",), markets=[_market("0xp")])])
    first = con.execute("SELECT start_utc FROM markets WHERE condition_id='0xp'"
                        ).fetchone()["start_utc"]
    _persist(cfg, con, [_event(tags=("politics",),
                               markets=[_market("0xp", ends_in_days=5)])])
    assert con.execute("SELECT start_utc FROM markets WHERE condition_id='0xp'"
                       ).fetchone()["start_utc"] == first


def test_a_market_already_mapped_to_a_sport_is_never_relabelled(cfg, con):
    """I1 (fix pass): `market_type` is protected by `first_sight_wins` exactly
    as `sport` and `start_utc` are. A tag page that happens to list an
    already-mapped MLB market used to rewrite WINNER -> BINARY, which invents a
    phantom `baseball_mlb:BINARY` bucket in every aggregation keyed on it."""
    con.execute("INSERT INTO markets (condition_id, sport, market_type, first_seen,"
                " last_seen) VALUES ('0xp','baseball_mlb','WINNER',?,?)",
                (NOW.isoformat(), NOW.isoformat()))
    con.commit()
    _persist(cfg, con, [_event(tags=("politics",), markets=[_market("0xp")])])
    row = con.execute("SELECT sport, market_type FROM markets "
                      "WHERE condition_id='0xp'").fetchone()
    assert (row["sport"], row["market_type"]) == ("baseball_mlb", "WINNER")


def test_the_mapper_never_pins_a_start_on_a_market_it_cannot_root(cfg, con):
    """I2 (fix pass): `whale_map._gamma_row` used to write `start_utc` from
    Gamma's `gameStartTime` for ANY market. A non-sports market carrying that
    field would have a wrong `start_utc` pinned before the tag builder ever saw
    it, and `first_sight_wins` then keeps the wrong one forever — the pregame
    gate would read a kickoff instead of the resolution deadline."""
    from arb import whale_map
    row = whale_map._gamma_row(
        "0xp", {"slug": "will-x-happen-by-friday", "gameStartTime": "2026-09-05T00:00:00Z",
                "clobTokenIds": '["t0", "t1"]', "outcomes": '["Yes", "No"]'},
        cfg["whale"]["slug_sport_prefixes"], NOW)
    assert row[1] is None                      # sport: no league prefix
    assert row[4] is None                      # start_utc: never pinned without one
    sports = whale_map._gamma_row(
        "0xs", {"slug": "mlb-tb-nyy-2026-09-05", "gameStartTime": "2026-09-05T00:00:00Z",
                "sportsMarketType": "winner", "clobTokenIds": '["t0", "t1"]',
                "outcomes": '["Rays", "Yankees"]'},
        cfg["whale"]["slug_sport_prefixes"], NOW)
    assert sports[1] == "baseball_mlb" and sports[4] == "2026-09-05T00:00:00+00:00"


def test_every_configured_root_is_a_polymarket_fee_category(cfg):
    """Rule 4 + the proposal's most likely silent bug: a root that is not a fee
    category prices the leg at the default rate."""
    from arb.venues.fees import _PM_CATEGORY_RATE_KEY
    roots = set(cfg["whale"]["nonsports"]["tags"].values())
    assert roots and roots <= set(_PM_CATEGORY_RATE_KEY) - {"sports"}
    assert Decimal(str(cfg["whale"]["nonsports"]["min_lifetime_hours"])) == 6


# ------------------------------------------------- fees, lane and rule 11

PSLUG = "will-x-happen-by-friday"
PCID, PYES, PNO = "0xpol", "0xpyes", "0xpno"


@pytest.fixture(autouse=True)
def _fresh_bucket():
    whale_tape._BUCKET = None
    yield
    whale_tape._BUCKET = None


def _pm_market(sport="politics", mtype="BINARY", volume="25000"):
    from arb.whale_universe import Market
    return Market(condition_id=PCID, sport=sport, market_type=mtype, line=None,
                  start_utc=(NOW + timedelta(days=3)).isoformat(),
                  event_key=f"pm:{PSLUG}", tokens=((PYES, "Yes"), (PNO, "No")),
                  volume_24h=volume)


def _fee(cfg, key, price):
    price = Decimal(str(price))
    return cfg["polymarket"][key] * 1 * price * (1 - price)


def test_a_politics_whale_leg_is_priced_at_the_politics_fee_rate(cfg):
    """The proposal's single most likely silent bug: pricing a politics leg at
    the sports rate (0.03 against 0.04) understates the cost of the very thing
    under test."""
    from arb.lanes import whale_book
    from arb.whale_signal import Signal

    market = _pm_market()
    books = {PYES: {"asks": [{"price": "0.51", "size": "1000"}],
                    "bids": [{"price": "0.49", "size": "1000"}]}}
    rows = whale_book.quote_rows(cfg, market, books, f"pm:{PSLUG}", NOW)
    assert [r["category"] for r in rows] == ["politics"]
    assert rows[0]["fee_cost"] == _fee(cfg, "politics_fee_rate", "0.51")

    sig = Signal(variant="V1", side=0, points_yes=Decimal(3), points_no=Decimal(0),
                 ratio=Decimal(1), whales=[], vwap=Decimal("0.50"),
                 touch_ask=Decimal("0.51"), touch_bid=Decimal("0.49"),
                 depth_usd=Decimal("500"), fired=True, suppressed_reason=None,
                 flagged_edge=Decimal("0.05"), S=Decimal("0.05"))
    cand = whale_book.candidate(cfg, market, sig, "flat", 1, f"pm:{PSLUG}", PYES,
                                "Yes", NOW + timedelta(days=3), NOW)
    assert cand["category"] == "politics"
    assert Decimal(cand["legs"][0]["fee_cost"]) == _fee(cfg, "politics_fee_rate", "0.51")
    # ... and a sports market still prices at the sports rate
    sports = _pm_market(sport="baseball_mlb", mtype="WINNER")
    assert whale_book.candidate(cfg, sports, sig, "flat", 1, "pm:x", PYES, "Yes",
                                None, NOW)["category"] == "sports"


def test_a_crypto_exit_is_priced_at_the_crypto_fee_rate(cfg):
    """The exit sells into the bid and pays the same category's fee — read off
    the bet's own stored `category`, since no Market is in scope there."""
    from arb.lanes import whale_book
    from arb.whale_exit import ExitDecision
    bet = {"category": "crypto", "stake_usd": "1"}
    payload = whale_book.exit_payload(cfg, bet, ExitDecision("flip", {}),
                                      Decimal("0.49"), Decimal(2))
    assert Decimal(payload["exit_fee"]) == _fee(cfg, "crypto_fee_rate", "0.49")


def test_a_nonsports_whale_row_is_review_whale_signal_never_ai_matched(cfg):
    """Rule 11 governs CROSS-VENUE matching. This lane is `polymarket:self`,
    keys on Polymarket's own event slug and never calls the matcher, so a
    non-sports WHALE row must read `whale-signal`, never `ai-matched` and never
    `nonsports-segregated`."""
    from arb.eligibility import classify
    for category in ("politics", "geopolitics", "crypto", "sports"):
        assert classify({"kind": "WHALE", "category": category, "sport": category,
                         "venue_pair": "polymarket:self", "status": "OK",
                         "quality_flag": "none", "ai_matched": False}, cfg) \
            == (False, "whale-signal", "review")
    # the pin that makes the caller side true: process_poll sets ai_matched
    # False for every candidate it classifies
    tracker = (ROOT / "arb" / "tracker.py").read_text()
    assert '"ai_matched": False' in tracker
    # ... and no whale module can reach rule 11's AI matcher (arb/nonsports/)
    for name in ("whale.py", "whale_book.py"):
        src = (ROOT / "arb" / "lanes" / name).read_text()
        for banned in ("arb.nonsports", "from arb import nonsports", "ai_match",
                       "nonsports_map", "verdict"):
            assert banned not in src, f"{name}: {banned}"


# ------------------------------------------------------------- the cycle

def _trade(wallet, idx=0, price="0.50", size="1000", age=60):
    return {"proxyWallet": wallet, "pseudonym": wallet, "conditionId": PCID,
            "asset": (PYES, PNO)[idx], "outcome": ("Yes", "No")[idx],
            "outcomeIndex": idx, "side": "BUY", "price": price, "size": size,
            "timestamp": int((NOW - timedelta(seconds=age)).timestamp()),
            "transactionHash": f"0x{wallet}", "eventSlug": PSLUG,
            "slug": f"{PSLUG}-yes", "title": "Will X happen by Friday?"}


class _Get:
    def __init__(self, rows):
        self.rows, self.urls = list(rows), []

    def __call__(self, url, cfg, **kw):
        self.urls.append(url)
        return list(self.rows) if "/trades" in url else []


def _fetch(cfg, token_ids, deadline=None):
    book = {"asks": [{"price": "0.51", "size": "1000"}],
            "bids": [{"price": "0.49", "size": "1000"}]}
    return {t: dict(book) for t in token_ids}


def test_the_cycle_books_a_politics_consensus_off_the_tape_universe(cfg, con, tmp_path):
    """End to end, one cycle, no watch poll at all: the non-sports half of the
    universe comes from the tape, the leg is priced at the politics rate, and
    the row keys on Polymarket's own event slug."""
    from arb.lanes.whale import run_whale_cycle
    from arb.store import Store
    from arb.whale_universe import persist_markets

    cfg["whale"]["min_score_lb"] = Decimal("-0.10")   # see tests/test_whale_lane.py
    persist_markets(con, [_pm_market()], NOW, first_sight_wins=True)
    store = Store(tmp_path / "arb.sqlite")
    rows = [_trade(f"w{i}", age=60 + 30 * i) for i in range(3)]
    res = run_whale_cycle(cfg, store, con, NOW, get=_Get(rows), fetch=_fetch)

    assert res["markets"] == 1 and res["fired"] >= 1
    bets = [dict(r) for r in store.query("SELECT * FROM flat_bets ORDER BY id")]
    assert bets, res["suppressed"]
    assert {b["category"] for b in bets} == {"politics"}
    assert {b["event_key"] for b in bets} == {f"pm:{PSLUG}"}
    leg = json.loads(bets[0]["legs_json"])[0]
    assert Decimal(leg["fee_cost"]) == _fee(cfg, "politics_fee_rate", "0.51")
    assert {b["lane"] for b in bets} == {"review"}
    # rule 7: the books this cycle fetched are quotes rows under the same key
    quotes = [dict(r) for r in store.query("SELECT * FROM quotes")]
    assert {q["category"] for q in quotes} == {"politics"}


def _v6(cfg, tmp_path, name, volume):
    from arb.lanes.whale import run_whale_cycle
    from arb.store import Store
    from arb.whale_universe import persist_markets
    con = whale_tape.open_tape(tmp_path / f"{name}.sqlite")
    persist_markets(con, [_pm_market(volume=volume)], NOW, first_sight_wins=True)
    run_whale_cycle(cfg, Store(tmp_path / f"{name}-arb.sqlite"), con, NOW,
                    get=_Get([_trade(f"w{i}", age=60 + 30 * i) for i in range(3)]),
                    fetch=_fetch)
    return dict(con.execute("SELECT points_yes, fired FROM signals "
                            "WHERE variant='V6'").fetchone())


def test_the_cycle_prices_v6_off_the_tapes_own_volume_not_the_floor(cfg, tmp_path):
    """A non-sports market has no watch quote, so without `markets.volume_24h`
    V6's denominator falls to `dominance.min_volume_usd` ($5,000) — on a
    $20M/day politics market that would read as maximal dominance and fire.
    The tape's own figure is what stops that."""
    from arb.whale_dominance import point
    cfg["whale"]["min_score_lb"] = Decimal("-0.10")
    known = _v6(cfg, tmp_path, "known", "20000000")
    assert not known["fired"]
    # ... and the denominator is the reason: the same whale over an UNKNOWN
    # volume reads the $5,000 floor instead of the market's $20M and prices
    # out at four orders of magnitude more dominance.
    whale = {"lb": "0.10", "open_notional": "1000"}
    assert point(whale, cfg, {"volume": None}) > point(whale, cfg,
                                                       {"volume": Decimal("20000000")})
    assert point(whale, cfg, {"volume": None}) == \
        point(whale, cfg, {"volume": Decimal("1")})   # both read the floor


def test_a_market_with_no_measured_volume_never_enters_the_lane_universe(cfg, con):
    """The registered universe (design §5 step 1b) is `volume24hr` present and
    >= `min_volume_24h_usd`, a null failing the floor. The by-tag builder has
    always applied it — but since 2026-09-05 the MAPPER also writes non-sports
    `markets` rows (`whale_map._event_roots`, rooting a market from its event's
    tags so a whale's historical politics positions can be scored), and it
    roots WITHOUT a volume floor on purpose: a resolved market's `volume24hr`
    is not a reason to refuse to score the whale who traded it. That makes the
    tape read the one place the lane's own universe is decided, so the floor
    is applied HERE — measured on the live tape 2026-09-05, the very first
    100-row sample of the revisit queue contained a politics market with a
    future endDate and `volume24hr: null`."""
    from arb.whale_universe import persist_markets
    for cid, volume in (("0xnull", None), ("0xthin", "999"), ("0xfat", "1000")):
        persist_markets(con, [Market(condition_id=cid, sport="politics",
                                     market_type="BINARY", line=None,
                                     start_utc=(NOW + timedelta(days=3)).isoformat(),
                                     event_key=None, tokens=(("t0", "Yes"), ("t1", "No")),
                                     volume_24h=volume)], NOW, first_sight_wins=True)
    assert [m.condition_id for m in whale_ns_lane.tape_universe(con, cfg, NOW)] \
        == ["0xfat"]


def test_a_tag_builder_market_keys_its_quotes_on_its_own_event_slug(cfg):
    """M1 (fix pass): §8 wants `pm:<event_slug>` on the quotes AND the bets of
    one market. A tag-builder row has no `slug` (Gamma's by-tag answer never
    fills it) but does carry the finished `pm:<event_slug>` in `event_key`, so
    without it in the chain a market whose prints have aged out of the window
    — every open non-sports bet awaiting a markout — keys its quotes on the
    condition id and stops clustering with its own bet."""
    from arb.lanes.whale_book import _event_key
    tape = {PCID: {"slug": None, "event_key": f"pm:{PSLUG}"},
            "0xs": {"slug": "mlb-tb-nyy-2026-09-05",
                    "event_key": "baseball_mlb:2026-09-05:TB:NYY"}}
    assert _event_key(PCID, {}, tape) == f"pm:{PSLUG}"
    assert _event_key(PCID, {PCID: "live-slug"}, tape) == "pm:live-slug"
    # a SPORTS row's stored event_key is the ledger's cross-venue key, which §8
    # says never to use — that row still falls back to slug, then condition id
    assert _event_key("0xs", {}, tape) == "pm:mlb-tb-nyy-2026-09-05"
    assert _event_key("0xn", {}, {"0xn": {"slug": None, "event_key": "mlb:x"}}) == "pm:0xn"


# ------------------------------- historical roots (amendment 2026-09-05)
#
# The mapper roots a market from its sport slug prefix alone, so every
# HISTORICAL politics/crypto market a whale traded stayed `sport=NULL` and
# `positions_from_prints` dropped it — no politics whale could ever be scored
# (Opus review C2). Gamma's `markets?condition_ids=` answer carries no `tags`
# and no `category` and its nested `events[0]` carries no `tags` either
# (verified 2026-09-04, re-probed 2026-09-05); the event's OWN row does, and
# `events?id=` batches 20 ids a call exactly as `markets?condition_ids=` does
# (verified live 2026-09-05, data/verified_facts.md). The fixtures below are
# shaped from those probes.

def _gamma_market(cid="0xh", slug="will-x-happen", event_id=33228,
                  end="2026-11-03T12:00:00Z", start="2026-01-01T00:00:00Z",
                  created="2025-12-01T00:00:00Z", sports_type=None):
    """One row of Gamma's `markets?condition_ids=` answer (probe 2026-09-05):
    no `tags`, no `category`, a nested `events[0]` carrying `id` and `slug`
    but NO `tags` key at all."""
    m = {"conditionId": cid, "slug": slug, "endDate": end, "startDate": start,
         "createdAt": created, "clobTokenIds": '["t0", "t1"]',
         "outcomes": '["Yes", "No"]', "volume24hr": "1234",
         "events": [{"id": event_id, "slug": "which-party-wins", "negRisk": False}]}
    if sports_type:
        m["sportsMarketType"] = sports_type
    return m


def _gamma_event(event_id=33228, tags=("politics",), neg_risk=False,
                 start="2025-12-01T00:00:00Z"):
    """One row of `events?id=` (probe 2026-09-05): a populated `tags` array,
    `startDate`, and `negRisk` — which Gamma OMITS when false (measured over
    2,822 events: `negRisk` and `enableNegRisk` never disagree)."""
    e = {"id": event_id, "slug": "which-party-wins", "startDate": start,
         "tags": [{"slug": t, "label": t} for t in tags]}
    if neg_risk:
        e["negRisk"] = True
    return e


def test_a_historical_politics_market_roots_from_its_events_tags(cfg):
    """The shared helper: same tag map, same `market_type` convention, same
    `start_utc := the market's own endDate` the by-tag builder uses."""
    got = whale_nonsports.root_row(cfg, _gamma_event(), _gamma_market())
    assert got == ("politics", "BINARY", "2026-11-03T12:00:00+00:00")
    ladder = whale_nonsports.root_row(cfg, _gamma_event(neg_risk=True), _gamma_market())
    assert ladder[1] == "LADDER"
    # priority order is the builder's own, and an event with none of our tags
    # is not ours at all
    multi = _gamma_event(tags=("trump-presidency", "crypto", "politics"))
    assert whale_nonsports.root_row(cfg, multi, _gamma_market())[0] == "politics"
    assert whale_nonsports.root_row(cfg, _gamma_event(tags=("weather",)),
                                    _gamma_market()) is None


def test_a_five_minute_crypto_market_fails_the_lifetime_floor(cfg):
    """`min_lifetime_hours` (6) is applied exactly as the builder applies it —
    endDate minus the LATEST of the market's startDate, its createdAt and the
    event's startDate — so a genuinely five-minute market is never rooted."""
    short = _gamma_market(cid="0xbtc", slug="btc-updown-5m-1788596100",
                          end="2026-09-05T08:25:00Z", start="2026-09-05T08:20:00Z",
                          created="2026-09-05T08:00:00Z")
    assert whale_nonsports.root_row(cfg, _gamma_event(tags=("crypto",)), short) is None
    # ... and a market with no measurable start is excluded, never assumed
    # long-lived (the builder's own reading)
    nostart = _gamma_market(start=None, created=None)
    assert whale_nonsports.root_row(cfg, _gamma_event(start=None), nostart) is None


# ------------- mechanical price series (fix pass 2, review C1, 2026-09-05)
#
# `root_row`/`markets_of` rooted Polymarket's MECHANICAL crypto price series
# as `crypto`: the up/down cadence events (`btc-updown-5m-<epoch>`, whose
# measured lifetime is 23.9 h, so `min_lifetime_hours` cannot stop them — the
# live tape holds 3,669 of them carrying 14,853 followed-wallet prints) and
# the daily strike ladders. Measured live 2026-09-05 over the whole crypto tag
# surface (1,200 events): 180 markets cleared every registered filter and 173
# of them were that series, so the crypto base rate would have been measured
# on coin flips. Politics and geopolitics lose NOTHING to the exclusion
# (0 of 158 and 0 of 91 at the same depth).

MECHANICAL = ["btc-updown-5m-1785140700", "eth-updown-15m-1785140700",
              "btc-updown-1h-1785140700", "sol-updown-4h-1785140700",
              "bitcoin-above-on-september-5-2026",
              "ethereum-above-on-september-6-2026",
              "what-price-will-bitcoin-hit-august-31-september-6-2026",
              "what-price-will-solana-hit-in-september-2026",
              "bitcoin-price-on-september-5-2026",
              "bitcoin-up-or-down-on-september-5-2026",
              "bitcoin-up-or-down-september-5-2026-10am-et"]


@pytest.mark.parametrize("slug", MECHANICAL)
def test_a_mechanical_price_series_is_never_rooted_nor_built(cfg, slug):
    """Both paths — the by-tag builder and the mapper's historical rooting —
    go through the one shared helper, so neither can admit the series."""
    ev = _event(slug=slug, tags=("crypto",), markets=[_market("0xm", slug=slug)])
    assert whale_nonsports.markets_of(cfg, ev, NOW) == []
    assert whale_nonsports.root_row(cfg, ev, ev["markets"][0]) is None


def test_a_genuine_crypto_event_market_still_roots_crypto(cfg):
    """The root is KEPT (operator decision 2026-09-05, option 1) — only the
    mechanical series is excluded. These three are what survived the exclusion
    on the live crypto surface."""
    for eslug, mslug in (("will-base-launch-a-token-in-2026-341",
                          "will-base-launch-a-token-by-june-30-2026"),
                         ("will-pacifica-launch-a-token-by",
                          "will-pacifica-launch-a-token-by-december-31-2026"),
                         ("will-rise-launch-a-token-by", "will-rise-launch-a-token")):
        ev = _event(slug=eslug, tags=("crypto",), markets=[_market("0xg", slug=mslug)])
        assert [m.condition_id for m in whale_nonsports.markets_of(cfg, ev, NOW)] == ["0xg"]
        assert whale_nonsports.root_row(cfg, ev, ev["markets"][0])[0] == "crypto"


def test_the_exclusion_reads_the_market_slug_as_well_as_the_events(cfg):
    """A strike-ladder market under an event slug no pattern names is still
    the same series — `bitcoin-above-115k-on-september-5-2026` is the market
    slug Gamma actually ships under `bitcoin-above-on-september-5-2026`."""
    ev = _event(slug="crypto-prices-today", tags=("crypto",),
                markets=[_market("0xa", slug="bitcoin-above-115k-on-september-5-2026"),
                         _market("0xb", slug="xrp-above-2pt8-on-september-5-2026")])
    assert whale_nonsports.markets_of(cfg, ev, NOW) == []


def test_a_cadence_slug_is_excluded_whatever_its_dates_say(cfg):
    """The NEGATIVE guard: a slug whose cadence token names a life shorter
    than `min_lifetime_hours` is excluded regardless of the dates, because the
    dates on this series lie (the 5-minute events read a 23.9 h lifetime).
    Registered patterns cannot be the only defence — a cadence family Gamma
    has not shipped yet would walk straight in."""
    ev = _event(slug="foo-bar-15m-1790000000", tags=("crypto",),
                markets=[_market("0xc", slug="foo-bar-15m-1790000000")])
    assert whale_nonsports.markets_of(cfg, ev, NOW) == []
    assert whale_nonsports.root_row(cfg, ev, ev["markets"][0]) is None
    # ... and a cadence at or above the floor is NOT excluded by this guard
    long = _event(slug="foo-bar-12h-1790000000", tags=("crypto",),
                  markets=[_market("0xd", slug="foo-bar-12h-1790000000")])
    assert [m.condition_id for m in whale_nonsports.markets_of(cfg, long, NOW)] == ["0xd"]


# --------------------- the `events?id=` tag shape (fix pass 2, review I1)

def test_root_of_event_reads_the_live_events_by_id_tag_shape(cfg):
    """`tests/fixtures/whale_gamma_events_by_id.json` is the LITERAL body of
    `GET gamma-api.polymarket.com/events?id=750976&id=33228` (2026-09-05):
    `tags` is a list of OBJECTS carrying `slug`/`label`/`id`, not a list of
    bare strings, and `startDate` is present on both rows."""
    body = json.loads((ROOT / "tests/fixtures/whale_gamma_events_by_id.json").read_text())
    by_slug = {e["slug"]: e for e in body}
    politics = by_slug["which-party-wins-2028-us-presidential-election"]
    updown = by_slug["btc-updown-5m-1785140700"]
    assert all(isinstance(t, dict) and t.get("slug") for t in politics["tags"])
    assert politics.get("startDate") and updown.get("startDate")
    assert whale_nonsports.root_of_event(cfg, politics) == "politics"
    assert whale_nonsports.root_of_event(cfg, updown) == "crypto"
    # ... and the mechanical series that same crypto tag would have admitted
    # is excluded by slug, from the live body
    assert whale_nonsports.root_row(cfg, updown, updown["markets"][0]) is None


def test_root_of_event_also_accepts_a_bare_string_tag_list(cfg):
    """A literal string list must not raise inside `_event_roots` — an
    exception there aborts the whole mapping cycle and arms the whale-wide
    backoff every cycle. Defensive, both shapes, one helper."""
    assert whale_nonsports.root_of_event(
        cfg, {"tags": ["us-presidential-election", "politics"]}) == "politics"
    assert whale_nonsports.root_of_event(cfg, {"tags": ["weather"]}) is None
    assert whale_nonsports.root_of_event(cfg, {"tags": None}) is None


def test_a_missing_event_start_date_rejects_rather_than_admits(cfg):
    """`events?id=` DOES carry `startDate` (probe 2026-09-05), but if it ever
    stops, the lifetime floor must reject — never assume long-lived."""
    m = dict(_market("0xn"), startDate=None, createdAt=None)
    ev = _event(slug="will-x-happen", tags=("politics",), markets=[m])
    ev["startDate"] = None
    assert whale_nonsports.root_row(cfg, ev, m) is None
    assert whale_nonsports.markets_of(cfg, ev, NOW) == []


# --------------- the lane's tape read (fix pass 2, review I2/I3, 2026-09-05)

def _lane_row(con, cid, *, days=3, volume="25000",
              tokens=(("t0", "Yes"), ("t1", "No"))):
    from arb.whale_universe import persist_markets
    persist_markets(con, [Market(condition_id=cid, sport="politics",
                                 market_type="BINARY", line=None,
                                 start_utc=(NOW + timedelta(days=days)).isoformat(),
                                 event_key=None, tokens=tokens,
                                 volume_24h=volume)], NOW, first_sight_wins=True)


def test_a_resolved_market_never_enters_the_lane_universe(cfg, con):
    """I2: `tape_universe` re-applied root, window and volume but never asked
    whether the market had already RESOLVED — and the mapper's `closed=true`
    pass writes exactly those rows (`whale_resolve.record_resolution`,
    source='mapper'), rooted by `_event_roots` since 2026-09-05. A resolved
    market with a future stored deadline would have been priced and fired on.
    Both halves of the guard: a `resolutions` row, and a deadline in the
    past."""
    _lane_row(con, "0xopen")
    _lane_row(con, "0xdone")
    con.execute("INSERT INTO resolutions (condition_id, resolved_at, "
                "winner_outcome_index, status, source, fetched_at) "
                "VALUES (?,?,?,?,?,?)",
                ("0xdone", NOW.isoformat(), 0, "resolved", "mapper", NOW.isoformat()))
    con.commit()
    assert [m.condition_id for m in whale_ns_lane.tape_universe(con, cfg, NOW)] \
        == ["0xopen"]
    # ... and past its own deadline it is gone whether or not it resolved
    assert whale_ns_lane.tape_universe(con, cfg, NOW + timedelta(days=4)) == []


def test_a_market_without_two_distinguishable_outcomes_never_enters_the_lane(cfg, con):
    """I3: the mapper wrote `tokens_json` with no two-sided guard, so a market
    whose outcomes collide could reach the lane under a fingerprint both
    tokens share. `_touch` reads outcome indices 0 and 1; anything else is not
    bookable here."""
    _lane_row(con, "0xgood")
    _lane_row(con, "0xdupe", tokens=(("t0", "Yes"), ("t1", "Yes")))
    _lane_row(con, "0xone", tokens=(("t0", "Yes"),))
    assert [m.condition_id for m in whale_ns_lane.tape_universe(con, cfg, NOW)] \
        == ["0xgood"]
