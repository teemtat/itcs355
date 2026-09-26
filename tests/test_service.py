"""Lab 3/4 — service contract tests."""
from __future__ import annotations

import os

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from src import config, data, seeds  # noqa: E402

RAW = config.REPO_ROOT / "data" / "raw" / "sensors.csv"
VALID = {
    "temp_c": 78.4, "vibration_mm_s": 3.1, "pressure_kpa": 315.2,
    "hours_since_service": 4200.0, "load_pct": 68.0, "ambient_humidity": 55.0,
}


@pytest.fixture(scope="module")
def client():
    if not RAW.exists():
        pytest.skip("run `make data` first")
    model_path = config.REPO_ROOT / "reports" / "model.joblib"
    if not model_path.exists():
        import joblib
        from sklearn.ensemble import RandomForestClassifier

        seed = seeds.set_all()
        df = data.load_raw(RAW)
        train_df, _, _ = data.split(df, seed=seed)
        model = RandomForestClassifier(n_estimators=60, max_depth=8, random_state=seed)
        model.fit(train_df[data.FEATURES], train_df[data.TARGET])
        model_path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(model, model_path)

    os.environ["MODEL_PATH"] = str(model_path)
    os.environ["MODEL_VERSION"] = "test-1"
    os.environ.pop("MODEL_REGISTRY_NAME", None)

    from service.app import app
    with TestClient(app) as c:
        yield c


def test_health_and_ready_are_different(client):
    """Liveness passes whenever the process is up. Readiness only when it can score."""
    assert client.get("/health").json()["status"] == "alive"
    ready = client.get("/ready")
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"


def test_predict_returns_probability_and_version(client):
    r = client.post("/predict", json=VALID)
    assert r.status_code == 200
    body = r.json()
    assert 0.0 <= body["probability"] <= 1.0
    assert body["model_version"] == "test-1"
    assert r.headers["x-model-version"] == "test-1"
    assert r.headers["x-request-id"]


def test_out_of_range_input_is_rejected(client):
    bad = {**VALID, "load_pct": 250.0}
    assert client.post("/predict", json=bad).status_code == 422


def test_unknown_field_is_rejected(client):
    """extra='forbid'. A silently ignored field is how a caller ends up sending a feature
    you never read while believing it matters."""
    assert client.post("/predict", json={**VALID, "surprise": 1}).status_code == 422


def test_missing_field_is_rejected(client):
    incomplete = {k: v for k, v in VALID.items() if k != "temp_c"}
    assert client.post("/predict", json=incomplete).status_code == 422


def test_batch_matches_singles(client):
    rows = [VALID, {**VALID, "temp_c": 92.0}]
    batch = client.post("/predict/batch", json={"rows": rows}).json()["probabilities"]
    singles = [client.post("/predict", json=r).json()["probability"] for r in rows]
    assert batch == pytest.approx(singles, abs=1e-9)


def test_batch_size_limit_enforced(client):
    r = client.post("/predict/batch", json={"rows": [VALID] * 101})
    assert r.status_code == 422


def test_ready_is_503_while_model_is_not_loaded(client):
    """The exact window the lab's sequence diagram warns about: alive, but cannot score."""
    from service.app import STATE

    model, STATE["model"] = STATE["model"], None
    try:
        assert client.get("/health").status_code == 200
        r = client.get("/ready")
        assert r.status_code == 503
        assert r.json()["status"] == "not_ready"
        assert client.post("/predict", json=VALID).status_code == 503
    finally:
        STATE["model"] = model


def test_malformed_input_says_which_field(client):
    r = client.post("/predict", json={**VALID, "load_pct": 250.0})
    assert r.status_code == 422
    assert "load_pct" in str(r.json()["detail"])


def test_server_timing_separates_scoring(client):
    timing = client.post("/predict", json=VALID).headers["server-timing"]
    assert "app;dur=" in timing and "score;dur=" in timing


def test_request_log_is_one_json_object(client, caplog):
    import json
    import logging

    with caplog.at_level(logging.INFO, logger="service"):
        client.post("/predict", json=VALID, headers={"x-request-id": 'abc"quote'})
    lines = [json.loads(r.getMessage()) for r in caplog.records if r.name == "service"]
    hit = [ln for ln in lines if ln.get("request_id") == 'abc"quote']
    assert hit and hit[0]["model_version"] == "test-1"
    assert hit[0]["latency_ms"] >= 0 and hit[0]["path"] == "/predict"
