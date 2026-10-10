"""Lab 4 — pick the drift threshold from my own data, not from a tutorial.

    python scripts/drift_calibrate.py --model <registered model.joblib>

Four questions, answered with the same psi() and ks_statistic() the detector uses:

  1. noise     How high does PSI go when NOTHING changed? Windows of new readings from
               the same 240 machines, compared with the training data.
  2. effect    How big a temp_c shift actually changes what the model tells people?
  3. detection With a +6 °C shift arriving mid-window, how much of the window has to be
               drifted before the alert fires?
  4. modes     shift / scale / mix from scripts/inject_drift.py: which statistic sees which.

Writes reports/lab4/calibration.json. The README quotes it.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import joblib
import numpy as np
import pandas as pd

from monitoring.drift import ks_statistic, psi
from scripts.make_dataset import build
from src import data

FLEET_SEED = 20260101  # the fleet the model was trained on
ALERT_PROBABILITY = 0.3  # the risk level a dashboard flags (Lab 3 canary section)


def window(df: pd.DataFrame, n: int, rng: np.random.Generator) -> pd.DataFrame:
    """n readings in arrival order. Every machine reports, so arrival order is random."""
    return df.iloc[rng.choice(len(df), size=n, replace=False)]


def noise(reference: pd.DataFrame, sizes: list[int], streams: int, per_stream: int) -> dict:
    out = {}
    rng = np.random.default_rng(0)
    for n in sizes:
        per_feature = {f: [] for f in data.FEATURES}
        worst = []  # the alert fires on ANY feature, so the max over features is what matters
        for s in range(1, streams + 1):
            stream = build(FLEET_SEED, reading_seed=1000 + s)
            for _ in range(per_stream):
                w = window(stream, n, rng)
                scores = {f: psi(reference[f].to_numpy(float), w[f].to_numpy(float))
                          for f in data.FEATURES}
                for f, v in scores.items():
                    per_feature[f].append(v)
                worst.append(max(scores.values()))
        out[n] = {
            "windows": len(worst),
            "max_over_features": {
                "p50": round(float(np.percentile(worst, 50)), 4),
                "p99": round(float(np.percentile(worst, 99)), 4),
                "max": round(float(np.max(worst)), 4),
            },
            "temp_c_p99": round(float(np.percentile(per_feature["temp_c"], 99)), 4),
        }
    return out


def effect(reference: pd.DataFrame, model, n: int, shifts: list[float]) -> list[dict]:
    stream = build(FLEET_SEED, reading_seed=2001)
    rng = np.random.default_rng(1)
    rows = []
    for d in shifts:
        shifted = stream.copy()
        shifted["temp_c"] = shifted["temp_c"] + d
        proba = model.predict_proba(shifted[data.FEATURES])[:, 1]
        psis = [psi(reference["temp_c"].to_numpy(float),
                    window(shifted, n, rng)["temp_c"].to_numpy(float)) for _ in range(20)]
        rows.append({
            "shift_c": d,
            "psi_median": round(float(np.median(psis)), 4),
            "mean_probability": round(float(proba.mean()), 4),
            "flagged_share": round(float((proba > ALERT_PROBABILITY).mean()), 4),
        })
    return rows


def detection(reference: pd.DataFrame, n: int, shift: float, fractions: list[float]) -> list[dict]:
    normal = build(FLEET_SEED, reading_seed=3001)
    drifted = build(FLEET_SEED, reading_seed=3002)
    drifted["temp_c"] = drifted["temp_c"] + shift
    rng = np.random.default_rng(2)
    rows = []
    for f in fractions:
        k = int(round(n * f))
        psis = []
        for _ in range(20):
            w = pd.concat([window(normal, n - k, rng), window(drifted, k, rng)]) if k else window(normal, n, rng)
            psis.append(psi(reference["temp_c"].to_numpy(float), w["temp_c"].to_numpy(float)))
        rows.append({"drifted_fraction": f, "psi_median": round(float(np.median(psis)), 4)})
    return rows


def modes(reference: pd.DataFrame) -> list[dict]:
    """The three injections from inject_drift.py, applied to a NEW stream (not to the
    reference itself), scored on the whole stream and on one window."""
    stream = build(FLEET_SEED, reading_seed=4001)
    rng = np.random.default_rng(3)
    cases = [("none", None), ("shift", 6.0), ("scale", 1.5), ("mix", None)]
    out = []
    for mode, magnitude in cases:
        df = stream.copy()
        if mode == "shift":
            df["temp_c"] = df["temp_c"] + magnitude
        elif mode == "scale":
            m = df["temp_c"].mean()
            df["temp_c"] = m + (df["temp_c"] - m) * magnitude
        elif mode == "mix":  # same code as inject_drift.py --mode mix, seed 7
            machines = df["machine_id"].unique()
            favoured = np.random.default_rng(7).choice(machines, size=len(machines) // 5,
                                                       replace=False)
            weights = np.where(df["machine_id"].isin(favoured), 5.0, 1.0)
            df = df.sample(n=len(df), replace=True, weights=weights, random_state=7)
        ref = reference["temp_c"].to_numpy(float)
        cur = df["temp_c"].to_numpy(float)
        worst = max(data.FEATURES, key=lambda f: psi(reference[f].to_numpy(float),
                                                      df[f].to_numpy(float)))
        out.append({
            "mode": mode,
            "temp_mean": round(float(cur.mean()), 2),
            "temp_psi": round(psi(ref, cur), 4),
            "temp_ks": round(ks_statistic(ref, cur), 4),
            "worst_feature": worst,
            "worst_psi": round(psi(reference[worst].to_numpy(float), df[worst].to_numpy(float)), 4),
            "temp_psi_one_window": round(psi(ref, window(df, 1800, rng)["temp_c"].to_numpy(float)), 4),
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reference", type=Path, default=Path("data/raw/sensors.csv"))
    ap.add_argument("--model", type=Path, required=True, help="the registered model.joblib")
    ap.add_argument("--window", type=int, default=1800, help="rows per detector window")
    ap.add_argument("--out", type=Path, default=Path("reports/lab4/calibration.json"))
    args = ap.parse_args()

    reference = data.load_raw(args.reference)
    model = joblib.load(args.model)

    result = {
        "window_rows": args.window,
        "noise": noise(reference, [300, 900, args.window, 3600], streams=10, per_stream=50),
        "effect": effect(reference, model, args.window, [0, 1, 2, 3, 4, 6]),
        "detection_shift_6": detection(reference, args.window, 6.0,
                                       [0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.7, 1.0]),
        "modes": modes(reference),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
