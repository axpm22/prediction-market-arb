from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .models import Opportunity
from .pricing import q6

RULE_ID = "H-EVFILTER"
KEEP = "KEEP"
DROP = "DROP"
PENDING_CONFIRM = "PENDING_CONFIRM"


@dataclass(frozen=True)
class ValidationRule:
    rule_id: str = RULE_ID
    kind: str = "EV"
    min_cost: Decimal = Decimal("0.40")
    max_cost: Decimal = Decimal("0.75")
    max_flagged_edge: Decimal = Decimal("0.02")
    target_n: int = 190
    halfway_n: int = 95
    kill_consecutive_losses: int = 10


def load_validation_rule(config) -> ValidationRule:
    raw = getattr(config, "validation", {}).get("ev_filter", {})
    return ValidationRule(
        rule_id=str(raw.get("rule_id", RULE_ID)),
        kind=str(raw.get("kind", "EV")),
        min_cost=Decimal(str(raw.get("min_cost", "0.40"))),
        max_cost=Decimal(str(raw.get("max_cost", "0.75"))),
        max_flagged_edge=Decimal(str(raw.get("max_flagged_edge", "0.02"))),
        target_n=int(raw.get("target_n", 190)),
        halfway_n=int(raw.get("halfway_n", 95)),
        kill_consecutive_losses=int(raw.get("kill_consecutive_losses", 10)),
    )


def classify_validation(opp: Opportunity, status: str, rule: ValidationRule) -> tuple[str | None, str | None, tuple[str, ...]]:
    """Return (rule_id, tag, notes) for the frozen validation rule.

    The confirmable requirement is only knowable on the append-only CONFIRMED row. First
    observations that pass the price/edge gates are therefore PENDING_CONFIRM, not KEEP.
    """
    if opp.kind != rule.kind:
        return None, None, ()
    notes: list[str] = []
    cost = _effective_cost(opp)
    if cost is None:
        return rule.rule_id, DROP, ("validation-drop:missing-cost",)
    # #5 max_flagged_edge is in probability POINTS (it sits beside a 0.40-0.75 cost band),
    # so compare against edge_points (p - c), NOT opp.edge — which for EV is the ratio
    # (p-c)/c and would make the cut cost-dependent (a true 2-pt edge at c=0.50 reads 0.04).
    edge_pts = opp.edge_points if opp.edge_points is not None else opp.edge
    passes_price_edge = rule.min_cost <= cost <= rule.max_cost and edge_pts < rule.max_flagged_edge
    if not passes_price_edge:
        if cost < rule.min_cost or cost > rule.max_cost:
            notes.append("validation-drop:cost-band")
        if edge_pts >= rule.max_flagged_edge:
            notes.append("validation-drop:flagged-edge")
        return rule.rule_id, DROP, tuple(notes)
    if status == "CONFIRMED":
        return rule.rule_id, KEEP, ("validation-keep",)
    return rule.rule_id, PENDING_CONFIRM, ("validation-pending-confirm",)


def validation_label(rule_id: str | None, tag: str | None) -> str:
    if rule_id is None or tag is None:
        return ""
    return f"{rule_id}:{tag}"


def _effective_cost(opp: Opportunity) -> Decimal | None:
    if opp.kind != "EV" or not opp.legs:
        return None
    return q6(opp.legs[0].effective_cost)
