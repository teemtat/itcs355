"""Copy a directory tree onto a mounted object store.

`shutil.copytree` and `shutil.copy2` preserve metadata, and preserving metadata means
calling utime() and chmod() on the destination. A bucket mounted as a filesystem does
not implement those: the objects underneath have no mtime you are allowed to set. So a
perfectly ordinary artifact copy dies with

    PermissionError: [Errno 1] Operation not permitted   (lookup("utime"))

after the model has already trained. Copy the bytes and nothing else.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path


def mirror_tree(src: Path | str, dst: Path | str) -> int:
    """Copy every file under src into dst, contents only. Returns files written."""
    src, dst = Path(src), Path(dst)
    if not src.exists():
        return 0
    written = 0
    for root, _dirs, files in os.walk(src):
        rel = Path(root).relative_to(src)
        target = dst / rel
        target.mkdir(parents=True, exist_ok=True)
        for name in files:
            shutil.copyfile(Path(root) / name, target / name)
            written += 1
    return written


def sync_tracking_dir(tracking_uri: str, sync_dir: str | None) -> None:
    """Mirror a local MLflow file store to SYNC_DIR, if one is configured.

    Called after every trial, not once at the end: a spot machine can be reclaimed
    between two trials, and runs that only exist on that machine's disk are gone.
    """
    if not sync_dir or not tracking_uri.startswith("file:"):
        return
    local = tracking_uri.removeprefix("file://")
    n = mirror_tree(local, sync_dir)
    print(f"  synced {n} tracking files -> {sync_dir}")
