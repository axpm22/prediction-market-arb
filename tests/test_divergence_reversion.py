from arb.experimental.analysis.divergence_reversion import reversion_stats


def test_large_spreads_that_revert_beat_baseline() -> None:
    series = {
        # a large spread (0.10) that collapses to 0.01 next poll -> reverts
        ("e1", "a"): [(1, 0.10), (2, 0.01), (3, 0.005)],
        # small spreads wandering -> baseline
        ("e2", "b"): [(1, 0.01), (2, 0.02), (3, 0.015)],
        ("e3", "c"): [(1, 0.08), (2, 0.02)],   # another reverting large spread
    }
    s = reversion_stats(series, large_threshold=0.05)
    assert s.n_large == 2                       # the 0.10 and 0.08 starts
    assert s.n_narrowed_next == 2
    assert s.narrow_rate == 1.0
    assert s.mean_delta_large < 0               # reverts on average
    assert s.n_baseline == 5                    # total consecutive pairs across all series


def test_no_large_spreads_is_empty() -> None:
    s = reversion_stats({("e", "o"): [(1, 0.01), (2, 0.01)]}, large_threshold=0.05)
    assert s.n_large == 0 and s.narrow_rate == 0.0
