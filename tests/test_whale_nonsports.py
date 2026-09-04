"""L14 non-sports roots (amendment 2026-09-04): the Gamma-by-tag universe.

No network: every Gamma answer here is a fixture built in this file, shaped
from the live probes dated in data/verified_facts.md (2026-09-04) — an event
carries `tags`, `negRisk`, `startDate`, `endDate` and nested `markets[]` whose
own `closed`, `conditionId`, `slug`, `outcomes`, `clobTokenIds`, `endDate`,
`startDate`, `createdAt` and `volume24hr` are what the builder filters on.
"""
import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from arb import whale_nonsports, whale_tape
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
        _market("0xfar", slug="far", ends_in_days=30),
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
    lane = whale_nonsports.tape_universe(con, cfg, NOW)
    assert [m.condition_id for m in lane] == ["0xp"]
    assert lane[0].tokens == (("t0", "Yes"), ("t1", "No"))
    assert lane[0].volume_24h == "25000"


def test_the_lane_universe_is_bounded_by_the_nonsports_horizon(cfg, con):
    _persist(cfg, con, [_event(tags=("politics",), markets=[_market("0xp")])])
    late = NOW + timedelta(days=int(cfg["nonsports_max_days"]) + 1)
    assert whale_nonsports.tape_universe(con, cfg, late) == []
    assert whale_nonsports.tape_universe(con, cfg, NOW - timedelta(days=10)) == []


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
    con.execute("INSERT INTO markets (condition_id, sport, market_type, first_seen,"
                " last_seen) VALUES ('0xp','baseball_mlb','WINNER',?,?)",
                (NOW.isoformat(), NOW.isoformat()))
    con.commit()
    _persist(cfg, con, [_event(tags=("politics",), markets=[_market("0xp")])])
    assert con.execute("SELECT sport FROM markets WHERE condition_id='0xp'"
                       ).fetchone()["sport"] == "baseball_mlb"


def test_every_configured_root_is_a_polymarket_fee_category(cfg):
    """Rule 4 + the proposal's most likely silent bug: a root that is not a fee
    category prices the leg at the default rate."""
    from arb.venues.fees import _PM_CATEGORY_RATE_KEY
    roots = set(cfg["whale"]["nonsports"]["tags"].values())
    assert roots and roots <= set(_PM_CATEGORY_RATE_KEY) - {"sports"}
    assert Decimal(str(cfg["whale"]["nonsports"]["min_lifetime_hours"])) == 6
