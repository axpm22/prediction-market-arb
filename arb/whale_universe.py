"""L14 the whale lane's market universe: which Polymarket sports markets the
collector tracks this cycle, and the tape's `markets` table.

Split out of arb/whale_tape.py on size alone (modules stay under ~300 lines);
same rules — the ledger is opened READ-ONLY here (rule 9: this file can only
write the tape), no network, no clock (`now` is always an argument).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from arb.logutil import get_logger

log = get_logger("arb.whale", "logs/whale.log")

# `slug` is kept so a market Gamma answered for but whose league code is not
# in whale.slug_sport_prefixes can be re-mapped LOCALLY when that map grows —
# without it, an unmappable market would be stuck at sport=NULL forever.
MARKET_COLS = ("condition_id, sport, market_type, line, start_utc, event_key,"
               " tokens_json, slug, first_seen, last_seen, volume_24h")
# The ledger's PM sports quotes for ONE poll: served by idx_quotes_poll. A
# fetched_at range instead would scan the 37 GB table. `poll_id` is bound by
# the caller (`_poll_with_pm_rows`), never hard-coded to MAX(id) — see there.
UNIVERSE_SQL = (
    "SELECT venue_market_id, sport, market_type, line, outcome, side, start_utc, "
    "event_key FROM quotes WHERE poll_id = ? AND venue='polymarket' AND category='sports'")


@dataclass(frozen=True)
class Market:
    condition_id: str
    sport: str | None
    market_type: str | None
    line: str | None
    start_utc: str | None
    event_key: str | None
    tokens: tuple = ()          # ((token_id, outcome_label), ...)
    # the venue's own 24h volume when a Gamma call already answered it (V6's
    # denominator for a market with no watch quote — non-sports roots, 2026-09-04)
    volume_24h: str | None = None

    @property
    def root(self) -> str | None:
        """This market's whale ROOT, which is also the Polymarket fee CATEGORY
        its leg is priced at (non-sports roots amendment 2026-09-04): `sports`
        for every mapped sport, else the fee category itself. `None` for an
        unmappable market — such a market forms no scored position, so the lane
        never books one, and `leg_fee_cost` would fall back to the venue's
        default `fee_rate` (0.05), dearer than every mapped category."""
        from arb.whale_score import root_of      # local: whale_score imports fees
        return root_of(self.sport, self.market_type)


def _iso(value) -> str | None:
    """Normalise a venue timestamp to the ledger's own ISO form. Gamma sends
    '2026-08-09 09:00:00+00', which SQLite's strftime cannot parse."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).isoformat()
    except ValueError:
        return None


def _labels(tokens: dict) -> dict | None:
    """Token -> the label its leg carries (§8: a WHALE leg BUYS one token, so
    `outcome` is that token's OWN label). A Yes/No proposition — every soccer
    3-way — writes the same question text on BOTH tokens and tells them apart
    by `side` (arb/venues/polymarket.py), so when the texts collide the side
    names the leg: literally "Yes" and "No", which is also what Gamma's own
    `outcomes` carry for such a market, so settlement's label comparison still
    lines up. Still not distinct: None, and the caller skips the market rather
    than booking two tokens under one fingerprint/position_key/quotes key."""
    labels = {t: o for t, (o, _) in tokens.items()}
    if len(set(labels.values())) < len(labels):
        labels = {t: ("No" if s == "NO" else "Yes") for t, (_, s) in tokens.items()}
    return labels if len(set(labels.values())) == len(labels) else None


def _poll_with_pm_rows(store_ro, lookback: int, now, stale_secs: int) -> int | None:
    """The most recent watch poll, within a bounded lookback, that actually
    has a Polymarket sports quotes row. Polymarket's CLOB has intermittent
    TLS failures on its own leg of a watch poll (observed 2026-09-04) while
    the poll itself still lands with rows from other venues/categories —
    strictly the latest poll would then hand the lane an empty universe for
    that whole cycle instead of falling back one poll.

    Time-bounded too (2026-09-04): a poll count alone can't tell a healthy
    fallback from a stalled watch lane handing back a universe hours old with
    no warning. The first candidate WITH rows is checked against
    `data_stale_alarm_secs` (the existing collector-health threshold) and, if
    older, treated as no candidate at all — every poll behind it is only
    older still, so there is nothing left to try."""
    for row in store_ro.execute(
            "SELECT id, started_at FROM polls WHERE kind='watch' ORDER BY id DESC LIMIT ?",
            (lookback,)):
        if store_ro.execute(
                "SELECT 1 FROM quotes WHERE poll_id=? AND venue='polymarket' "
                "AND category='sports' LIMIT 1", (row["id"],)).fetchone():
            age = (now - datetime.fromisoformat(row["started_at"])).total_seconds()
            if age > stale_secs:
                log.warning("whale universe: newest poll with Polymarket sports rows "
                            "(id=%s) is %.0fs old (> data_stale_alarm_secs=%s) — "
                            "treating as no universe", row["id"], age, stale_secs)
                return None
            return row["id"]
    return None


def universe(store_ro, cfg, now, res=None) -> list[Market]:
    """Polymarket sports markets from the most recent watch poll (within
    `whale.universe_poll_lookback`, and no older than `data_stale_alarm_secs`)
    that has any, whose start lies in [now - lookback_hours, now +
    lookahead_hours] (spec §5 step 1). `res` is an optional diagnostics dict,
    as in whale_score.positions_from_prints — this is also where the poll id
    this call picked is handed back (`res["poll_id"]`, I2, mapper fix pass 2
    2026-09-04): a caller that needs a SECOND read against this same universe
    (e.g. `run_whale_cycle`'s VOLUME_SQL) must bind to THIS poll, not
    re-derive its own after network time has passed and a separate watch-lane
    process may have committed a newer one."""
    w = cfg["whale"]
    res = {} if res is None else res
    poll_id = _poll_with_pm_rows(store_ro, int(w["universe_poll_lookback"]), now,
                                 int(cfg["data_stale_alarm_secs"]))
    res["poll_id"] = poll_id
    if poll_id is None:
        return []
    lo = now - timedelta(hours=float(w["lookback_hours"]))
    hi = now + timedelta(hours=float(w["lookahead_hours"]))
    by_cond: dict[str, dict] = {}
    for r in store_ro.execute(UNIVERSE_SQL, (poll_id,)):
        vmid = r["venue_market_id"] or ""
        start = _iso(r["start_utc"])
        if ":" not in vmid or start is None or not (lo <= datetime.fromisoformat(start) <= hi):
            continue
        cond, token = vmid.split(":", 1)
        m = by_cond.setdefault(cond, {"sport": r["sport"], "market_type": r["market_type"],
                                      "line": r["line"], "start_utc": start,
                                      "event_key": r["event_key"], "tokens": {}})
        m["tokens"][token] = (r["outcome"], r["side"])
    out = []
    for cond, m in by_cond.items():
        labels = _labels(m["tokens"])
        if labels is None:
            res["label_collision"] = res.get("label_collision", 0) + 1
            log.warning("whale universe: market %s has %d tokens it cannot tell "
                        "apart (%s) — skipped", cond, len(m["tokens"]),
                        sorted({o for o, _ in m["tokens"].values()}))
            continue
        out.append(Market(condition_id=cond, sport=m["sport"],
                          market_type=m["market_type"], line=m["line"],
                          start_utc=m["start_utc"], event_key=m["event_key"],
                          tokens=tuple(sorted(labels.items()))))
    return out


def persist_markets(con, markets, now, first_sight_wins=False):
    """`first_sight_wins` (non-sports roots, 2026-09-04) pins `sport`,
    `market_type` and `start_utc` to whatever the row already holds instead of
    letting this call's answer win.

    The non-sports universe needs all three. `start_utc` for a non-sports
    market is Gamma's own `endDate`, and Gamma can EDIT an endDate after the
    fact — a moving `start_utc` retroactively changes which of a whale's
    positions were "pregame", which is look-ahead straight into the score
    (proposal §1 Risks).
    `sport` because a market already mapped to a real sport must never be
    re-labelled `politics` by a tag page that happens to list it — and
    `market_type` for the same reason and with the same consequence (I1, fix
    pass): the tag builder derives it from the EVENT's `negRisk` alone, so an
    already-mapped MLB market surfacing on a tag page used to have its WINNER
    rewritten to BINARY, inventing a phantom `baseball_mlb:BINARY` bucket in
    every aggregation keyed on the pair.

    The sports universe keeps the original excluded-wins behaviour: there the
    watch poll IS the authority on all three fields."""
    sport = "COALESCE(sport, excluded.sport)" if first_sight_wins \
        else "COALESCE(excluded.sport, sport)"
    start = "COALESCE(start_utc, excluded.start_utc)" if first_sight_wins \
        else "COALESCE(excluded.start_utc, start_utc)"
    mtype = "COALESCE(market_type, excluded.market_type)" if first_sight_wins \
        else "COALESCE(excluded.market_type, market_type)"
    con.executemany(
        f"INSERT INTO markets ({MARKET_COLS}) VALUES ({','.join('?' * 11)}) "
        "ON CONFLICT(condition_id) DO UPDATE SET last_seen=excluded.last_seen, "
        f"sport={sport}, "
        f"market_type={mtype}, "
        f"start_utc={start}, "
        "tokens_json=excluded.tokens_json, "
        "volume_24h=COALESCE(excluded.volume_24h, volume_24h)",
        [(m.condition_id, m.sport, m.market_type, m.line, m.start_utc, m.event_key,
          json.dumps([list(t) for t in m.tokens]), None, now.isoformat(), now.isoformat(),
          m.volume_24h) for m in markets])
    con.commit()
