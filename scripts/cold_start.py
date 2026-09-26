"""Lab 3 Task 3 — cold-start latency, reported separately from the load test.

    python scripts/serve_ctl.py deploy --version 1 --instance 2cpu-4gi --canary \
        --tag cold --min-instances 0
    python scripts/cold_start.py --tag cold --samples 3 --idle-min 18

The load-tested revisions keep one warm instance (min 1), so their p99 contains no cold
start. This measures what a scale-to-zero configuration would add: a revision with
min 0 and no traffic, left idle long enough for the platform to reclaim its instance
(Cloud Run keeps an idle instance for up to 15 minutes), then hit once.

Each sample is checked against the service's own log: a cold start is only counted if a
`model_loaded` line for that revision appears after the request was sent. A fast answer
without one means an instance survived the idle period, and is reported as warm.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloudlayer.factory import get_adapter
from src import config

OUT = Path("reports/lab3/cold-start.json")
PAYLOAD = {"temp_c": 78.4, "vibration_mm_s": 3.1, "pressure_kpa": 315.2,
           "hours_since_service": 4200, "load_pct": 68.0, "ambient_humidity": 55.0}


def model_loaded_after(cfg, endpoint: str, since: str) -> list[dict]:
    """The service's own startup record, from the platform log."""
    flt = (f'resource.type="cloud_run_revision" AND resource.labels.service_name="{endpoint}" '
           f'AND jsonPayload.msg.event="model_loaded" AND timestamp>="{since}"')
    raw = subprocess.run(["gcloud", "logging", "read", flt, f"--project={cfg.project_id}",
                          "--format=json", "--limit=10"], capture_output=True, text=True).stdout
    return [{"at": e["timestamp"], "revision": e["resource"]["labels"]["revision_name"],
             "load_ms": e["jsonPayload"]["msg"].get("load_ms")} for e in json.loads(raw or "[]")]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default="itcs355-serve")
    ap.add_argument("--tag", default="cold")
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--idle-min", type=float, default=18)
    ap.add_argument("--warm-follow-ups", type=int, default=5)
    args = ap.parse_args()

    cfg = config.load()
    adapter = get_adapter(cfg)
    url = adapter.endpoint_url(args.endpoint, tag=args.tag)
    results = json.loads(OUT.read_text()) if OUT.exists() else []

    for i in range(args.samples):
        print(f"[{datetime.now(timezone.utc):%H:%M:%S}] idle {args.idle_min} min before sample {i + 1}")
        time.sleep(args.idle_min * 60)
        sent = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        first = adapter.invoke(args.endpoint, PAYLOAD, url=url)
        warm = [adapter.invoke(args.endpoint, PAYLOAD, url=url)["latency_ms"]
                for _ in range(args.warm_follow_ups)]
        time.sleep(90)  # log ingestion lag
        loaded = model_loaded_after(cfg, args.endpoint, sent)
        rec = {"sent": sent, "status": first["status"], "first_request_ms": first["latency_ms"],
               "warm_follow_up_ms": warm, "cold_confirmed_by_log": bool(loaded),
               "model_loaded": loaded, "idle_min": args.idle_min}
        results.append(rec)
        OUT.write_text(json.dumps(results, indent=2) + "\n")
        print(json.dumps(rec))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
