"""Data loading and splitting.

The split is built here, inside the pipeline, and is deterministic given the seed.
It is NOT built by hand and committed, because a hand-made split cannot be reproduced
and cannot be audited for leakage.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

TARGET = "failed_within_7d"
GROUP = "machine_id"
ID = "reading_id"

FEATURES = [
    "temp_c",
    "vibration_mm_s",
    "pressure_kpa",
    "hours_since_service",
    "load_pct",
    "ambient_humidity",
]

# Contract the data must satisfy. tests/test_data.py asserts against this, and Lab 4
# turns it into a CI gate.
SCHEMA: dict[str, str] = {
    ID: "int64",
    GROUP: "int64",
    "temp_c": "float64",
    "vibration_mm_s": "float64",
    "pressure_kpa": "float64",
    "hours_since_service": "float64",
    "load_pct": "float64",
    "ambient_humidity": "float64",
    TARGET: "int64",
}

PLAUSIBLE_RANGES: dict[str, tuple[float, float]] = {
    "temp_c": (-10.0, 140.0),
    "vibration_mm_s": (0.0, 60.0),
    "pressure_kpa": (0.0, 600.0),
    "hours_since_service": (0.0, 20000.0),
    "load_pct": (0.0, 100.0),
    "ambient_humidity": (0.0, 100.0),
}


def load_raw(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Run `make data` first, or `dvc pull` if the remote is configured."
        )
    return pd.read_csv(path)


def data_fingerprint(path: Path) -> str:
    """Content hash of the raw file. Logged with every run so a metric can be traced to data."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def dvc_dir_md5(directory: Path) -> str:
    """Recompute DVC's hash of a tracked directory from the bytes actually on disk.

    This is the value `dvc add` writes into data/raw.dvc: the md5 of the sorted
    {md5, relpath} manifest, suffixed ".dir". Computing it rather than reading it means
    the logged data version describes the data the model saw, not the pointer file.
    """
    entries = [
        {"md5": _md5(p), "relpath": p.relative_to(directory).as_posix()}
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    ]
    entries.sort(key=lambda e: e["relpath"])
    manifest = json.dumps(entries, sort_keys=True).encode("utf-8")
    return hashlib.md5(manifest).hexdigest() + ".dir"


def dvc_tracked_md5(pointer: Path) -> str | None:
    """The md5 recorded in a .dvc pointer file, or None if the data is not DVC-tracked."""
    if not pointer.exists():
        return None
    outs = (yaml.safe_load(pointer.read_text()) or {}).get("outs") or [{}]
    return outs[0].get("md5")


def split(
    df: pd.DataFrame,
    seed: int,
    val_frac: float = 0.2,
    test_frac: float = 0.2,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Group-aware split: every reading from one machine lands in exactly one partition.

    Splitting on rows instead of machines leaks — readings from the same machine minutes
    apart are near-duplicates, so a row-wise split lets the model memorise the machine and
    reports a validation score it will never reproduce in production. tests/test_data.py
    checks this property holds.
    """
    rng = np.random.default_rng(seed)
    groups = np.sort(df[GROUP].unique())
    shuffled = rng.permutation(groups)

    n = len(shuffled)
    n_test = int(round(n * test_frac))
    n_val = int(round(n * val_frac))

    test_g = set(shuffled[:n_test].tolist())
    val_g = set(shuffled[n_test:n_test + n_val].tolist())
    train_g = set(shuffled[n_test + n_val:].tolist())

    parts = tuple(
        df[df[GROUP].isin(g)].sort_values(ID).reset_index(drop=True)
        for g in (train_g, val_g, test_g)
    )
    if any(len(p) == 0 for p in parts):
        raise ValueError("A split partition is empty — too few machines for these fractions.")
    return parts
