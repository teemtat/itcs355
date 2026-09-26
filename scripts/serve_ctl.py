"""Lab 3 — drive the serving endpoint through the adapter.

    python scripts/serve_ctl.py deploy --version 1 --instance 1cpu-2gi
    python scripts/serve_ctl.py deploy --version 2 --canary          # no traffic yet
    python scripts/serve_ctl.py smoke
    python scripts/serve_ctl.py traffic <rev>=90 <rev>=10
    python scripts/serve_ctl.py revisions
    python scripts/serve_ctl.py url | token

Everything provider-shaped happens inside cloudlayer/. Every action that changes the
endpoint is appended to reports/lab3/events.jsonl with a UTC timestamp, because "I
rolled back" is a claim and the event log is the evidence.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloudlayer.factory import get_adapter
from src import config

EVENTS = Path("reports/lab3/events.jsonl")
IMAGE_FILE = Path("reports/lab3/serve-image.txt")

# Three known payloads: a typical reading, a batch, and one the schema must reject.
SMOKE = [
    ("single", {"temp_c": 78.4, "vibration_mm_s": 3.1, "pressure_kpa": 315.2,
                "hours_since_service": 4200, "load_pct": 68.0, "ambient_humidity": 55.0}, 200),
    ("batch", {"rows": [
        {"temp_c": 70.0, "vibration_mm_s": 2.0, "pressure_kpa": 300.0,
         "hours_since_service": 500, "load_pct": 40.0, "ambient_humidity": 45.0},
        {"temp_c": 98.0, "vibration_mm_s": 7.5, "pressure_kpa": 340.0,
         "hours_since_service": 8800, "load_pct": 97.0, "ambient_humidity": 80.0},
    ]}, 200),
    ("out-of-range", {"temp_c": 78.4, "vibration_mm_s": 3.1, "pressure_kpa": 315.2,
                      "hours_since_service": 4200, "load_pct": 250.0,
                      "ambient_humidity": 55.0}, 422),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _event(kind: str, **fields) -> dict:
    EVENTS.parent.mkdir(parents=True, exist_ok=True)
    rec = {"at": _now(), "event": kind, **fields}
    with EVENTS.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default="itcs355-serve")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("deploy")
    d.add_argument("--version", required=True, help="model registry version")
    d.add_argument("--instance", default="1cpu-2gi")
    d.add_argument("--image", default=None, help="digest ref; default reports/lab3/serve-image.txt")
    d.add_argument("--canary", action="store_true", help="deploy with no traffic")
    d.add_argument("--tag", default=None)
    d.add_argument("--min-instances", type=int, default=1)
    d.add_argument("--max-instances", type=int, default=1)

    sub.add_parser("smoke")
    t = sub.add_parser("traffic")
    t.add_argument("split", nargs="+", help="revision=percent")
    t.add_argument("--reason", default="")
    sub.add_parser("revisions")
    u = sub.add_parser("url")
    u.add_argument("--tag", default=None)
    sub.add_parser("token")
    args = ap.parse_args()

    cfg = config.load()
    adapter = get_adapter(cfg)

    if args.cmd == "deploy":
        image = args.image or IMAGE_FILE.read_text().strip()
        ref = f"{cfg.model_registry_name}@{args.version}"
        started = _now()
        rev = adapter.deploy(ref, args.endpoint, args.instance, image=image,
                             traffic=not args.canary, tag=args.tag,
                             min_instances=args.min_instances, max_instances=args.max_instances)
        print(rev)
        _event("deploy", started=started, endpoint=args.endpoint, revision=rev, model_ref=ref,
               instance=args.instance, image=image, canary=args.canary,
               min_instances=args.min_instances, max_instances=args.max_instances,
               traffic=adapter.traffic(args.endpoint))
        return 0

    if args.cmd == "smoke":
        url = adapter.endpoint_url(args.endpoint)
        failed = 0
        results = []
        for name, payload, expect in SMOKE:
            r = adapter.invoke(args.endpoint, payload, url=url)
            ok = r["status"] == expect and (expect != 200 or r["body"].get("model_version"))
            failed += not ok
            results.append({"case": name, "expect": expect, **r})
            print(f"{'PASS' if ok else 'FAIL'}  {name:13} {r['status']}  "
                  f"version={r['model_version']}  {r['latency_ms']:.0f} ms  "
                  f"{json.dumps(r['body'])[:110]}")
        _event("smoke", endpoint=args.endpoint, url=url, passed=not failed, results=results)
        return 1 if failed else 0

    if args.cmd == "traffic":
        split = {k: int(v) for k, v in (s.split("=", 1) for s in args.split)}
        rec = adapter.set_traffic(args.endpoint, split)
        _event("traffic", endpoint=args.endpoint, reason=args.reason, **rec)
        print(json.dumps(rec, indent=2))
        return 0

    if args.cmd == "revisions":
        print(json.dumps(adapter.traffic(args.endpoint), indent=2))
        return 0

    if args.cmd == "url":
        print(adapter.endpoint_url(args.endpoint, tag=args.tag))
        return 0

    if args.cmd == "token":
        print(adapter.auth_header()["Authorization"].removeprefix("Bearer "))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
