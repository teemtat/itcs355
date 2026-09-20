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
    """Copy files under src into dst, contents only, skipping unchanged ones."""
    src, dst = Path(src), Path(dst)
    if not src.exists():
        return 0
    written = 0
    for root, _dirs, files in os.walk(src):
        rel = Path(root).relative_to(src)
        target = dst / rel
        target.mkdir(parents=True, exist_ok=True)
        for name in files:
            src_file, dst_file = Path(root) / name, target / name
            # MLflow's file store is close to append-only: finished runs never change.
            # Re-copying the whole tree after every trial made the study O(n^2) and took
            # 43 minutes of billed machine time to do about 40 seconds of training.
            try:
                if dst_file.exists() and dst_file.stat().st_size == src_file.stat().st_size:
                    continue
            except OSError:
                pass
            shutil.copyfile(src_file, dst_file)
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
    print(f"  synced {n} new tracking files -> {sync_dir}")
