"""Lab 3 Task 4 — one picture per canary run: canary share (evidence) and detector z.

    python scripts/canary_plot.py run90 run50
"""
from __future__ import annotations

import csv
import json
import sys
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

D = Path("reports/lab3/canary")


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


def main() -> int:
    tags = sys.argv[1:] or ["run90", "run50"]
    fig, axes = plt.subplots(len(tags), 1, figsize=(9, 3.4 * len(tags)), squeeze=False)
    for ax, tag in zip(axes[:, 0], tags):
        s = json.loads((D / f"{tag}-summary.json").read_text())
        t0 = ts(s["load_start"])
        share = list(csv.DictReader((D / f"{tag}-versions-per-10s.csv").open()))
        win = list(csv.DictReader((D / f"{tag}-windows.csv").open()))
        ax.step([(ts(r["bucket_start_utc"]) - t0).total_seconds() for r in share],
                [100 * float(r["share_canary"]) for r in share], where="post",
                color="#c0392b", label="served by v2, % (response header — evidence only)")
        ax.set_ylabel("% of requests on v2")
        ax.set_ylim(0, 60)
        ax2 = ax.twinx()
        ax2.plot([float(r["t_s"]) for r in win], [float(r["z"]) for r in win], "o-",
                 color="#2c3e50", ms=3, label="detector z (blind: mean p only)")
        ax2.axhline(s["z_threshold"], ls="--", color="#2c3e50", lw=0.8)
        ax2.set_ylabel("z")
        for key, lab in (("shift", "split applied"), ("rollback", "rolled back")):
            x = (ts(s[key]["at"]) - t0).total_seconds()
            ax.axvline(x, color="grey", lw=0.8)
            ax.text(x, 57, f" {lab}", fontsize=8, va="top")
        ax.set_title(f"{tag}: split {s['split']}, detected {s['detection_s']} s after the split")
        ax.set_xlabel("seconds since load start")
        h1, l1 = ax.get_legend_handles_labels()
        h2, l2 = ax2.get_legend_handles_labels()
        ax.legend(h1 + h2, l1 + l2, loc="upper left", fontsize=8)
    fig.tight_layout()
    out = D / "canary.png"
    fig.savefig(out, dpi=130)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
