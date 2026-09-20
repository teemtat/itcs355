"""Lab 2 — rank tracked runs by metric AND by cost per point.

    python scripts/compare_runs.py --experiment itcs355-lab2

Writes reports/lab2-comparison.md. The cost-per-point column is what the lab is about:
the highest-scoring run is frequently not the one you should register.

Three things this report insists on:

  * validation and test in separate columns, because choosing on test leaves you with no
    honest estimate of anything;
  * the seed sweep printed next to the grid, because a 0.0015 win between two rows means
    nothing if rerunning one of them at another seed moves it by 0.0033;
  * the money the study THINKS it spent reconciled against the machine time actually
    billed, which are not the same number and are not close.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mlflow
import pandas as pd

from src import config, costs

PARAM_COLS = ["n_estimators", "max_depth", "min_samples_leaf", "class_weight"]


def to_md(df: pd.DataFrame) -> str:
    """Markdown table without pulling in tabulate.

    requirements.txt is installed with --require-hashes, so adding a dependency means
    recompiling the lock. Not worth it for a table renderer.
    """
    cols = list(df.columns)
    rows = [[("" if pd.isna(v) else str(v)) for v in rec] for rec in df.itertuples(index=False)]
    widths = [max(len(c), *(len(r[i]) for r in rows)) if rows else len(c)
              for i, c in enumerate(cols)]
    head = "| " + " | ".join(c.ljust(w) for c, w in zip(cols, widths)) + " |"
    rule = "|" + "|".join("-" * (w + 2) for w in widths) + "|"
    body = ["| " + " | ".join(v.ljust(w) for v, w in zip(r, widths)) + " |" for r in rows]
    return "\n".join([head, rule, *body])


def job_wallclock(path: Path) -> list[dict]:
    """Every managed job we ran, from the log train_remote.py appends to."""
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


JUSTIFICATION_HEADING = "## Which model did you register, and why?"

TEMPLATE = JUSTIFICATION_HEADING + """

TODO(Lab 2): 200 words maximum. Must address all four:

1. Why this model rather than the highest-scoring one, if they differ
2. The variance across seeds for your chosen configuration
3. What it costs to train, and to retrain monthly
4. One way this choice could be wrong

An answer that only says "highest validation score" scores zero on this task."""


def keep_justification(out: Path) -> str:
    """Rerunning the comparison must not delete the answer you wrote into it.

    The tables above are regenerated from the tracking store every time. The
    justification is not derived from anything; it is the graded part, and it lives in
    the same file. Carry it across rather than overwriting it with the prompt again.
    """
    if out.exists():
        text = out.read_text()
        if JUSTIFICATION_HEADING in text:
            existing = text[text.index(JUSTIFICATION_HEADING):].rstrip()
            if "TODO(Lab 2)" not in existing:
                return existing
    return TEMPLATE


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--experiment", default="itcs355-lab2")
    ap.add_argument("--metric", default="val_roc_auc")
    ap.add_argument("--instance", default="n1-standard-4")
    ap.add_argument("--spot", action="store_true", default=True)
    ap.add_argument("--jobs", type=Path, default=Path("reports/lab2-jobs.jsonl"))
    ap.add_argument("--out", type=Path, default=Path("reports/lab2-comparison.md"))
    args = ap.parse_args()

    cfg = config.load(strict=False)
    mlflow.set_tracking_uri(cfg.mlflow_tracking_uri)
    exp = mlflow.get_experiment_by_name(args.experiment)
    if exp is None:
        print(f"No experiment named {args.experiment!r}. Run `make tune-remote` first.")
        return 1

    runs = mlflow.search_runs(experiment_ids=[exp.experiment_id])
    if runs.empty:
        print("No runs found.")
        return 1

    phase = runs.get("tags.phase", pd.Series(["grid"] * len(runs)))
    grid_runs = runs[phase == "grid"]
    sweep_runs = runs[phase == "seed-sweep"]
    if grid_runs.empty:
        grid_runs = runs

    metric_col = f"metrics.{args.metric}"
    baseline = grid_runs[metric_col].min()

    table = pd.DataFrame({
        "run_id": grid_runs["run_id"].str[:8],
        **{p: grid_runs.get(f"params.{p}") for p in PARAM_COLS},
        "val_roc_auc": grid_runs[metric_col].round(4),
        "val_pr_auc": grid_runs.get("metrics.val_pr_auc").round(4),
        "test_roc_auc": grid_runs.get("metrics.test_roc_auc").round(4),
        "fit_s": grid_runs.get("metrics.duration_s").round(2),
        "fit_thb": grid_runs.get("metrics.cost_thb").round(6),
    })
    gain = (table["val_roc_auc"] - baseline).clip(lower=1e-9)
    table["thb_per_point"] = (table["fit_thb"] / (gain * 100)).round(5)
    table = table.sort_values("val_roc_auc", ascending=False)

    # --- the seed sweep -----------------------------------------------------
    sweep_lines: list[str] = []
    spread = stdev = None
    if not sweep_runs.empty:
        scores = sorted(sweep_runs[metric_col].tolist())
        spread = max(scores) - min(scores)
        stdev = statistics.stdev(scores) if len(scores) > 1 else 0.0
        sweep_tbl = pd.DataFrame({
            "run_id": sweep_runs["run_id"].str[:8],
            "seed": sweep_runs.get("params.seed"),
            "val_roc_auc": sweep_runs[metric_col].round(4),
            "test_roc_auc": sweep_runs.get("metrics.test_roc_auc").round(4),
        }).sort_values("seed")
        top2 = table["val_roc_auc"].head(2).tolist()
        margin = round(top2[0] - top2[1], 4) if len(top2) > 1 else 0.0
        sweep_lines = [
            "## Seed variance for the best validation configuration",
            "",
            "The split is held at the study seed; only the model's own randomness moves, so",
            "this is the configuration's variance and not the split's.",
            "",
            to_md(sweep_tbl),
            "",
            f"**mean {statistics.mean(scores):.4f} · stdev {stdev:.4f} · spread {spread:.4f}**",
            "",
            f"The gap between the top two grid rows is {margin:.4f}. "
            + ("That gap is smaller than the spread above, so the top rows are not "
               "distinguishable on score and something else has to decide."
               if margin < spread else
               "That gap is larger than the spread above, so the ordering survives a reseed."),
            "",
        ]

    # --- what the study thinks it spent, vs what was billed -----------------
    fit_total = float(table["fit_thb"].sum())
    rate = costs.hourly_rate(cfg.provider, args.instance, spot=args.spot)
    jobs = [j for j in job_wallclock(args.jobs) if j.get("succeeded")]
    billed_s = sum(j.get("duration_s") or 0 for j in jobs)
    billed = billed_s / 3600.0 * rate

    lines = [
        "# Lab 2 — Run comparison",
        "",
        f"Experiment `{args.experiment}` · {len(table)} grid trials · "
        f"{len(sweep_runs)} seed-sweep runs",
        "",
        "`thb_per_point` is cost per percentage point of "
        f"{args.metric} above the worst trial. Cheap improvements rank low; expensive "
        "improvements rank high, however good the headline number is.",
        "",
        to_md(table),
        "",
        *sweep_lines,
        "## Cost: measured two ways, and they disagree",
        "",
        "| | THB |",
        "|---|---|",
        f"| Sum of per-trial fit time, as the study logged it | {fit_total:.4f} |",
        f"| Machine time actually billed ({billed_s/60:.1f} min at {rate:.2f} THB/h) | {billed:.4f} |",
        "",
        f"A factor of about {billed / fit_total:.0f}. The study times `model.fit`; the",
        "provider charges for the machine from the moment it boots — image pull, data",
        "load, artifact writes, and every second the process spends doing something that",
        "is not training. Quote the second number in anything anyone pays for.",
        "",
        "## Price provenance",
        "",
        f"Rates from `src/costs.py`: {args.instance} at {rate:.2f} THB/h "
        f"({'spot' if args.spot else 'on-demand'}), verified 2026-09-20 against the Vertex AI",
        "pricing page with the region set to Singapore (asia-southeast1) and against the",
        "Cloud Billing Catalog API. USD/THB 33.36. See the module docstring for the",
        "derivation, including why spot is 34% of on-demand here and not 30%.",
        "",
        keep_justification(args.out),
    ]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines))
    print(f"wrote {args.out}  ({len(table)} grid trials, {len(sweep_runs)} sweep runs)")
    print(table.head(6).to_string(index=False))
    if spread is not None:
        print(f"\nseed spread {spread:.4f}  stdev {stdev:.4f}")
    print(f"\nfit-time cost {fit_total:.4f} THB   billed {billed:.4f} THB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
