from decimal import Decimal

from arb.experimental.analysis.devig_regrade import MethodRegrade, format_regrade, regrade_flags


def test_regrade_counts_positive_and_sign_flips() -> None:
    flags = [
        {"event_key": "e1", "outcome_key": "fav", "cost": 0.55},
        {"event_key": "e1", "outcome_key": "dog", "cost": 0.45},
        {"event_key": "e2", "outcome_key": "x", "cost": 0.50},   # no consensus -> skipped
    ]
    consensus = {
        # proportional: fav looks +EV, dog looks -EV
        "proportional": {("e1", "fav"): Decimal("0.60"), ("e1", "dog"): Decimal("0.40")},
        # shin down-weights the longshot 'dog' further and trims the favorite: fav flips to -EV
        "shin": {("e1", "fav"): Decimal("0.52"), ("e1", "dog"): Decimal("0.35")},
    }
    [prop, shin] = regrade_flags(flags, consensus)
    assert prop.n == 2 and prop.n_positive == 1            # fav +0.05, dog -0.05
    assert prop.sign_flips_vs_proportional == 0
    assert shin.n == 2 and shin.n_positive == 0            # fav -0.03, dog -0.10
    assert shin.sign_flips_vs_proportional == 1            # fav flipped + -> -


def test_regrade_handles_no_matches() -> None:
    [r] = regrade_flags([{"event_key": "e", "outcome_key": "o", "cost": 0.5}], {"power": {}})
    assert r == MethodRegrade("power", 0, 0.0, 0.0, 0, 0)


def test_format_is_stable() -> None:
    txt = format_regrade([MethodRegrade("proportional", 10, 0.01, 0.02, 6, 0)])
    assert "IN-SAMPLE" in txt and "proportional" in txt
