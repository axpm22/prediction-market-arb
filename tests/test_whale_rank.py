"""L14 read-time trader profiles (design §12 "Trader profiles"): rank every
scored wallet and say where it is good.

Hand-built positions, no tape and no clock for the pure tests; a tiny tmp tape
for the CLI. Nothing here touches the network or the production DBs.
"""
import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from arb import commands, whale_rank, whale_score, whale_tape
from arb.config import load
from arb.models import q6
from arb.whale_state import WhaleState

ROOT = Path(__file__).resolve().parent.parent

AS_OF = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
START = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
RESOLVED_AT = (AS_OF - timedelta(days=1)).isoformat()
T = int(START.timestamp()) - 3600          # one hour before start: pregame


@pytest.fixture
def cfg():
    return copy.deepcopy(load(ROOT / "config.yaml"))


# ---------------------------------------------------------------- builders

def _bucket(wallet, price, wins, losses, res, sport="basketball_nba",
            mtype="WINNER", size="100"):
    """`wins + losses` positions in one bucket, every one bought at `price`.

    A win resolves to the outcome the wallet holds (payout 1), a loss to the
    other one — so `implied` is exactly `price` and the realized edge is
    `1 - price` or `-price`.
    """
    out = []
    for i in range(wins + losses):
        cid = f"0x{wallet}{sport}{mtype}{price}{i}"
        out.append(whale_score.Position(
            wallet=wallet, condition_id=cid, outcome_index=0,
            avg_price=q6(Decimal(price)), size=Decimal(size),
            notional_usd=q6(Decimal(price) * Decimal(size)), entry_ts=T,
            last_ts=T, pregame=True, sport=sport, market_type=mtype,
            root=whale_score.root_of(sport, mtype), source="tape"))
        res[cid] = {"status": "resolved", "resolved_at": RESOLVED_AT,
                    "winner_outcome_index": 0 if i < wins else 1}
    return out


def _state(cfg, positions, resolutions, excluded=()):
    """A `WhaleState` with the fields `profiles` reads, assembled exactly as
    `whale_state.score_state` assembles them — so `lookup` is the lane's own
    score, never a stand-in. `excluded` names hygiene-excluded wallets."""
    open_pos = [p for p in positions if p.wallet not in set(excluded)]
    outcomes = whale_score.resolved_outcomes(open_pos, resolutions, AS_OF)
    agg = whale_score.aggregate(outcomes)
    rates = {r: whale_score.base_rate(outcomes, r, AS_OF, cfg)[0]
             for r in ("sports", "nonsports")}
    return WhaleState(
        prints=[], markets={}, resolutions=resolutions, positions=positions,
        open_positions=open_pos, hygiene_excluded=len(positions) - len(open_pos),
        rates=rates, rate_n={}, agg=agg,
        lookup=lambda w, s, m: whale_score.score_for(agg, w, s, m, cfg, rates))


def _two_wallets(cfg, excluded=()):
    """wA: 10 at 0.60, 8 won — 0.8 win rate against a 0.6 break-even.
    wB: 10 at 0.30, 3 won — exactly what it paid for, so zero excess."""
    res = {}
    pos = (_bucket("wA", "0.60", 8, 2, res) + _bucket("wB", "0.30", 3, 7, res))
    state = _state(cfg, pos, res, excluded)
    return state, whale_rank.profiles(state, cfg, AS_OF)


def _find(rows, wallet, level, key=None):
    got = [r for r in rows if r["wallet"] == wallet and r["level"] == level
           and (key is None or r["key"] == key)]
    assert len(got) == 1, f"{wallet}/{level}/{key}: {len(got)} rows"
    return got[0]


# --------------------------------------------------------------- profiles

def test_a_profile_prices_the_wallet_against_what_the_market_charged_it(cfg):
    """`excess_win_rate` IS "success rate above average based on risk": the
    price a wallet paid is the break-even win rate the market quoted it."""
    _, rows = _two_wallets(cfg)

    a = _find(rows, "wA", "bucket", "basketball_nba:WINNER")
    assert (a["n"], a["wins"]) == (10, 8)
    assert a["win_rate"] == q6(Decimal("0.8"))
    assert a["implied"] == q6(Decimal("0.6"))
    assert a["excess_win_rate"] == q6(Decimal("0.2"))
    assert a["edge_mean"] == q6(Decimal("0.2"))
    # 8 x (+0.40) + 2 x (-0.60) = +2.00 per share, on 0.60 x 100 x 10 at risk
    assert a["roi_at_risk"] == q6(Decimal("200") / Decimal("600"))
    assert a["notional"] == q6(Decimal("600"))         # 10 x 0.60 x 100 shares
    assert a["excess_vs_base"] == q6(Decimal("0.2") - Decimal("-0.02"))
    assert a["last_resolved_at"] == RESOLVED_AT
    assert a["native"] is True and a["hygiene_flag"] is False


def test_a_wallet_that_only_got_what_it_paid_for_has_no_excess(cfg):
    _, rows = _two_wallets(cfg)

    b = _find(rows, "wB", "bucket", "basketball_nba:WINNER")
    assert b["win_rate"] == b["implied"] == q6(Decimal("0.3"))
    assert b["excess_win_rate"] == q6(Decimal("0"))
    assert b["edge_mean"] == q6(Decimal("0"))
    assert b["roi_at_risk"] == q6(Decimal("0"))
    assert b["t_stat"] == q6(Decimal("0"))


def test_the_t_stat_is_the_edge_over_its_own_standard_error(cfg):
    """8 x +0.4 and 2 x -0.6: mean 0.2, sample sd sqrt(1.6/9), n 10 -> t = 1.5
    exactly. n < 2 or a flat sample leaves it null rather than infinite."""
    _, rows = _two_wallets(cfg)
    a = _find(rows, "wA", "bucket", "basketball_nba:WINNER")
    assert abs(a["t_stat"] - Decimal("1.5")) <= Decimal("0.000001")

    res = {}
    one = _bucket("wS", "0.50", 1, 0, res)
    single = _find(whale_rank.profiles(_state(cfg, one, res), cfg, AS_OF),
                   "wS", "bucket")
    assert single["n"] == 1 and single["t_stat"] is None
    assert single["edge_sd"] == q6(Decimal(0))


def test_the_bucket_row_carries_the_lanes_own_lb80(cfg):
    """The rank table must never quote a different lb80 from the one the fire
    rule reads: at bucket level the row IS `score_for(...).lb`."""
    state, rows = _two_wallets(cfg)

    a = _find(rows, "wA", "bucket", "basketball_nba:WINNER")
    assert a["lb80"] == state.lookup("wA", "basketball_nba", "WINNER").lb


def test_sport_and_root_rows_are_the_posterior_truncated_to_their_level(cfg):
    """Same chain, stopped at that level — a sport row is not a bucket row and
    is shrunk less far, so its lb80 differs and is never null."""
    _, rows = _two_wallets(cfg)

    root = _find(rows, "wA", "root", "sports")
    sport = _find(rows, "wA", "sport", "basketball_nba")
    bucket = _find(rows, "wA", "bucket", "basketball_nba:WINNER")
    assert root["n"] == sport["n"] == bucket["n"] == 10
    assert None not in (root["lb80"], sport["lb80"])
    assert root["lb80"] != sport["lb80"] != bucket["lb80"]
    assert root["native"] is False and sport["native"] is False
    # a root row is every sport at once, so it names none
    assert [r["sport"] for r in (root, sport, bucket)] == \
        [None, "basketball_nba", "basketball_nba"]


def test_rank_orders_by_lb80_and_applies_the_display_floor(cfg):
    _, rows = _two_wallets(cfg)

    ranked = whale_rank.rank(rows, cfg)
    assert [r["wallet"] for r in ranked] == ["wA", "wB"]
    assert all(r["level"] == "bucket" for r in ranked)
    assert whale_rank.rank(rows, cfg, min_n=11) == []
    assert len(whale_rank.rank(rows, cfg, level="root")) == 2


def test_a_hygiene_excluded_wallet_is_ranked_and_flagged_never_hidden(cfg):
    _, rows = _two_wallets(cfg, excluded=["wB"])

    b = _find(rows, "wB", "bucket", "basketball_nba:WINNER")
    assert b["hygiene_flag"] is True and b["n"] == 10
    assert "wB" in {r["wallet"] for r in whale_rank.rank(rows, cfg)}
    # the clean wallet's score is untouched by its neighbour's exclusion
    assert _find(rows, "wA", "bucket")["hygiene_flag"] is False


def test_best_markets_names_where_the_wallet_is_actually_good(cfg):
    res = {}
    pos = (_bucket("wA", "0.60", 8, 2, res)
           + _bucket("wA", "0.60", 3, 7, res, sport="baseball_mlb")
           + _bucket("wA", "0.60", 2, 0, res, mtype="TOTAL"))   # n = 2, below the floor
    rows = whale_rank.profiles(_state(cfg, pos, res), cfg, AS_OF)

    best = whale_rank.best_markets(rows, "wA", 3, cfg["whale"]["rank"]["min_n"])
    assert [r["key"] for r in best] == ["basketball_nba:WINNER", "baseball_mlb:WINNER"]
    assert whale_rank.best_markets(rows, "wA", 1, 5)[0]["key"] == "basketball_nba:WINNER"
    # no floor: the two-position bucket comes back too
    assert len(whale_rank.best_markets(rows, "wA", 9)) == 3


def test_wallet_profile_is_root_then_sport_then_bucket(cfg):
    _, rows = _two_wallets(cfg)

    got = whale_rank.wallet_profile(rows, "wA")
    assert [r["level"] for r in got] == ["root", "sport", "bucket"]
    assert all(r["wallet"] == "wA" for r in got)


# -------------------------------------------------------------------- CLI

def _tape(tmp_path, cfg, now=None):
    """A tape one wallet has six resolved pregame positions on (5 won), dated
    relative to `now` — the real clock by default, because that is the `as_of`
    the CLI and the dashboard run at; the daily report passes its day's end."""
    now = now or datetime.now(timezone.utc)
    start, resolved = now - timedelta(days=2), (now - timedelta(days=1)).isoformat()
    path = tmp_path / "tape.sqlite"
    con = whale_tape.open_tape(path)
    for i in range(6):
        cid = f"0xc{i}"
        con.execute(
            "INSERT INTO markets (condition_id, sport, market_type, start_utc, "
            "event_key) VALUES (?,?,?,?,?)",
            (cid, "basketball_nba", "WINNER", start.isoformat(), f"pm:{i}"))
        con.execute(
            "INSERT INTO prints (observed_at, ts, tx_hash, proxy_wallet, "
            "condition_id, asset, outcome_index, side, price, size, "
            "notional_usd, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (start.isoformat(), int(start.timestamp()) - 3600, f"0xt{i}",
             "0xwhale", cid, "0xyes", 0, "BUY", "0.600000", "1000.000000",
             "600.000000", "tape"))
        con.execute(
            "INSERT INTO resolutions (condition_id, resolved_at, "
            "winner_outcome_index, status) VALUES (?,?,?,?)",
            (cid, resolved, 0 if i < 5 else 1, "resolved"))
    con.commit()
    con.close()
    cfg["whale"]["tape_db"] = str(path)
    return path


class _Args:
    def __init__(self, level="bucket", wallet=None, top=None, min_n=None):
        self.level, self.wallet, self.top, self.min_n = level, wallet, top, min_n


def _rows(out, wallet="0xwhale"):
    return [ln for ln in out.splitlines() if ln.startswith(wallet + " ")]


def test_cmd_whale_rank_prints_a_ranked_table_from_the_tape(cfg, tmp_path, capsys):
    _tape(tmp_path, cfg)

    rc = commands.cmd_whale_rank(cfg, None, _Args())

    assert rc == 0
    out = capsys.readouterr().out
    assert "lb80" in out and "excess%" in out and "roi@risk" in out
    assert len(_rows(out)) == 1 and "basketball_nba:WINNER" in _rows(out)[0]


def test_cmd_whale_rank_wallet_prints_the_whole_profile(cfg, tmp_path, capsys):
    _tape(tmp_path, cfg)

    commands.cmd_whale_rank(cfg, None, _Args(wallet="0xwhale"))

    out = capsys.readouterr().out
    assert len(_rows(out)) == 3                       # root, sport, bucket
    assert "sports" in out and "basketball_nba:WINNER" in out


def test_cmd_whale_rank_says_so_when_the_tape_is_missing(cfg, tmp_path, capsys):
    cfg["whale"]["tape_db"] = str(tmp_path / "nope.sqlite")

    assert commands.cmd_whale_rank(cfg, None, _Args()) == 0
    assert "tape unavailable" in capsys.readouterr().out


def test_whale_rank_is_registered_as_a_subcommand():
    src = (ROOT / "arb" / "cli.py").read_text()
    assert '"whale-rank"' in src and "--min-n" in src and "--level" in src
