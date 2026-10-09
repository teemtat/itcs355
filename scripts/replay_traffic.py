"""Lab 4 — the production input stream, with a drift injected part-way through.

    python scripts/replay_traffic.py --normal data/stream/normal.csv \
        --drifted data/stream/drifted.csv --normal-min 40 --drifted-min 30

Sends one /predict per second to the staging endpoint, through the adapter, in random
order across all 240 machines (they all report, so arrivals interleave). After
--normal-min it switches to the drifted file without stopping: that moment is the
injection, logged with a UTC timestamp, and detection time is measured from it.

About 1% of requests are deliberately out of range, so the dashboard's 4xx line shows
something real; the service rejects them with 422 and they never reach the model or
the drift window.

Writes reports/lab4/replay-events.jsonl (start, inject, end) and replay-requests.csv.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from cloudlayer.factory import get_adapter
from src import config, data


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default="itcs355-staging")
    ap.add_argument("--normal", type=Path, required=True)
    ap.add_argument("--drifted", type=Path, required=True)
    ap.add_argument("--normal-min", type=float, default=40)
    ap.add_argument("--drifted-min", type=float, default=30)
    ap.add_argument("--rate", type=float, default=1.0, help="requests per second")
    ap.add_argument("--invalid-share", type=float, default=0.01)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--out-dir", type=Path, default=Path("reports/lab4"))
    args = ap.parse_args()

    rng = np.random.default_rng(args.seed)
    phases = []
    for name, path in (("normal", args.normal), ("drifted", args.drifted)):
        df = pd.read_csv(path)[data.FEATURES]
        phases.append((name, df.iloc[rng.permutation(len(df))].to_dict("records")))

    adapter = get_adapter(config.load())
    url = adapter.endpoint_url(args.endpoint)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    events = (args.out_dir / "replay-events.jsonl").open("a")
    requests = (args.out_dir / "replay-requests.csv").open("a", newline="")
    log = csv.writer(requests)
    if requests.tell() == 0:
        log.writerow(["at", "phase", "kind", "status", "latency_ms", "model_version", "temp_c"])

    def event(kind: str, **fields) -> None:
        rec = {"at": now(), "event": kind, **fields}
        events.write(json.dumps(rec) + "\n")
        events.flush()
        print(json.dumps(rec), flush=True)

    event("start", endpoint=args.endpoint, url=url, rate=args.rate,
          normal_min=args.normal_min, drifted_min=args.drifted_min,
          normal=str(args.normal), drifted=str(args.drifted))
    interval = 1.0 / args.rate
    sent = {"normal": 0, "drifted": 0}
    for (phase, rows), minutes in zip(phases, (args.normal_min, args.drifted_min)):
        ends = time.monotonic() + minutes * 60
        if phase == "drifted":
            event("inject", detail=f"switched to {args.drifted}", after_requests=sent["normal"])
        i = 0
        next_at = time.monotonic()
        while time.monotonic() < ends:
            row = dict(rows[i % len(rows)])
            i += 1
            kind = "valid"
            if rng.random() < args.invalid_share:
                row["load_pct"] = 250.0
                kind = "invalid"
            try:
                r = adapter.invoke(args.endpoint, row, url=url)
            except OSError as exc:  # a dropped connection is a data point, not the end
                r = {"status": 0, "latency_ms": None, "model_version": None, "body": str(exc)}
            sent[phase] += 1
            log.writerow([now(), phase, kind, r["status"], r["latency_ms"],
                          r["model_version"], row["temp_c"]])
            if r["status"] >= 500 or r["status"] == 0:
                print(f"{now()}  {r['status']}  {r['body']}", flush=True)
            if sent[phase] % 300 == 0:
                requests.flush()
                print(f"{now()}  {phase}: {sent[phase]} sent", flush=True)
            next_at += interval
            time.sleep(max(0.0, next_at - time.monotonic()))
    requests.flush()
    event("end", sent=sent)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
