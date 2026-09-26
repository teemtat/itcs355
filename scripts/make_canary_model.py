"""Lab 3 Task 4 — a deliberately worse model version, registered like any other.

    python scripts/make_canary_model.py --baseline-uri gs://.../lab2-registry/v1

The regression is a plausible one, not a broken model: someone "boosted recall" by
up-weighting the failure class 2x and trimmed the trees to depth 2. Ranking barely moves
(test ROC AUC about -0.005), which is exactly why an offline AUC gate would wave it
through; what it breaks is calibration — every probability is pushed up, log loss and
Brier get worse, and a dashboard alerting at a fixed threshold fires more often.

It goes through the same registry path as the real model (upload to BLOB_URI, then
adapter.register_model), so the canary is deployed by version exactly like v1.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import joblib
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score

from cloudlayer.factory import get_adapter
from src import config, data, seeds

OUT = Path("reports/lab3/canary-model.json")


def evaluate(model, X, y) -> dict[str, float]:
    p = model.predict_proba(X)[:, 1]
    return {"test_roc_auc": round(roc_auc_score(y, p), 4),
            "test_log_loss": round(log_loss(y, p), 4),
            "test_brier": round(brier_score_loss(y, p), 4),
            "mean_probability": round(float(p.mean()), 4),
            "alert_rate_p_gt_0.3": round(float((p > 0.3).mean()), 4)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline-uri", required=True, help="registry artifact dir of the good version")
    ap.add_argument("--dry-run", action="store_true", help="evaluate only, register nothing")
    args = ap.parse_args()

    cfg = config.load()
    adapter = get_adapter(cfg)
    seed = seeds.set_all()
    df = data.load_raw(cfg.raw_path)
    train_df, _, test_df = data.split(df, seed=seed)
    X, y = test_df[data.FEATURES], test_df[data.TARGET]

    worse = RandomForestClassifier(max_depth=2, min_samples_leaf=5, class_weight={0: 1, 1: 2},
                                   n_jobs=1, random_state=seed)
    worse.fit(train_df[data.FEATURES], train_df[data.TARGET])

    with tempfile.TemporaryDirectory() as tmp:
        base_path = Path(tmp) / "baseline.joblib"
        adapter.download(f"{args.baseline_uri.rstrip('/')}/model.joblib", str(base_path))
        baseline = joblib.load(base_path)

        report = {"baseline": evaluate(baseline, X, y), "canary": evaluate(worse, X, y),
                  "canary_config": {"max_depth": 2, "min_samples_leaf": 5,
                                    "class_weight": {"0": 1, "1": 2}, "seed": seed}}
        print(json.dumps(report, indent=2))
        if args.dry_run:
            return 0

        path = Path(tmp) / "model.joblib"
        joblib.dump(worse, path)
        key = "lab3-registry/canary-worse"
        uri = adapter.upload(str(path), f"{key}/model.joblib")
        version = adapter.register_model(f"{cfg.blob_uri}/{key}", cfg.model_registry_name)

    report.update({"artifact": uri, "registry_version": version})
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2) + "\n")
    print(f"registered {cfg.model_registry_name}@{version}  ({uri})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
