from decimal import Decimal
from pathlib import Path

from arb.config import load_config
from arb.pricing import depth_scaled_buffer, walk_ladder
from arb.venues.slippage import effective_buffer

FIX = Path("tests/fixtures")


def test_walk_ladder_vwap_and_fill() -> None:
    ladder = [(Decimal("0.50"), Decimal("50")), (Decimal("0.55"), Decimal("50"))]
    vwap, filled = walk_ladder(ladder, Decimal("50"))
    assert vwap == Decimal("0.500000") and filled == Decimal("50.000000")   # top level fills it
    vwap2, filled2 = walk_ladder(ladder, Decimal("100"))
    assert filled2 == Decimal("100.000000") and Decimal("0.50") < vwap2 < Decimal("0.55")


def test_walk_ladder_depth_capped() -> None:
    _, filled = walk_ladder([(Decimal("0.50"), Decimal("30"))], Decimal("100"))
    assert filled == Decimal("30.000000")   # can't fill 100 from a 30-deep book


def test_depth_scaled_buffer_floors_at_base_and_scales_with_size() -> None:
    ladder = [(Decimal("0.50"), Decimal("20")), (Decimal("0.60"), Decimal("80"))]
    base = Decimal("0.005")
    # filling 20 stays at top -> no walk -> floored at base buffer.
    assert depth_scaled_buffer(ladder, Decimal("20"), base) == base
    # filling 100 walks into the 0.60 level -> buffer well above base.
    assert depth_scaled_buffer(ladder, Decimal("100"), base) > base


def test_effective_buffer_flat_vs_ladder() -> None:
    ladder = ((Decimal("0.50"), Decimal("20")), (Decimal("0.60"), Decimal("80")))
    base = Decimal("0.005")
    assert effective_buffer(ladder, "flat", Decimal("100"), base) == base           # flat = legacy
    assert effective_buffer(ladder, "ladder", Decimal("100"), base) > base          # depth-scaled
    assert effective_buffer(None, "ladder", Decimal("100"), base) == base           # no ladder -> base


def test_kalshi_adapter_captures_ladder() -> None:
    from arb.venues.kalshi import KalshiAdapter
    qs = KalshiAdapter(load_config(), FIX).fetch_quotes("baseball_mlb")
    winners = [q for q in qs if q.market_type == "WINNER"]
    assert winners and any(q.ladder for q in winners)   # the fixture orderbook yields a ladder
