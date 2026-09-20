"""Lab 2 — run the training container as a managed job instead of on your laptop.

    python scripts/train_remote.py                     # one training run  (Task 1)
    python scripts/train_remote.py --module src.tune   # the budgeted study (Task 2)

Nothing here names a provider. The job needs four things — an image, a command, an
instance type, and an identity — and the adapter knows what its platform calls them.

The job reads data from BLOB_URI and writes everything back to BLOB_URI. It never
touches this machine, which is the whole point: if it needs your laptop, it is not a
managed job, it is a remote shell.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloudlayer.factory import get_adapter
from src import config

JOB_LOG = Path("reports/lab2-jobs.jsonl")


def git_commit() -> str:
    out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 else "unknown"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ITCS355 Lab 2 — managed training job")
    p.add_argument("--image-uri", help="digest-pinned image. Default: push IMAGE:TAG first.")
    p.add_argument("--image", default=None, help="local tag to push, e.g. itcs355-lab1:abc1234")
    p.add_argument("--module", default="src.train", help="src.train (Task 1) or src.tune (Task 2)")
    p.add_argument("--machine-type", default="n1-standard-4")
    p.add_argument("--no-spot", action="store_true", help="on-demand. Costs ~3x. Task 2 wants spot.")
    p.add_argument("--experiment", default="itcs355-lab2")
    p.add_argument("--run-name", default=None, help="subdirectory under BLOB_URI for this job")
    p.add_argument("--extra", nargs=argparse.REMAINDER, default=[],
                   help="everything after this flag is passed to the module verbatim")
    p.add_argument("--skip-upload", action="store_true", help="data already in the bucket")
    p.add_argument("--no-wait", action="store_true", help="submit and exit")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    cfg = config.load()
    adapter = get_adapter(cfg)
    commit = git_commit()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    run_name = args.run_name or f"{args.module.split('.')[-1]}-{stamp}"

    # 1. Data into the bucket. The job reads it from there, not from here.
    if not args.skip_upload:
        print(f"uploading {cfg.raw_path} -> BLOB_URI/data/raw/sensors.csv")
        print("  " + adapter.upload(str(cfg.raw_path), "data/raw/sensors.csv"))

    # 2. The image. Digest-pinned, so the job cannot silently run different code.
    image_uri = args.image_uri
    if not image_uri:
        local_tag = args.image or f"itcs355-lab1:{commit[:7]}"
        print(f"pushing {local_tag}")
        image_uri = adapter.push_image(local_tag)
    print(f"image: {image_uri}")

    # 3. Where the job reads and writes. All of it inside BLOB_URI.
    data_dir = adapter.mount_path("data")
    reports_dir = adapter.mount_path(f"lab2/{run_name}")
    # MLflow tracks on the container's own disk and we mirror it to the bucket after
    # each trial. Tracking straight onto the mount fails the moment MLflow logs a model:
    # it copies artifacts with shutil.copy2, which sets mtime, which a bucket mount does
    # not allow. See src/mirror.py.
    tracking_uri = "file:///tmp/mlruns"
    sync_dir = adapter.mount_path("lab2/mlruns")

    job_args = ["--experiment", args.experiment, *args.extra]
    if args.module == "src.train":
        job_args += ["--metrics-out", f"{reports_dir}/metrics.json"]
    if args.module == "src.tune":
        # The checkpoint lives in the bucket, not in the container. That is what makes a
        # reclaimed spot machine cost minutes instead of the whole study: resubmit, and
        # the next job picks up the trials the last one finished.
        job_args += [
            "--checkpoint", adapter.mount_path("lab2/tune_checkpoint.json"),
            "--instance", args.machine_type,
        ]
        if not args.no_spot:
            job_args += ["--spot"]

    spec = {
        "command": ["python", "-m", args.module],
        "job_args": job_args,
        "machine_type": args.machine_type,
        "spot": not args.no_spot,
        "display_name": f"itcs355-lab2-{run_name}",
        "lab": 2,
        "env": {
            # cloud.env is deliberately not in the image, so the job would otherwise
            # resolve CLOUD_PROVIDER to "local" and price a cloud machine off the local
            # price table. The capability slots the job needs travel with the job.
            "CLOUD_PROVIDER": cfg.provider,
            "PROJECT_ID": cfg.project_id,
            "REGION": cfg.region,
            "DATA_DIR": data_dir,
            "REPORTS_DIR": reports_dir,
            "MLFLOW_TRACKING_URI": tracking_uri,
            "GIT_COMMIT": commit,
            # MLflow 3 refuses a file-store backend unless you opt in. A file store is
            # what we want here: the bucket is mounted as a filesystem, and runs are
            # written as plain files that survive the machine being reclaimed mid-study.
            # A sqlite database over a bucket mount would not survive it — that is a
            # single file rewritten in place, over a filesystem with no real locking.
            "MLFLOW_ALLOW_FILE_STORE": "true",
            "MLRUNS_SYNC_DIR": sync_dir,
        },
    }

    print(f"\nsubmitting: python -m {args.module} {' '.join(job_args)}")
    print(f"  machine   {args.machine_type}  spot={not args.no_spot}")
    print(f"  data      {data_dir}")
    print(f"  tracking  {tracking_uri}  -> mirrored to {sync_dir}")
    job_id = adapter.submit_training(image_uri, spec)
    print(f"\njob id: {job_id}")

    record = {
        "job_id": job_id, "module": args.module, "run_name": run_name,
        "image_uri": image_uri, "git_commit": commit,
        "machine_type": args.machine_type, "spot": not args.no_spot,
        "submitted_at": stamp,
    }

    if args.no_wait:
        _append(record)
        return 0

    print("\nwaiting for the job to finish")
    result = adapter.wait_training(job_id)
    record.update(result)
    _append(record)

    print(json.dumps(result, indent=2))
    if not result["succeeded"]:
        print("\nJOB FAILED. Read the error above before changing anything.")
        print("If it is a permissions error: the identity that RAN the job is not the")
        print("identity that submitted it. Record which permission was missing — Drill 2 asks.")
        return 1
    print(f"\nartifacts: {cfg.blob_uri}/lab2/{run_name}")
    return 0


def _append(record: dict) -> None:
    JOB_LOG.parent.mkdir(parents=True, exist_ok=True)
    with JOB_LOG.open("a") as fh:
        fh.write(json.dumps(record, default=str) + "\n")
    print(f"recorded in {JOB_LOG}")


if __name__ == "__main__":
    raise SystemExit(main())
