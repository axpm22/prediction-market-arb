"""Deterministic strike narrowing for multi-strike nonsports series (weather/macro) —
STRIKE_AWARE_NONSPORTS_MATCHER. Stage 1 of the two-stage matcher: free, pure computation
over listings the watcher already fetched. It expands a multi-market Kalshi series /
Polymarket event into individual strikes, parses each into a normalized StrikeSpec, and
pairs only strikes that block (same date + fuzzy entity) AND align EXACTLY (identical
comparator/value/unit or identical range bounds+inclusivity). Only aligned survivors reach
Haiku (stage 2). Strictness is the point: the venues bucket differently, so a non-identical
strike is left to a human map entry, never auto-aligned (that is how fake arbs are born).

No I/O, no new endpoints (CLAUDE.md rule 3): callers pass in the market dicts already
fetched. Decimal only for strike math (rule 5).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from arb.matching import normalize_name, token_ratio

# Comparator canonical set (thresholds). ">=" and ">" are distinct: the parser does no
# integer-domain reasoning, so ">99" and ">=100" never align (rule from the spec).
GE, GT, LE, LT = ">=", ">", "<=", "<"


@dataclass(frozen=True)
class StrikeSpec:
    kind: str                       # "threshold" | "range"
    unit: str                       # normalized: F, C, pct, bps, index, count, deg
    comparator: str | None = None   # threshold only: >=, >, <=, <
    value: Decimal | None = None    # threshold only
    low: Decimal | None = None      # range only
    high: Decimal | None = None     # range only
    low_inclusive: bool = True      # range only
    high_inclusive: bool = True     # range only
    dual_unit: bool = False         # market stated BOTH °F and °C → F/C conversion permitted


# --- unit detection -------------------------------------------------------------------

def detect_unit(text: str) -> str:
    t = text.lower()
    has_f = bool(re.search(r"°\s*f\b|\bfahrenheit\b|deg(?:rees)?\s*f\b", t))
    has_c = bool(re.search(r"°\s*c\b|\bcelsius\b|deg(?:rees)?\s*c\b", t))
    if has_f:
        return "F"
    if has_c:
        return "C"
    if "°" in t or "degree" in t:
        return "deg"
    if "bps" in t or "basis point" in t:
        return "bps"
    if "%" in t or "percent" in t:
        return "pct"
    if "index point" in t or re.search(r"\bindex\b", t):
        return "index"
    return "count"


def _dual_unit(text: str) -> bool:
    t = text.lower()
    return bool(re.search(r"°\s*f", t)) and bool(re.search(r"°\s*c", t))


def _num(raw: str) -> Decimal | None:
    try:
        return Decimal(raw.replace(",", "").replace("m", "").strip())
    except (InvalidOperation, AttributeError):
        return None


# --- text parsing (Polymarket questions, Kalshi title fallback) ------------------------

_RANGE_BRACKET = re.compile(r"([\[\(])\s*(-?\d[\d,\.]*)\s*,\s*(-?\d[\d,\.]*)\s*([\]\)])")
_RANGE_WORDS = re.compile(
    r"\bbetween\s+(-?\d[\d,\.]*)\s*(?:m\b)?\s*(?:&|and|to|-|–)\s*(-?\d[\d,\.]*)", re.I)
_RANGE_DASH = re.compile(r"(?<![\d.])(-?\d[\d,\.]*)\s*(?:°|m\b)?\s*(?:to|-|–)\s*(-?\d[\d,\.]*)", re.I)
_GE = re.compile(r"(?:>=|≥|at least|no (?:less|fewer) than|(-?\d[\d,\.]*)\s*(?:°[fc]?|%|bps)?\s*or (?:above|higher|more|over))", re.I)
_LE = re.compile(r"(?:<=|≤|at most|no more than|(-?\d[\d,\.]*)\s*(?:°[fc]?|%|bps)?\s*or (?:below|lower|less|under))", re.I)
_GT = re.compile(r"(?:>|greater than|more than|above|over)\s*(-?\d[\d,\.]*)", re.I)
_LT = re.compile(r"(?:<|less than|fewer than|below|under)\s*(-?\d[\d,\.]*)", re.I)
_GE_NUM = re.compile(r"(?:>=|≥)\s*(-?\d[\d,\.]*)|(?:at least|no (?:less|fewer) than)\s+(-?\d[\d,\.]*)", re.I)
_LE_NUM = re.compile(r"(?:<=|≤)\s*(-?\d[\d,\.]*)|(?:at most|no more than)\s+(-?\d[\d,\.]*)", re.I)


def parse_strike(text: str) -> StrikeSpec | None:
    """Parse a StrikeSpec from free strike text (Polymarket question/groupItemTitle or a
    Kalshi title). Returns None when no strike is recognizable — the caller logs the gap
    (never silently swallow) and falls back to the single-market path if applicable."""
    if not text:
        return None
    unit = detect_unit(text)
    dual = _dual_unit(text)
    # A parenthetical unit restatement ("100°F (37.8°C)") is what LICENSES conversion, but its
    # number must not be mistaken for the strike — drop it before parsing (dual is already set).
    text = re.sub(r"\([^)]*°[^)]*\)", " ", text)

    bracket = _RANGE_BRACKET.search(text)
    if bracket:
        low, high = _num(bracket.group(2)), _num(bracket.group(3))
        if low is None or high is None:
            return None
        return _range(low, high, bracket.group(1) == "[", bracket.group(4) == "]", unit, dual)

    words = _RANGE_WORDS.search(text)
    if words:
        low, high = _num(words.group(1)), _num(words.group(2))
        if low is not None and high is not None:
            # "between X & Y" on continuous quantities is half-open [X, Y); integer buckets
            # ("X to Y") are inclusive. We cannot know the domain, so treat "&"/"and" as
            # half-open and "to"/"-" as inclusive — Haiku is the semantic backstop either way.
            half_open = bool(re.search(r"&|and", words.group(0), re.I))
            return _range(low, high, True, not half_open, unit, dual)

    return _threshold_from_text(text, unit, dual)


def _threshold_from_text(text: str, unit: str, dual: bool) -> StrikeSpec | None:
    for pattern, comp in ((_GE_NUM, GE), (_LE_NUM, LE)):
        m = pattern.search(text)
        if m:
            val = _num(next(g for g in m.groups() if g))
            if val is not None:
                return StrikeSpec("threshold", unit, comparator=comp, value=val, dual_unit=dual)
    m = re.search(r"(-?\d[\d,\.]*)\s*(?:°[fc]?|%|bps)?\s*or (?:above|higher|more|over)", text, re.I)
    if m and (val := _num(m.group(1))) is not None:
        return StrikeSpec("threshold", unit, comparator=GE, value=val, dual_unit=dual)
    m = re.search(r"(-?\d[\d,\.]*)\s*(?:°[fc]?|%|bps)?\s*or (?:below|lower|less|under)", text, re.I)
    if m and (val := _num(m.group(1))) is not None:
        return StrikeSpec("threshold", unit, comparator=LE, value=val, dual_unit=dual)
    for pattern, comp in ((_GT, GT), (_LT, LT)):
        m = pattern.search(text)
        if m and (val := _num(m.group(1))) is not None:
            return StrikeSpec("threshold", unit, comparator=comp, value=val, dual_unit=dual)
    m = re.search(r"(?:>=|≥)\s*(-?\d[\d,\.]*)", text)
    if m and (val := _num(m.group(1))) is not None:
        return StrikeSpec("threshold", unit, comparator=GE, value=val, dual_unit=dual)
    m = re.search(r"(?:<=|≤|<)\s*(-?\d[\d,\.]*)", text)
    if m and (val := _num(m.group(1))) is not None:
        comp = LT if "<" in m.group(0) and "=" not in m.group(0) and "≤" not in m.group(0) else LE
        return StrikeSpec("threshold", unit, comparator=comp, value=val, dual_unit=dual)
    return None


def _range(low: Decimal, high: Decimal, low_inc: bool, high_inc: bool, unit: str, dual: bool) -> StrikeSpec:
    return StrikeSpec("range", unit, low=low, high=high,
                      low_inclusive=low_inc, high_inclusive=high_inc, dual_unit=dual)


# --- Kalshi structured strike (preferred over title parsing) ---------------------------

def kalshi_strike(market: dict) -> StrikeSpec | None:
    """StrikeSpec from Kalshi's structured strike fields (strike_type + cap/floor_strike);
    falls back to parsing the title when the fields are absent. YES resolves when the
    measured value lands on the described side, so 'less'/cap=98 → YES iff value < 98."""
    stype = str(market.get("strike_type") or "").lower()
    cap, floor = market.get("cap_strike"), market.get("floor_strike")
    unit = detect_unit(f"{market.get('title', '')} {market.get('yes_sub_title', '')}")
    if stype == "greater" and floor is not None:
        return StrikeSpec("threshold", unit, comparator=GT, value=_dec(floor))
    if stype in ("greater_or_equal", "greater_equal") and floor is not None:
        return StrikeSpec("threshold", unit, comparator=GE, value=_dec(floor))
    if stype == "less" and cap is not None:
        return StrikeSpec("threshold", unit, comparator=LT, value=_dec(cap))
    if stype in ("less_or_equal", "less_equal") and cap is not None:
        return StrikeSpec("threshold", unit, comparator=LE, value=_dec(cap))
    if stype == "between" and cap is not None and floor is not None:
        # Kalshi integer temp buckets ("98° to 99°") are inclusive on both ends.
        return _range(_dec(floor), _dec(cap), True, True, unit, False)
    return parse_strike(str(market.get("yes_sub_title") or market.get("title") or ""))


def _dec(value) -> Decimal:
    return Decimal(str(value))


# --- alignment ------------------------------------------------------------------------

def _to_common_unit(a: StrikeSpec, b: StrikeSpec) -> tuple[StrikeSpec | None, StrikeSpec | None]:
    if a.unit == b.unit:
        return a, b
    pair = {a.unit, b.unit}
    # °F↔°C conversion ONLY when a side explicitly stated both units (spec rule).
    if pair == {"F", "C"} and (a.dual_unit or b.dual_unit):
        conv = a if a.unit == "C" else b
        other = b if conv is a else a
        return (_celsius_to_f(conv), other) if conv is a else (other, _celsius_to_f(conv))
    return None, None


def _celsius_to_f(spec: StrikeSpec) -> StrikeSpec:
    f = lambda c: c * Decimal("9") / Decimal("5") + Decimal("32") if c is not None else None
    return StrikeSpec(spec.kind, "F", spec.comparator, f(spec.value),
                      f(spec.low), f(spec.high), spec.low_inclusive, spec.high_inclusive, True)


def aligned(a: StrikeSpec | None, b: StrikeSpec | None) -> bool:
    """True iff two strikes are EXACTLY the same bet: same comparator+value (thresholds) or
    same bounds+inclusivity (ranges), after unit normalization. No integer-domain reasoning,
    no half-width tolerance — a `[70,75)` vs `[70,72.5)` or `>99` vs `>=100` pair is False."""
    if a is None or b is None:
        return False
    a, b = _to_common_unit(a, b)
    if a is None or b is None or a.kind != b.kind:
        return False
    if a.kind == "threshold":
        return a.comparator == b.comparator and a.value == b.value
    return (a.low == b.low and a.high == b.high
            and a.low_inclusive == b.low_inclusive and a.high_inclusive == b.high_inclusive)


# --- blocking + candidate assembly ----------------------------------------------------

_STRIKE_STRIP = re.compile(
    r"(>=|<=|>|<|≥|≤|°[fc]?|between|at least|at most|or (?:above|below|higher|lower|more|less)|"
    r"greater than|less than|more than|fewer than|\bunder\b|\bover\b|\d[\d,\.]*|[\[\(].*?[\]\)])",
    re.I)


def _entity_text(title: str) -> str:
    """Title with strike/comparator/number noise stripped, for fuzzy entity matching so
    '<98° in NYC on Jul 3' and 'NYC high temperature Jul 3' compare on the entity, not the
    strike (which the alignment check owns)."""
    stripped = _STRIKE_STRIP.sub(" ", title)
    return normalize_name(re.sub(r"\s+", " ", stripped).strip(), "nonsports", aliases={})


@dataclass(frozen=True)
class StrikeCandidate:
    kalshi_ticker: str
    polymarket_condition_id: str
    polymarket_token_id_yes: str | None
    spec: StrikeSpec
    close_date: date


def strike_candidates(kalshi_markets: list[dict], polymarket_markets: list[dict],
                      threshold: int, on_parse_gap=None) -> list[StrikeCandidate]:
    """Block (same close date + fuzzy entity ≥ threshold) then align (exact strike). Returns
    the aligned survivors as (kalshi_ticker, polymarket_condition_id, spec). `on_parse_gap`
    is called with a one-line reason for each market whose strike can't be parsed."""
    def log_gap(msg: str) -> None:
        if on_parse_gap is not None:
            on_parse_gap(msg)

    k_specs = []
    for m in kalshi_markets:
        spec = kalshi_strike(m)
        if spec is None:
            log_gap(f"strike-parse-gap kalshi:{m.get('ticker')} {m.get('yes_sub_title')!r}")
            continue
        k_specs.append((m, spec, _entity_text(str(m.get("title") or "")),
                        _close_date(m.get("close_time"))))

    p_specs = []
    for m in polymarket_markets:
        text = str(m.get("question") or m.get("groupItemTitle") or "")
        spec = parse_strike(text)
        if spec is None:
            log_gap(f"strike-parse-gap polymarket:{m.get('conditionId') or m.get('id')} {text!r}")
            continue
        p_specs.append((m, spec, _entity_text(text), _close_date(m.get("endDate"))))

    candidates: list[StrikeCandidate] = []
    for km, k_spec, k_entity, k_date in k_specs:
        for pm, p_spec, p_entity, p_date in p_specs:
            if k_date is None or p_date is None or k_date != p_date:
                continue
            if token_ratio(k_entity, p_entity) < threshold:
                continue
            if not aligned(k_spec, p_spec):
                continue
            candidates.append(StrikeCandidate(
                kalshi_ticker=str(km.get("ticker")),
                polymarket_condition_id=str(pm.get("conditionId") or pm.get("id")),
                polymarket_token_id_yes=_yes_token(pm),
                spec=k_spec, close_date=k_date,
            ))
    return candidates


def _yes_token(market: dict) -> str | None:
    import json
    try:
        outcomes = json.loads(market.get("outcomes") or "[]")
        tokens = json.loads(market.get("clobTokenIds") or "[]")
    except (TypeError, ValueError):
        return None
    for label, token in zip(outcomes, tokens):
        if str(label).strip().lower() in ("yes", "y"):
            return str(token)
    return None


def _close_date(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
