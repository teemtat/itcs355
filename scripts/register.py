"""Lab 2 Task 4 — register the chosen model with lineage back to code and data.

    python scripts/register.py --run 3436d36d --study-mlruns reports/remote-mlruns-final

Lineage exists to answer one question: six months from now, can you rebuild this exact
model? Every tag below is one edge of that answer, and a missing edge is a path by which
the answer becomes no.

The tags go on the MODEL VERSION, not on the run. Tagging the run is the commoner
mistake and looks identical until someone queries the registry and finds it empty.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("MLFLOW_ALLOW_FILE_STORE", "true")

import mlflow
from mlflow import MlflowClient

from cloudlayer.factory import get_adapter
from src import config, data

REQUIRED_TAGS = [
    "git_commit", "data_version", "mlflow_run_id", "training_job_id",
    "image_digest", "seed", "metric_val", "metric_test",
]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ITCS355 Lab 2 — register with lineage")
    p.add_argument("--run", required=True, help="run id or unique prefix of the chosen run")
    p.add_argument("--study-mlruns", type=Path, default=Path("reports/remote-mlruns-final"),
                   help="local copy of the tracking store the job wrote")
    p.add_argument("--experiment", default="itcs355-lab2")
    p.add_argument("--jobs", type=Path, default=Path("reports/lab2-jobs.jsonl"))
    p.add_argument("--stage", default="Staging", help="promote to this stage after registering")
    p.add_argument("--skip-provider", action="store_true",
                   help="register in MLflow only, skip the provider model registry")
    return p.parse_args()


def find_run(store: Path, experiment: str, prefix: str):
    """Locate the run in the study's own store, and its logged model directory."""
    mlflow.set_tracking_uri(f"file://{store.resolve()}")
    client = MlflowClient()
    exps = [e for e in client.search_experiments() if e.name == experiment]
    if not exps:
        raise SystemExit(f"No experiment {experiment!r} in {store}")
    if len(exps) > 1:
        raise SystemExit(
            f"{len(exps)} experiments share the name {experiment!r} in {store}. "
            "Lineage cannot point at an ambiguous experiment — pull a clean store."
        )
    exp = exps[0]
    runs = mlflow.search_runs(experiment_ids=[exp.experiment_id])
    hit = runs[runs.run_id.str.startswith(prefix)]
    if len(hit) != 1:
        raise SystemExit(f"{len(hit)} runs match {prefix!r}; need exactly one")
    row = hit.iloc[0]

    models = client.search_logged_models(experiment_ids=[exp.experiment_id])
    mine = [m for m in models if m.source_run_id == row.run_id]
    if not mine:
        raise SystemExit(f"Run {row.run_id} logged no model")
    # The store recorded the container's own path; the artifacts are here now.
    model_dir = store / exp.experiment_id / "models" / mine[0].model_id / "artifacts"
    if not model_dir.exists():
        raise SystemExit(f"Model artifacts not found at {model_dir}")
    return row, model_dir


def job_for(jobs_path: Path, run_prefix: str, module: str = "src.tune") -> dict:
    """The managed job that produced this run. Most recent successful study job."""
    if not jobs_path.exists():
        return {}
    records = [json.loads(ln) for ln in jobs_path.read_text().splitlines() if ln.strip()]
    studies = [r for r in records if r.get("module") == module and r.get("succeeded")]
    return studies[-1] if studies else {}


def main() -> int:
    args = parse_args()
    cfg = config.load()
    row, model_dir = find_run(args.study_mlruns, args.experiment, args.run)

    # The run recorded a content fingerprint of the data it consumed. The DVC hash is
    # the version name for that content. They are only interchangeable if the working
    # copy still IS that content, so check rather than assume.
    local_fp = data.data_fingerprint(cfg.raw_path)
    run_fp = row.get("tags.data_fingerprint")
    if run_fp != local_fp:
        raise SystemExit(
            f"Data has moved since the run: run consumed {run_fp}, working copy is "
            f"{local_fp}. The DVC hash here would not be the version that trained it."
        )
    dvc_version = data.dvc_tracked_md5(cfg.data_dir / "raw.dvc") or "untracked"

    job = job_for(args.jobs, args.run)
    lineage = {
        "git_commit": row.get("tags.git_commit", "unknown"),
        "data_version": dvc_version,
        "mlflow_run_id": row.run_id,
        "training_job_id": (job.get("job_id") or "unknown").rsplit("/", 1)[-1],
        "image_digest": (job.get("image_uri") or "unknown").rsplit("@", 1)[-1],
        "seed": str(row.get("params.seed")),
        "metric_val": f"{row['metrics.val_roc_auc']:.6f}",
        "metric_test": f"{row['metrics.test_roc_auc']:.6f}",
    }
    missing = [k for k in REQUIRED_TAGS if not lineage.get(k) or "unknown" in str(lineage[k])]
    if missing:
        raise SystemExit(f"Refusing to register with incomplete lineage: {missing}")

    print("lineage")
    for k in REQUIRED_TAGS:
        print(f"  {k:16} {lineage[k]}")

    # --- MLflow registry. This is what reload_check.py loads from. ---------
    mlflow.set_tracking_uri(cfg.mlflow_tracking_uri)
    client = MlflowClient()
    name = cfg.model_registry_name
    try:
        client.create_registered_model(name)
    except Exception:
        pass
    version = client.create_model_version(name=name, source=f"file://{model_dir.resolve()}")
    print(f"\nregistered {name} version {version.version}")

    for k, v in lineage.items():
        client.set_model_version_tag(name, version.version, k, v)
    print(f"tagged the VERSION with {len(lineage)} lineage fields (not the run)")

    client.transition_model_version_stage(name, version.version, args.stage)
    client.set_registered_model_alias(name, args.stage.lower(), version.version)
    print(f"promoted to {args.stage}")

    # --- Provider registry -------------------------------------------------
    if not args.skip_provider:
        adapter = get_adapter(cfg)
        joblib_dir = Path("reports/registry-upload")
        joblib_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [sys.executable, "scripts/export_model.py", "--out", str(joblib_dir / "model.joblib")],
            check=True, env={**os.environ, "MLFLOW_TRACKING_URI": cfg.mlflow_tracking_uri},
        )
        key = f"lab2-registry/v{version.version}"
        uri = adapter.upload(str(joblib_dir / "model.joblib"), f"{key}/model.joblib")
        print(f"uploaded {uri}")
        provider_version = adapter.register_model(f"{cfg.blob_uri}/{key}", name)
        client.set_model_version_tag(name, version.version,
                                     "provider_model_version", provider_version)
        print(f"provider registry version {provider_version}")

    print(f"\nnext:  make reload-check VERSION={version.version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
