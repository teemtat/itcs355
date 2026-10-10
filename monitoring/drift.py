"""Lab 4 — drift detection.

PSI and Kolmogorov–Smirnov, implemented directly so you can see what the numbers mean.
Evidently is allowed instead, but you must still be able to explain what your chosen
statistic measures and why your threshold is what it is.

    python -m monitoring.drift --current data/current.csv                 # a file
    python -m monitoring.drift --endpoint itcs355-staging --emit          # production

The second form is what the scheduled job runs: the last WINDOW_MINUTES of inputs the
endpoint actually scored, read back from its request logs, compared with the training
data, and written as metrics. The alert itself is a Cloud Monitoring policy on those
metrics, with the same threshold as PSI_ALERT below.

A threshold copied from a tutorial is not a justified threshold, and Lab 4 grades the
justification, not the code.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

# The threshold, and why (numbers from scripts/drift_calibrate.py, in the README):
#
# 0.10 / 0.25 is the credit-scoring convention, for stable features and huge volumes. On
# this data, with a 30-minute window of ~1,800 readings from the same 240 machines, PSI
# with nothing changed never went above 0.023 (500 windows, worst of six features). A
# +6 °C temperature offset reads 0.39, but +4 °C only 0.18 and +3 °C 0.10: at 0.25 a
# 4-degree calibration fault would never be seen, while the model kept flagging ~14% more
# machines than it should. 0.05 is twice the worst noise I measured and corresponds to a
# ~+2 °C offset, about the standard tolerance of an ordinary industrial thermocouple.
# Below that I could not tell a fault from a normal sensor anyway.
PSI_ALERT = 0.05
PSI_SIGNIFICANT = 0.25

# Smaller windows are noisier: the worst no-change PSI was 0.038 at 900 readings and 0.108
# at 300. Under 900, a half-empty window after a restart or a quiet night would page
# someone for nothing, so the check skips instead.
MIN_WINDOW_ROWS = 900
WINDOW_MINUTES = 30


@dataclass
class FeatureDrift:
    feature: str
    psi: float
    ks_statistic: float
    ref_mean: float
    cur_mean: float
    verdict: str


def psi(reference: np.ndarray, current: np.ndarray, bins: int = 10) -> float:
    """Population Stability Index.

    Bins the reference into quantiles and compares the proportion of mass falling in each.
    Sensitive to changes in shape, not only in mean — which is why a feature can drift
    badly while its average looks untouched.
    """
    edges = np.quantile(reference, np.linspace(0, 1, bins + 1))
    edges[0], edges[-1] = -np.inf, np.inf
    edges = np.unique(edges)
    if len(edges) < 3:
        return 0.0

    ref_counts, _ = np.histogram(reference, bins=edges)
    cur_counts, _ = np.histogram(current, bins=edges)

    # Laplace smoothing: an empty bin would otherwise make the log term infinite.
    ref_prop = (ref_counts + 1) / (ref_counts.sum() + len(ref_counts))
    cur_prop = (cur_counts + 1) / (cur_counts.sum() + len(cur_counts))

    return float(np.sum((cur_prop - ref_prop) * np.log(cur_prop / ref_prop)))


def ks_statistic(reference: np.ndarray, current: np.ndarray) -> float:
    """Two-sample Kolmogorov–Smirnov statistic: the largest gap between the two CDFs.

    Complements PSI. KS is more sensitive to a shift in location; PSI to a change in shape.
    Reporting both, and noticing when they disagree, is worth more than either alone.
    """
    ref = np.sort(reference)
    cur = np.sort(current)
    pooled = np.concatenate([ref, cur])
    cdf_ref = np.searchsorted(ref, pooled, side="right") / len(ref)
    cdf_cur = np.searchsorted(cur, pooled, side="right") / len(cur)
    return float(np.max(np.abs(cdf_ref - cdf_cur)))


def verdict_for(score: float) -> str:
    if score < PSI_ALERT:
        return "stable"
    if score < PSI_SIGNIFICANT:
        return "drift"
    return "significant"


def compare(reference: pd.DataFrame, current: pd.DataFrame, features: list[str]) -> list[FeatureDrift]:
    results = []
    for feature in features:
        ref = reference[feature].to_numpy(dtype=float)
        cur = current[feature].to_numpy(dtype=float)
        score = psi(ref, cur)
        results.append(FeatureDrift(
            feature=feature,
            psi=round(score, 5),
            ks_statistic=round(ks_statistic(ref, cur), 5),
            ref_mean=round(float(ref.mean()), 4),
            cur_mean=round(float(cur.mean()), 4),
            verdict=verdict_for(score),
        ))
    return sorted(results, key=lambda r: r.psi, reverse=True)


def _reference(path_or_key: str, cfg) -> pd.DataFrame:
    """A local path, or a key under BLOB_URI fetched through the adapter (the scheduled
    job has no repo checkout)."""
    local = Path(path_or_key)
    if local.exists():
        return pd.read_csv(local)
    import tempfile

    from cloudlayer.factory import get_adapter
    dest = Path(tempfile.mkdtemp()) / "reference.csv"
    get_adapter(cfg).download(f"{cfg.blob_uri.rstrip('/')}/{path_or_key.lstrip('/')}", str(dest))
    return pd.read_csv(dest)


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from src import config, data

    ap = argparse.ArgumentParser()
    ap.add_argument("--reference", default="data/raw/sensors.csv",
                    help="local path, or a key under BLOB_URI")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--current", type=Path, help="a CSV of current inputs")
    src.add_argument("--endpoint", help="read the inputs this endpoint scored recently")
    ap.add_argument("--window-min", type=int, default=WINDOW_MINUTES)
    ap.add_argument("--threshold", type=float, default=PSI_ALERT,
                    help="alert at or above this PSI. Justified in the README.")
    ap.add_argument("--out", type=Path, default=Path("reports/drift.json"))
    ap.add_argument("--emit", action="store_true", help="send scores as cloud metrics")
    ap.add_argument("--no-fail", action="store_true",
                    help="exit 0 on drift. The scheduled job alerts through the metric, "
                         "and a failed run would only be retried.")
    args = ap.parse_args()

    cfg = config.load(strict=False)
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    reference = _reference(args.reference, cfg)
    if args.current:
        current = pd.read_csv(args.current)
    else:
        from cloudlayer.factory import get_adapter
        rows = get_adapter(cfg).recent_inputs(args.endpoint, args.window_min)
        current = pd.DataFrame(rows, columns=data.FEATURES)

    adapter = None
    if args.emit:
        from cloudlayer.factory import get_adapter
        adapter = get_adapter(cfg)
        adapter.emit_metric("drift.window_rows", len(current))

    if len(current) < MIN_WINDOW_ROWS:
        print(f"{checked_at}  SKIP  {len(current)} rows in the window, need {MIN_WINDOW_ROWS}. "
              "Too few to tell drift from noise; no scores written.")
        return 0

    results = compare(reference, current, data.FEATURES)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps([asdict(r) for r in results], indent=2))

    print(f"{checked_at}  {len(current)} rows")
    print(f"{'feature':<22}{'psi':>10}{'ks':>10}{'ref mean':>11}{'cur mean':>11}  verdict")
    for r in results:
        print(f"{r.feature:<22}{r.psi:>10.5f}{r.ks_statistic:>10.5f}"
              f"{r.ref_mean:>11.3f}{r.cur_mean:>11.3f}  {r.verdict}")
    # One JSON line per check: in the job's logs this is the record of what it saw, when.
    print(json.dumps({"event": "drift_check", "checked_at": checked_at, "rows": len(current),
                      "psi": {r.feature: r.psi for r in results}}))

    if adapter is not None:
        for r in results:
            labels = {"feature": r.feature}
            adapter.emit_metric("drift.psi", r.psi, labels=labels)
            adapter.emit_metric("drift.ks", r.ks_statistic, labels=labels)
            adapter.emit_metric("drift.mean", r.cur_mean, labels=labels)

    breached = [r for r in results if r.psi >= args.threshold]
    if breached:
        print(f"\nALERT  {len(breached)} feature(s) at or above threshold {args.threshold}: "
              + ", ".join(r.feature for r in breached))
        print("Before you retrain: is this drift, or is it a broken upstream pipeline? "
              "Retraining on corrupted data destroys a working model faster than any "
              "schedule would.")
        return 0 if args.no_fail else 2
    print(f"\nOK  no feature at or above threshold {args.threshold}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
