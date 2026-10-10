"""Lab 3 Task 4 — canary, blind detection, rollback.

    python scripts/canary_watch.py --stable <rev-v1> --canary <rev-v2> --split 90 --tag run90

One run, end to end:
  1. baseline   all traffic on --stable; learn what normal looks like
  2. shift      set_traffic(stable=split, canary=100-split); start the clock
  3. watch      every 10 s, test the last WINDOW seconds against the baseline
  4. alert      -> set_traffic(stable=100) immediately, i.e. automatic rollback
  5. after      keep sending for --after-s so the evidence shows traffic leaving

THE DETECTOR IS BLIND. It sees only what a monitoring system sees for the endpoint as a
whole — the stream of predicted probabilities (and, as a secondary, delayed-label metric,
log loss). The version header on every response is written to the request log as
EVIDENCE that traffic moved, and nothing in the detection path reads it.

Detection rule: z = (mean p over the last WINDOW s - baseline mean) / (baseline sd / sqrt n).
Alert when z > Z for two consecutive checks. The baseline max z is reported, so the
false-alarm margin is on record rather than assumed.

The replayed inputs come from the held-out test split throughout, so the input
distribution is constant by construction; a shift in the OUTPUT distribution can only
come from the model. In production you would check input drift (Lab 4) before blaming
the model for a moved prediction mean.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests

from cloudlayer.factory import get_adapter
from src import config, data, seeds

EVENTS = Path("reports/lab3/events.jsonl")
OUT = Path("reports/lab3/canary")


def utc(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time(), timezone.utc) \
        .isoformat(timespec="milliseconds")


def event(kind: str, **fields) -> None:
    EVENTS.parent.mkdir(parents=True, exist_ok=True)
    with EVENTS.open("a") as fh:
        fh.write(json.dumps({"at": utc(), "event": kind, **fields}) + "\n")


class Load:
    """N threads replaying labelled test rows at the endpoint, keep-alive per thread."""

    def __init__(self, url: str, token: str, rows: list[dict], labels: list[int], workers: int):
        self.url, self.token, self.rows, self.labels = url + "/predict", token, rows, labels
        self.records: list[tuple] = []
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.threads = [threading.Thread(target=self._run, args=(i,), daemon=True)
                        for i in range(workers)]

    def _run(self, i: int) -> None:
        rng = random.Random(i)
        s = requests.Session()
        s.headers.update({"Authorization": f"Bearer {self.token}"})
        while not self.stop.is_set():
            j = rng.randrange(len(self.rows))
            t0 = time.time()
            try:
                r = s.post(self.url, json=self.rows[j], timeout=30)
                status = r.status_code
                prob = r.json().get("probability") if status == 200 else None
                version = r.headers.get("x-model-version")
            except requests.RequestException:
                status, prob, version = 0, None, None
            rec = (t0, (time.time() - t0) * 1000, status, prob, self.labels[j], version)
            with self.lock:
                self.records.append(rec)

    def start(self) -> None:
        for t in self.threads:
            t.start()

    def since(self, t0: float) -> list[tuple]:
        with self.lock:
            return [r for r in self.records if r[0] >= t0]


def window_stats(recs: list[tuple]) -> dict:
    ok = [r for r in recs if r[2] == 200 and r[3] is not None]
    n = len(ok)
    if not n:
        return {"n": 0}
    ps = [r[3] for r in ok]
    ll = [-(y * math.log(max(p, 1e-6)) + (1 - y) * math.log(max(1 - p, 1e-6)))
          for (_, _, _, p, y, _) in ok]
    lat = sorted(r[1] for r in recs)
    return {"n": n, "mean_p": sum(ps) / n, "log_loss": sum(ll) / n,
            "error_rate": 1 - n / len(recs), "p95_ms": lat[int(0.95 * (len(lat) - 1))]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default="itcs355-serve")
    ap.add_argument("--stable", required=True, help="revision serving the good version")
    ap.add_argument("--canary", required=True, help="revision serving the candidate")
    ap.add_argument("--split", type=int, default=90, help="percent kept on stable")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--baseline-s", type=int, default=180)
    ap.add_argument("--window-s", type=int, default=120)
    ap.add_argument("--z", type=float, default=4.0)
    ap.add_argument("--max-watch-s", type=int, default=900)
    ap.add_argument("--after-s", type=int, default=120)
    ap.add_argument("--no-rollback", action="store_true")
    ap.add_argument("--canary-version", default="2",
                    help="registry version on --canary; used ONLY for the evidence table")
    args = ap.parse_args()

    cfg = config.load()
    adapter = get_adapter(cfg)
    seed = seeds.set_all()
    _, _, test_df = data.split(data.load_raw(cfg.raw_path), seed=seed)
    rows = test_df[data.FEATURES].astype(float).to_dict("records")
    labels = test_df[data.TARGET].astype(int).tolist()

    OUT.mkdir(parents=True, exist_ok=True)
    timeline: dict = {"tag": args.tag, "stable": args.stable, "canary": args.canary,
                      "split": f"{args.split}/{100 - args.split}", "window_s": args.window_s,
                      "z_threshold": args.z}

    before = adapter.set_traffic(args.endpoint, {args.stable: 100})
    event("traffic", endpoint=args.endpoint, reason=f"{args.tag}: baseline, all on stable", **before)

    url = adapter.endpoint_url(args.endpoint)
    load = Load(url, adapter.auth_header()["Authorization"].split()[1], rows, labels, args.workers)
    t_start = time.time()
    load.start()
    timeline["load_start"] = utc(t_start)
    print(f"[{utc()}] baseline {args.baseline_s}s on {args.stable}")
    time.sleep(args.baseline_s)

    base = [r for r in load.since(t_start + 20) if r[2] == 200]  # skip warm-up
    ps = [r[3] for r in base]
    mu = sum(ps) / len(ps)
    sd = math.sqrt(sum((p - mu) ** 2 for p in ps) / (len(ps) - 1))
    base_ll = window_stats(base)["log_loss"]
    timeline["baseline"] = {"n": len(ps), "mean_p": mu, "sd_p": sd, "log_loss": base_ll}
    print(f"  baseline n={len(ps)} mean_p={mu:.4f} sd={sd:.4f} log_loss={base_ll:.4f}")

    windows: list[dict] = []

    def check(now: float, phase: str) -> dict:
        w = window_stats(load.since(now - args.window_s))
        z = (w["mean_p"] - mu) / (sd / math.sqrt(w["n"])) if w.get("n") else float("nan")
        row = {"at": utc(now), "t_s": round(now - t_start, 1), "phase": phase, "z": round(z, 2),
               **{k: round(v, 5) if isinstance(v, float) else v for k, v in w.items()}}
        windows.append(row)
        return row

    # False-alarm margin: the same test, run over the baseline itself.
    base_z = []
    for off in range(args.window_s, args.baseline_s + 1, 10):
        w = window_stats([r for r in load.records if t_start <= r[0] < t_start + off
                          and r[0] >= t_start + off - args.window_s])
        if w.get("n"):
            base_z.append((w["mean_p"] - mu) / (sd / math.sqrt(w["n"])))
    timeline["baseline_max_abs_z"] = round(max(abs(z) for z in base_z), 2) if base_z else None

    shift = adapter.set_traffic(args.endpoint, {args.stable: args.split,
                                                args.canary: 100 - args.split})
    event("traffic", endpoint=args.endpoint, reason=f"{args.tag}: canary starts", **shift)
    t_shift = time.time()
    timeline["shift"] = shift
    print(f"[{shift['at']}] traffic {args.split}/{100 - args.split} -> watching")

    detected, over = None, 0
    while time.time() - t_shift < args.max_watch_s:
        time.sleep(10)
        row = check(time.time(), "canary")
        print(f"  t+{time.time() - t_shift:5.0f}s  n={row.get('n')}  mean_p={row.get('mean_p')}"
              f"  z={row['z']}  log_loss={row.get('log_loss')}")
        over = over + 1 if row["z"] > args.z else 0
        if over >= 2:
            detected = time.time()
            break

    timeline["detected"] = utc(detected) if detected else None
    timeline["detection_s"] = round(detected - t_shift, 1) if detected else None
    if not args.no_rollback:
        back = adapter.set_traffic(args.endpoint, {args.stable: 100})
        reason = "automatic rollback on alert" if detected else "no alert within max watch; rolled back"
        event("traffic", endpoint=args.endpoint, reason=f"{args.tag}: {reason}", **back)
        timeline["rollback"] = back
        timeline["rollback_s_after_detection"] = (
            round(time.time() - detected, 1) if detected else None)
        print(f"[{back['at']}] rolled back -> {args.stable}=100")

    t_back = time.time()
    while time.time() - t_back < args.after_s:
        time.sleep(10)
        check(time.time(), "after-rollback")
    load.stop.set()
    time.sleep(2)

    # Evidence only, computed after the fact: what the SERVICE said it was.
    recs = load.records
    with (OUT / f"{args.tag}-requests.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["sent_utc", "latency_ms", "status", "probability", "label", "model_version"])
        for r in recs:
            w.writerow([utc(r[0]), round(r[1], 2), r[2], r[3], r[4], r[5]])
    per10: dict[int, dict] = {}
    for r in recs:
        b = int((r[0] - t_start) // 10) * 10
        per10.setdefault(b, {}).setdefault(r[5] or "error", 0)
        per10[b][r[5] or "error"] += 1
    with (OUT / f"{args.tag}-versions-per-10s.csv").open("w", newline="") as fh:
        vs = sorted({v for d in per10.values() for v in d})
        w = csv.writer(fh)
        w.writerow(["bucket_start_utc", "t_s"] + [f"version_{v}" for v in vs] + ["share_canary"])
        canary_v = args.canary_version
        for b in sorted(per10):
            d = per10[b]
            tot = sum(d.values())
            w.writerow([utc(t_start + b), b] + [d.get(v, 0) for v in vs]
                       + [round(d.get(canary_v, 0) / tot, 3)])
    with (OUT / f"{args.tag}-windows.csv").open("w", newline="") as fh:
        keys = ["at", "t_s", "phase", "n", "mean_p", "z", "log_loss", "error_rate", "p95_ms"]
        w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(windows)
    timeline["requests"] = len(recs)
    timeline["errors"] = sum(1 for r in recs if r[2] != 200)
    (OUT / f"{args.tag}-summary.json").write_text(json.dumps(timeline, indent=2) + "\n")
    print(json.dumps({k: timeline[k] for k in ("detection_s", "baseline_max_abs_z",
                                                "requests", "errors")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
