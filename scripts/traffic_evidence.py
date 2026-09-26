"""Lab 3 Task 4 — platform-side evidence that traffic moved.

    python scripts/traffic_evidence.py --summary reports/lab3/canary/run90-summary.json

Reads the canary run's own timeline for the window, then asks the platform (not the
client) how many requests each revision served per minute. Writes a CSV next to the
summary and prints a table with the traffic events marked.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloudlayer.factory import get_adapter
from src import config


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--summary", type=Path, required=True)
    ap.add_argument("--endpoint", default="itcs355-serve")
    args = ap.parse_args()

    tl = json.loads(args.summary.read_text())
    start = datetime.fromisoformat(tl["load_start"]) - timedelta(minutes=1)
    end = datetime.fromisoformat(tl["rollback"]["at"]) + timedelta(minutes=4)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    rows = get_adapter(config.load()).request_counts(
        args.endpoint, start.strftime(fmt), end.strftime(fmt))

    out = args.summary.with_name(args.summary.name.replace("-summary.json", "-platform-counts.csv"))
    with out.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["end", "revision", "requests"])
        w.writeheader()
        w.writerows(rows)

    revs = sorted({r["revision"] for r in rows})
    by_end: dict[str, dict[str, int]] = {}
    for r in rows:
        by_end.setdefault(r["end"], {})[r["revision"]] = r["requests"]
    marks = {tl["shift"]["at"][:16]: "<- split applied", tl["rollback"]["at"][:16]: "<- rollback"}
    print("minute ending (UTC)   " + "  ".join(f"{r[-18:]:>18}" for r in revs))
    for e in sorted(by_end):
        note = next((m for k, m in marks.items() if e[:16] > k and
                     (datetime.fromisoformat(e.replace("Z", "+00:00"))
                      - datetime.fromisoformat(k + ":00+00:00")) <= timedelta(minutes=1)), "")
        print(f"{e:22}" + "  ".join(f"{by_end[e].get(r, 0):>18}" for r in revs) + f"  {note}")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
