"""Lab 4 — integration test against a running container.

    SERVICE_URL=http://localhost:8080 EXPECTED_MODEL_VERSION=<sha> pytest tests/test_integration.py

CI builds the serving image, starts it, and runs this. Skipped when SERVICE_URL is unset,
so a plain `pytest` on a laptop does not need Docker.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

import pytest

URL = os.environ.get("SERVICE_URL", "").rstrip("/")
VERSION = os.environ.get("EXPECTED_MODEL_VERSION")
pytestmark = pytest.mark.skipif(not URL, reason="SERVICE_URL not set — no container to test")

VALID = {"temp_c": 78.4, "vibration_mm_s": 3.1, "pressure_kpa": 315.2,
         "hours_since_service": 4200, "load_pct": 68, "ambient_humidity": 55}


def call(path: str, payload: dict | None = None):
    req = urllib.request.Request(
        URL + path, method="POST" if payload is not None else "GET",
        data=None if payload is None else json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), json.loads(e.read())


def test_ready_reports_the_expected_version():
    status, _, body = call("/ready")
    assert status == 200 and body["status"] == "ready"
    if VERSION:
        assert body["model_version"] == VERSION


def test_predict_response_shape_and_version():
    status, headers, body = call("/predict", VALID)
    assert status == 200
    assert set(body) == {"probability", "model_version"}, body
    assert isinstance(body["probability"], float) and 0.0 <= body["probability"] <= 1.0
    assert headers.get("x-model-version") == body["model_version"]
    if VERSION:
        assert body["model_version"] == VERSION


def test_batch_returns_one_probability_per_row():
    status, _, body = call("/predict/batch", {"rows": [VALID, {**VALID, "temp_c": 95.0}]})
    assert status == 200 and len(body["probabilities"]) == 2


def test_container_rejects_out_of_range_input():
    status, _, body = call("/predict", {**VALID, "load_pct": 250})
    assert status == 422 and "load_pct" in json.dumps(body)
