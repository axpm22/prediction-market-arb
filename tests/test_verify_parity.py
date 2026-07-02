"""K1/K2 parity: the flash verifier's effective cost must equal the adapter's
effective_cost at the same raw book state, per venue.

K1 was the verifier pricing Kalshi on the LINEAR fee curve while entry used the
quadratic one (overstating every re-quoted Kalshi leg by 0.07*p^2 — larger than most
real arb edges); K2 was the SX re-quote omitting the win fee and slippage model the
entry price included. Both now route through venues.slippage.leg_fee_cost; these tests
pin the live fetcher paths (which had zero coverage) to the adapters against recorded
fixtures.
"""
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from arb.config import load_config
from arb.experimental import verify
from arb.venues.kalshi import KalshiAdapter
from arb.venues.sx import SxAdapter, requote_sx_leg

FIXTURES = Path("tests/fixtures")
KALSHI_TICKER = "KXMLBGAME-26JUN101940TEXKC-TEX"


class _Resp:
    def __init__(self, payload):
        self.payload = payload
        self.status_code = 200

    def json(self):
        return self.payload

    def raise_for_status(self):
        pass


def test_kalshi_verifier_requote_equals_adapter_entry_cost(monkeypatch) -> None:
    config = load_config()
    quotes = KalshiAdapter(config, FIXTURES).fetch_quotes("baseball_mlb")
    quote = next(q for q in quotes if q.book == KALSHI_TICKER and q.market_type == "WINNER")

    orderbook = json.loads((FIXTURES / "kalshi_baseball_mlb.json").read_text())["orderbooks"][KALSHI_TICKER]
    monkeypatch.setattr(verify.requests, "get", lambda *a, **k: _Resp(orderbook))
    leg = {"venue": "kalshi", "book": KALSHI_TICKER, "market_type": "WINNER",
           "outcome_label": quote.outcome_label}
    requoted = verify._kalshi_current_ask(leg, config)

    # Same book, same moment -> the re-quote IS the entry effective cost. Under the K1 bug
    # (linear basis) this differs by fee_rate*p^2 and the assertion fails.
    assert requoted == quote.effective_cost


def test_sx_requote_equals_adapter_entry_cost_with_win_fee_on() -> None:
    # Force the win fee on (R4 scenario) — K2 was the re-quote grading poly+sx arbs
    # against a cheaper standard than entry the moment this rate becomes non-zero.
    config = replace(load_config(), sx_fee_rate=Decimal("0.02"), sx_win_fee_rate=Decimal("0.05"))
    quotes = SxAdapter(config, FIXTURES).fetch_quotes("baseball_mlb")
    quote = next(q for q in quotes if q.market_type == "WINNER" and q.ref in {"1", "2"})

    payload = json.loads((FIXTURES / "sx_baseball_mlb.json").read_text())
    orders = [o for o in payload["orders"].get(quote.book, [])] if isinstance(payload.get("orders"), dict) else []

    class _Session:
        headers: dict = {}

        def get(self, url, timeout=30):
            return _Resp({"data": orders})

    requoted = requote_sx_leg(quote.book, quote.ref, config.sx_fee_rate,
                              config.taker_price_buffer, session=_Session(),
                              win_fee_rate=config.sx_win_fee_rate,
                              slippage_model=config.taker_slippage_model,
                              slippage_ref_stake=config.slippage_ref_stake_usd)
    assert requoted == quote.effective_cost
