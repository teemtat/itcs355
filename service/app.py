"""Lab 3 — inference service.

Provider-neutral by construction: the model arrives through the adapter, and the same
container image deploys to SageMaker, Azure ML, or Vertex AI. Route paths differ per
platform; that difference belongs in cloudlayer/, never here.

Run locally:  uvicorn service.app:app --port 8080
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from service.schemas import BatchRequest, BatchResponse, PredictRequest, PredictResponse

# One JSON object per line. "severity" is the key Cloud Logging reads a level from; the
# other providers' log agents take any JSON line as-is.
logging.basicConfig(
    level=logging.INFO,
    format='{"ts":"%(asctime)s","severity":"%(levelname)s","msg":%(message)s}',
)
log = logging.getLogger("service")


def _log(level: int, **fields: Any) -> None:
    """json.dumps, not string formatting: a request id is caller-supplied, and one quote
    character in it would otherwise turn the line into something no log query can parse."""
    log.log(level, json.dumps(fields, default=str))

STATE: dict[str, Any] = {"model": None, "version": os.environ.get("MODEL_VERSION", "unknown")}


def _load_model():
    """Load once, at startup. Never per request.

    Loading per request is the commonest cause of a p99 that looks nothing like p50, and
    it is the first thing to check when your latency distribution has a long tail.
    """
    name = os.environ.get("MODEL_REGISTRY_NAME")
    version = os.environ.get("MODEL_VERSION")
    uri = os.environ.get("MODEL_URI")
    if uri:
        # Deployed path. deploy() resolved MODEL_REGISTRY_NAME@MODEL_VERSION in the
        # provider's registry to this artifact location; the adapter fetches it, so this
        # file never learns which cloud it is running on.
        import tempfile

        import joblib

        from cloudlayer.factory import get_adapter
        from src import config

        local = os.path.join(tempfile.mkdtemp(prefix="model-"), "model.joblib")
        get_adapter(config.load(strict=False)).download(f"{uri.rstrip('/')}/model.joblib", local)
        return joblib.load(local)

    if name and version:
        import mlflow.sklearn  # imported lazily so tests can run without a registry

        mlflow.set_tracking_uri(os.environ.get("MLFLOW_TRACKING_URI", "sqlite:///mlflow.db"))
        return mlflow.sklearn.load_model(f"models:/{name}/{version}")

    # Fallback for local development and tests only. Submitting this is not acceptable:
    # your deployed service must load a registered version.
    from pathlib import Path

    import joblib

    path = Path(os.environ.get("MODEL_PATH", "reports/model.joblib"))
    if not path.exists():
        raise RuntimeError(
            "No model available. Set MODEL_REGISTRY_NAME and MODEL_VERSION, or MODEL_PATH."
        )
    return joblib.load(path)


@asynccontextmanager
async def lifespan(app: FastAPI):
    started = time.perf_counter()
    try:
        STATE["model"] = _load_model()
        _log(logging.INFO, event="model_loaded", model_version=STATE["version"],
             model_uri=os.environ.get("MODEL_URI"),
             load_ms=round((time.perf_counter() - started) * 1000, 1))
    except Exception as exc:  # readiness stays false; liveness still passes
        STATE["model"] = None
        _log(logging.ERROR, event="model_load_failed", model_version=STATE["version"],
             error=str(exc))
    yield
    STATE["model"] = None


app = FastAPI(title="ITCS355 inference", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def add_request_context(request: Request, call_next):
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    started = time.perf_counter()
    response = await call_next(request)
    latency_ms = (time.perf_counter() - started) * 1000
    score_ms = getattr(request.state, "score_ms", None)
    response.headers["x-request-id"] = request_id
    response.headers["x-model-version"] = str(STATE["version"])
    # Server-Timing splits the server's time into scoring and everything else (parse,
    # validate, serialise). The client's total minus app;dur is network. Task 3 uses the
    # split to find where serialisation starts to dominate.
    timing = f"app;dur={latency_ms:.2f}"
    if score_ms is not None:
        timing += f", score;dur={score_ms:.2f}"
    response.headers["server-timing"] = timing
    _log(logging.INFO, request_id=request_id, path=request.url.path,
         status=response.status_code, latency_ms=round(latency_ms, 2),
         score_ms=None if score_ms is None else round(score_ms, 2),
         rows=getattr(request.state, "rows", None), model_version=str(STATE["version"]))
    return response


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness. The process is up. Says nothing about whether it can serve."""
    return {"status": "alive"}


@app.get("/ready")
def ready():
    """Readiness. The model is loaded and can score.

    These two are genuinely different, and confusing them causes a specific production
    failure: traffic routed to a container whose model has not finished loading. All three
    providers distinguish them, and Quiz 3 asks about it.
    """
    if STATE["model"] is None:
        return JSONResponse(status_code=503, content={"status": "not_ready", "reason": "model not loaded"})
    return {"status": "ready", "model_version": STATE["version"]}


def _score(request: Request, rows: list[dict]) -> list[float]:
    if STATE["model"] is None:
        raise HTTPException(status_code=503, detail="model not loaded")
    import pandas as pd

    from src.data import FEATURES

    started = time.perf_counter()
    frame = pd.DataFrame(rows)[FEATURES]
    scores = [float(p) for p in STATE["model"].predict_proba(frame)[:, 1]]
    request.state.score_ms = (time.perf_counter() - started) * 1000
    request.state.rows = len(rows)
    return scores


@app.post("/predict", response_model=PredictResponse)
def predict(payload: PredictRequest, request: Request) -> PredictResponse:
    score = _score(request, [payload.model_dump()])[0]
    return PredictResponse(probability=score, model_version=str(STATE["version"]))


@app.post("/predict/batch", response_model=BatchResponse)
def predict_batch(payload: BatchRequest, request: Request) -> BatchResponse:
    scores = _score(request, [row.model_dump() for row in payload.rows])
    return BatchResponse(probabilities=scores, model_version=str(STATE["version"]))
