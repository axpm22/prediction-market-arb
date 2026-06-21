"""Quality classification — BUILD_PLAN Phase 1.

A flag is 'clean' only if it could plausibly be executed. The contaminants the EV buckets
exposed: tiny asks against thin/empty books producing four-digit "edges". We never delete
(append-only ethos) — we LABEL, and the report shows a clean column beside the raw one.
"""
from __future__ import annotations

from decimal import Decimal

from .models import Opportunity


def classify_quality(opp: Opportunity, min_executable_usd: Decimal, edge_suspect_above: Decimal) -> str | None:
    """Return a quality flag for an opportunity, or None if clean.

    - 'suspect-edge': |edge| beyond any plausible real edge → stale/one-sided book leak.
    - 'thin-book': a leg's depth at the quoted price is below the executable floor.
    Order matters: suspect-edge is the strongest signal (a 49.0 "edge" is a $0.02 ask),
    so it is checked first.
    """
    if abs(opp.edge) > edge_suspect_above:
        return "suspect-edge"
    for leg in opp.legs:
        if leg.depth_usd is not None and leg.depth_usd < min_executable_usd:
            return "thin-book"
    return None
