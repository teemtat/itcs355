"""Lab 3 Task 3 — where does serialisation start to dominate?

    python scripts/payload_bench.py --model <model.joblib> [--rows 1 10 100 1000 10000]

The deployed schema caps a batch at 100 rows, and at 100 rows the endpoint's time is still
model scoring plus network (reports/lab3-load.md). To find the crossover the request has
to grow past the cap, so this times the service's OWN code path in-process, stage by
stage, for payloads the API would reject:

    decode+validate   JSON bytes -> list[PredictRequest]   (what FastAPI does per request)
    frame             list -> DataFrame[FEATURES]
    score             model.predict_proba
    encode            response model -> JSON bytes

It runs on this machine, not on the Cloud Run instance, so read the SHARES, not the
milliseconds: the ratio of (decode+validate+encode) to score is what carries over.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import joblib
import pandas as pd
from pydantic import TypeAdapter

from service.schemas import BatchResponse, PredictRequest
from src.data import FEATURES

ROWS = TypeAdapter(list[PredictRequest])


def row(i: int) -> dict:
    return {"temp_c": 60 + (i * 7.3) % 35, "vibration_mm_s": 3.1, "pressure_kpa": 315.2,
            "hours_since_service": 4200.0, "load_pct": 20 + (i * 13.1) % 80,
            "ambient_humidity": 55.0}


def time_once(model, body: bytes) -> dict[str, float]:
    t0 = time.perf_counter()
    rows = ROWS.validate_python(json.loads(body)["rows"])
    t1 = time.perf_counter()
    frame = pd.DataFrame([r.model_dump() for r in rows])[FEATURES]
    t2 = time.perf_counter()
    probs = [float(p) for p in model.predict_proba(frame)[:, 1]]
    t3 = time.perf_counter()
    BatchResponse(probabilities=probs, model_version="1").model_dump_json().encode()
    t4 = time.perf_counter()
    return {"decode_validate": t1 - t0, "frame": t2 - t1, "score": t3 - t2, "encode": t4 - t3}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", type=Path, required=True)
    ap.add_argument("--rows", type=int, nargs="+", default=[1, 10, 100, 1000, 5000, 20000])
    ap.add_argument("--repeats", type=int, default=15)
    args = ap.parse_args()

    model = joblib.load(args.model)
    print("| rows | request KB | decode+validate ms | frame ms | score ms | encode ms "
          "| serialisation share |")
    print("|---:|---:|---:|---:|---:|---:|---:|")
    for n in args.rows:
        body = json.dumps({"rows": [row(i) for i in range(n)]}).encode()
        time_once(model, body)  # warm
        runs = [time_once(model, body) for _ in range(args.repeats)]
        med = {k: statistics.median(r[k] for r in runs) * 1000 for k in runs[0]}
        ser = med["decode_validate"] + med["encode"]
        share = ser / sum(med.values())
        print(f"| {n} | {len(body) / 1024:.1f} | {med['decode_validate']:.2f} | "
              f"{med['frame']:.2f} | {med['score']:.2f} | {med['encode']:.2f} | {share:.0%} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
