"""Lab 4 — model behaviour tests.

These assert what the model DOES, independent of its metric. A model can score 0.85 and
still be broken in ways these catch, and unlike a metric threshold they do not drift out
of relevance as the data changes.

Quiz 4 asks you to distinguish these from data contract tests. The difference: a contract
test fails when an upstream producer changes something; a behaviour test fails when the
model changes.
"""
from __future__ import annotations

import time

import numpy as np
import pandas as pd
import pytest
from sklearn.ensemble import RandomForestClassifier

from src import config, data, seeds

RAW = config.REPO_ROOT / "data" / "raw" / "sensors.csv"
# The p95 target in loadtest/k6.js is 200 ms at the client with 10 users. In Lab 3 that
# came out at 179 ms on 2 vCPU: ~100 ms of network, and most of the rest waiting behind
# other requests. Scoring one row cost 6.4 ms there, and the wait grows with it, so a model
# that scores much slower cannot meet the target. One row takes ~4 ms here (laptop); 25 ms
# is about six times that, enough headroom for a slower CI runner, and low enough to stop a
# model that got several times bigger before it reaches the endpoint. A tripwire, not a
# p95 forecast: the load test is still what proves the target.
LATENCY_BUDGET_MS = 25.0


@pytest.fixture(scope="module")
def fitted():
    if not RAW.exists():
        pytest.skip("run `make data` first")
    seed = seeds.set_all()
    df = data.load_raw(RAW)
    train_df, _, test_df = data.split(df, seed=seed)
    # The production configuration, as src/train.py builds it: the model these tests
    # describe should be the one that serves, not a cheaper stand-in.
    model = RandomForestClassifier(n_estimators=200, max_depth=8, min_samples_leaf=5,
                                   random_state=seed, n_jobs=-1)
    model.fit(train_df[data.FEATURES], train_df[data.TARGET])
    model.set_params(n_jobs=1)
    return model, test_df


def test_predictions_are_valid_probabilities(fitted):
    model, test_df = fitted
    proba = model.predict_proba(test_df[data.FEATURES])[:, 1]
    assert np.all((proba >= 0) & (proba <= 1))
    assert not np.isnan(proba).any()


def test_known_healthy_machine_scores_low(fitted):
    """A cool, new, lightly loaded machine must not be flagged as about to fail.

    This is a domain assertion. If it fails, the model has learned something backwards,
    and no aggregate metric would have told you.
    """
    model, _ = fitted
    healthy = pd.DataFrame([{
        "temp_c": 66.0, "vibration_mm_s": 1.4, "pressure_kpa": 330.0,
        "hours_since_service": 50.0, "load_pct": 30.0, "ambient_humidity": 50.0,
    }])[data.FEATURES]
    assert model.predict_proba(healthy)[0, 1] < 0.5


def test_risk_increases_with_wear(fitted):
    """Monotonicity the domain requires: more hours since service, higher risk.

    Tree ensembles are not monotonic by construction, so this can genuinely fail. If it
    does, that is information — not a reason to delete the test.
    """
    model, _ = fitted
    base = {
        "temp_c": 82.0, "vibration_mm_s": 4.0, "pressure_kpa": 310.0,
        "load_pct": 75.0, "ambient_humidity": 60.0,
    }
    rows = pd.DataFrame([{**base, "hours_since_service": h} for h in (100, 3000, 8500)])[data.FEATURES]
    proba = model.predict_proba(rows)[:, 1]
    assert proba[0] <= proba[-1], f"risk fell as wear rose: {proba}"


def test_risk_rises_with_temperature(fitted):
    """Directional: the same readings, 10 °C hotter, must look riskier on average. Across
    six seeds the rise was +0.030 to +0.044, so +0.01 fails only if the model has stopped
    using temperature, or learned it backwards."""
    model, test_df = fitted
    rows = test_df[data.FEATURES]
    hotter = rows.assign(temp_c=rows["temp_c"] + 10)
    rise = model.predict_proba(hotter)[:, 1].mean() - model.predict_proba(rows)[:, 1].mean()
    assert rise > 0.01, f"10 °C hotter raised mean risk by only {rise:.4f}"


def test_humidity_does_not_drive_risk(fitted):
    """Invariance: ambient humidity has no part in how failures happen here. Moving every
    row from 30% to 85% humidity changed mean risk by at most 0.017 across six seeds; past
    0.03 (a quarter of the 0.12 base rate) the model is leaning on a feature that only
    correlates by chance, and will break when the weather does."""
    model, test_df = fitted
    rows = test_df[data.FEATURES]
    dry = model.predict_proba(rows.assign(ambient_humidity=30.0))[:, 1].mean()
    wet = model.predict_proba(rows.assign(ambient_humidity=85.0))[:, 1].mean()
    assert abs(wet - dry) < 0.03, f"humidity moved mean risk by {wet - dry:+.4f}"


def test_prediction_latency_within_budget(fitted):
    model, test_df = fitted
    sample = test_df[data.FEATURES].head(100)
    model.predict_proba(sample)  # warm up; the first call includes lazy setup
    started = time.perf_counter()
    for _ in range(20):
        model.predict_proba(sample.head(1))
    elapsed_ms = (time.perf_counter() - started) * 1000 / 20
    assert elapsed_ms < LATENCY_BUDGET_MS, f"single prediction took {elapsed_ms:.2f} ms"


def test_model_is_not_constant(fitted):
    """A model that always predicts the majority class can post a respectable accuracy
    and is useless. Catch it here rather than in production."""
    model, test_df = fitted
    proba = model.predict_proba(test_df[data.FEATURES])[:, 1]
    assert proba.std() > 0.01, "predictions have almost no variance"
