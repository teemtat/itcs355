"""Lab 2 — bring the runs the managed job tracked in the bucket back down.

    python scripts/pull_runs.py

The job tracks into BLOB_URI/lab2/mlruns. Comparison and registration happen here, so
the runs have to come back. Nothing is recomputed: these are the same run directories
the job wrote, byte for byte.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloudlayer.factory import get_adapter
from src import config


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", default="lab2/mlruns", help="prefix under BLOB_URI")
    ap.add_argument("--out", type=Path, default=Path("reports/remote-mlruns"))
    args = ap.parse_args()

    cfg = config.load()
    adapter = get_adapter(cfg)
    print(f"pulling {cfg.blob_uri}/{args.key} -> {args.out}")
    n = adapter.download_prefix(args.key, str(args.out))
    print(f"{n} files")
    print("\\ncompare them with:")
    print(f"  MLFLOW_ALLOW_FILE_STORE=true MLFLOW_TRACKING_URI=file://{args.out.resolve()} \\\\")
    print("    python scripts/compare_runs.py --experiment itcs355-lab2")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
