"""Lab 4 — unit tests for the drift statistics and the input-stream generator.

Fast, no network, no data files. They pin down what the detector's numbers mean, so a
"refactor" of psi() that quietly changes its scale fails here rather than in the alert.
"""
from __future__ import annotations

import numpy as np
import pytest

from monitoring import drift
from scripts.make_dataset import build


@pytest.fixture(scope="module")
def rng():
    return np.random.default_rng(0)


def test_psi_is_zero_for_identical_samples(rng):
    x = rng.normal(80, 9, 5000)
    assert drift.psi(x, x) == pytest.approx(0.0, abs=1e-9)


def test_psi_noise_stays_under_the_alert_threshold(rng):
    """Two samples from one distribution, the size of one detector window. If this fails,
    the threshold would page someone for sampling noise."""
    worst = max(drift.psi(rng.normal(80, 9, 6000), rng.normal(80, 9, 1800)) for _ in range(50))
    assert worst < drift.PSI_ALERT, f"noise alone reached PSI {worst:.4f}"


def test_psi_fires_on_a_shift_the_threshold_was_set_for(rng):
    ref = rng.normal(80, 9, 6000)
    assert drift.psi(ref, rng.normal(86, 9, 1800)) > drift.PSI_SIGNIFICANT


def test_psi_sees_a_spread_change_that_leaves_the_mean_alone(rng):
    """The scale case from inject_drift.py: a mean-only check would call this healthy."""
    ref = rng.normal(80, 9, 6000)
    cur = rng.normal(80, 9 * 1.5, 1800)
    assert abs(cur.mean() - ref.mean()) < 1.0
    assert drift.psi(ref, cur) > drift.PSI_ALERT


def test_ks_is_zero_for_identical_and_one_for_disjoint(rng):
    x = rng.normal(0, 1, 1000)
    assert drift.ks_statistic(x, x) == 0.0
    assert drift.ks_statistic(x, x + 100) == 1.0


def test_verdict_boundaries():
    assert drift.verdict_for(drift.PSI_ALERT - 1e-6) == "stable"
    assert drift.verdict_for(drift.PSI_ALERT) == "drift"
    assert drift.verdict_for(drift.PSI_SIGNIFICANT) == "significant"


def test_compare_ranks_the_drifted_feature_first(rng):
    import pandas as pd

    ref = pd.DataFrame({"a": rng.normal(0, 1, 3000), "b": rng.normal(0, 1, 3000)})
    cur = pd.DataFrame({"a": rng.normal(0, 1, 1800), "b": rng.normal(1, 1, 1800)})
    results = drift.compare(ref, cur, ["a", "b"])
    assert [r.feature for r in results] == ["b", "a"]
    assert results[0].verdict == "significant"


def test_stream_keeps_the_fleet_and_changes_the_readings():
    """build(seed, reading_seed) is the Lab 4 input stream: same machines, new readings.
    Without reading_seed it must be the training data exactly, or Lab 1's pinned data
    fingerprint breaks."""
    train = build(11)
    assert build(11).equals(train)
    stream = build(11, reading_seed=5)
    assert not stream["temp_c"].equals(train["temp_c"])
    per_machine = train.groupby("machine_id")["temp_c"].mean()
    assert per_machine.corr(stream.groupby("machine_id")["temp_c"].mean()) > 0.9
