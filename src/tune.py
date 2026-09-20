"""Lab 2 — budgeted hyperparameter study.

Run:  python -m src.tune --trials 12 --budget-thb 150

The budget is enforced, not advisory. The study stops when projected spend would exceed
it, and reports what it did not get to. This is the habit the lab is teaching: compute is
a resource you spend deliberately, and a trial that is 0.3% better and four times the cost
is not better.

Every trial logs its estimated cost alongside its metric, so `scripts/compare_runs.py` can
rank by cost per point rather than by metric alone.
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import random
import statistics
import time
from pathlib import Path

import mlflow
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import average_precision_score, roc_auc_score

from src import config, costs, data, mirror, seeds
from src.train import git_commit

# Four axes, 16 combinations. The first three set how much the forest can memorise;
# class_weight is the one that changes what the model is trying to do at all. Only about
# 12% of rows are failures, so "balanced" moves the decision boundary rather than just
# smoothing it, and it shows up in pr_auc long before it shows up in roc_auc.
#
# n_estimators earns its place as the cost axis: 300 trees cost three times 100 trees,
# and whether that buys anything is the question Task 3 asks.
SEARCH_SPACE: dict[str, list] = {
    "n_estimators": [100, 300],
    "max_depth": [4, 12],
    "min_samples_leaf": [1, 5],
    "class_weight": [None, "balanced"],
}


def grid(space: dict[str, list], seed: int | None = None) -> list[dict]:
    """All combinations, shuffled deterministically.

    itertools.product varies the LAST key fastest, so truncating the raw product to a
    budget of N leaves the first key stuck on one value — a study that claims four
    hyperparameters and varies three. Shuffling with the run's seed makes any truncation
    a fair sample of the space, and keeps the order reproducible.
    """
    keys = list(space)
    combos = [dict(zip(keys, values)) for values in itertools.product(*(space[k] for k in keys))]
    if seed is not None:
        random.Random(seed).shuffle(combos)
    return combos


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ITCS355 Lab 2 — budgeted study")
    p.add_argument("--trials", type=int, default=12, help="minimum 12 for the lab")
    p.add_argument("--budget-thb", type=float, default=150.0)
    p.add_argument("--instance", default="local", help="key into src/costs.py PRICE_TABLE")
    p.add_argument("--seed", type=int, default=seeds.DEFAULT_SEED)
    p.add_argument("--experiment", default="itcs355-lab2")
    p.add_argument("--checkpoint", type=Path, default=Path("reports/tune_checkpoint.json"),
                   help="Resume file. Spot interruption should cost minutes, not the run.")
    p.add_argument("--spot", action="store_true",
                   help="Price the study at the discounted rate. Set it only if the job "
                        "really is running on spot capacity — a cost report built on the "
                        "wrong rate is worse than no cost report.")
    p.add_argument("--seed-sweep", type=int, default=0, metavar="N",
                   help="After the grid, rerun the best VALIDATION config at N seeds. "
                        "Task 3 asks for the variance; measure it, do not guess it.")
    return p.parse_args()


def load_checkpoint(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {"completed": [], "spent_thb": 0.0}


def save_checkpoint(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))


def run_trial(
    *,
    params: dict,
    model_seed: int,
    split_seed: int,
    splits: tuple,
    fingerprint: str,
    rate: float,
    instance: str,
    spot: bool,
    run_name: str,
    phase: str,
) -> dict:
    """One tracked trial. Returns its metrics plus what it cost to produce them."""
    train_df, val_df, test_df = splits
    started = time.perf_counter()
    with mlflow.start_run(run_name=run_name):
        model = RandomForestClassifier(random_state=model_seed, n_jobs=-1, **params)
        model.fit(train_df[data.FEATURES], train_df[data.TARGET])
        # Score single-threaded: summing per-tree probabilities in thread-completion
        # order makes the last bits depend on the machine's core count, and ties in the
        # ROC ranking can flip. Same reason as src/train.py.
        model.set_params(n_jobs=1)

        metrics = {}
        for name, part in (("val", val_df), ("test", test_df)):
            proba = model.predict_proba(part[data.FEATURES])[:, 1]
            metrics[f"{name}_roc_auc"] = float(roc_auc_score(part[data.TARGET], proba))
            metrics[f"{name}_pr_auc"] = float(average_precision_score(part[data.TARGET], proba))

        elapsed_h = (time.perf_counter() - started) / 3600.0
        trial_cost = elapsed_h * rate

        mlflow.log_params({
            **params,
            "seed": model_seed,
            "split_seed": split_seed,
            "instance": instance,
            "spot": spot,
        })
        mlflow.log_metrics({
            **metrics,
            "duration_s": round(elapsed_h * 3600, 3),
            "cost_thb": round(trial_cost, 6),
        })
        mlflow.set_tags({
            "git_commit": git_commit(),
            "data_fingerprint": fingerprint,
            "lab": "2",
            "phase": phase,
        })
        mlflow.sklearn.log_model(model, name="model")
        run_id = mlflow.active_run().info.run_id

    return {"run_id": run_id, "cost_thb": trial_cost, **metrics}


def main() -> None:
    args = parse_args()
    cfg = config.load(strict=False)
    seed = seeds.set_all(args.seed)

    df = data.load_raw(cfg.raw_path)
    fingerprint = data.data_fingerprint(cfg.raw_path)
    splits = data.split(df, seed=seed)

    mlflow.set_tracking_uri(cfg.mlflow_tracking_uri)
    mlflow.set_experiment(args.experiment)

    state = load_checkpoint(args.checkpoint)
    # Grid results and seed-sweep results live in separate maps. Mixing them means the
    # "best configuration" lookup can land on a sweep entry, whose key is not a
    # configuration at all — which is exactly what the first resume of this study hit.
    state.setdefault("results", {})
    state.setdefault("sweep", {})
    candidates = grid(SEARCH_SPACE, seed=seed)[: args.trials]
    rate = costs.hourly_rate(cfg.provider, args.instance, spot=args.spot)
    print(f"{len(candidates)} trials at {rate:.4f} THB/hour "
          f"({args.instance}{', spot' if args.spot else ', on-demand'})")

    skipped: list[dict] = []
    for i, params in enumerate(candidates):
        key = json.dumps(params, sort_keys=True)
        if key in state["completed"]:
            print(f"trial {i:02d}: already done, skipping (resumed from checkpoint)")
            continue

        if state["spent_thb"] >= args.budget_thb:
            skipped.append(params)
            continue

        result = run_trial(
            params=params, model_seed=seed, split_seed=seed, splits=splits,
            fingerprint=fingerprint, rate=rate, instance=args.instance, spot=args.spot,
            run_name=f"trial-{i:02d}", phase="grid",
        )
        state["spent_thb"] += result["cost_thb"]
        state["completed"].append(key)
        state["results"][key] = result
        save_checkpoint(args.checkpoint, state)
        mirror.sync_tracking_dir(cfg.mlflow_tracking_uri, os.environ.get("MLRUNS_SYNC_DIR"))
        print(f"trial {i:02d}: {params} -> val_roc_auc={result['val_roc_auc']:.4f} "
              f"cost={result['cost_thb']:.6f} THB  cumulative={state['spent_thb']:.6f}")

    # The best configuration is chosen on VALIDATION. Picking it on test would make the
    # test metric a number you fitted, and you would have no honest estimate left.
    if args.seed_sweep and state["results"]:
        best_key = max(state["results"], key=lambda k: state["results"][k]["val_roc_auc"])
        best_params = json.loads(best_key)
        print(f"\nseed sweep: {args.seed_sweep} seeds at {best_params}")
        # The split stays fixed at the study seed. Only the model's own randomness moves,
        # so the spread below is the variance of THIS configuration, not the variance of
        # the data split. Report which one you measured — they answer different questions.
        scores = []
        for s_i in range(1, args.seed_sweep + 1):
            sweep_key = f"{best_key}::seed{s_i}"
            if sweep_key in state["sweep"]:
                scores.append(state["sweep"][sweep_key]["val_roc_auc"])
                print(f"  seed {s_i}: already done, skipping (resumed from checkpoint)")
                continue
            result = run_trial(
                params=best_params, model_seed=s_i, split_seed=seed, splits=splits,
                fingerprint=fingerprint, rate=rate, instance=args.instance, spot=args.spot,
                run_name=f"seed-{s_i}", phase="seed-sweep",
            )
            state["spent_thb"] += result["cost_thb"]
            state["sweep"][sweep_key] = result
            save_checkpoint(args.checkpoint, state)
            mirror.sync_tracking_dir(cfg.mlflow_tracking_uri, os.environ.get("MLRUNS_SYNC_DIR"))
            scores.append(result["val_roc_auc"])
            print(f"  seed {s_i}: val_roc_auc={result['val_roc_auc']:.4f}")
        if len(scores) > 1:
            spread = max(scores) - min(scores)
            stdev = statistics.stdev(scores)
            print(f"\nSEED VARIANCE  mean={statistics.mean(scores):.4f}  "
                  f"stdev={stdev:.4f}  spread={spread:.4f}")
            print("This is the number Task 3 asks for, and Drill 2 asks for. Write it down.")

    print(f"\nspent {state['spent_thb']:.6f} of {args.budget_thb} THB")
    if skipped:
        print(f"BUDGET EXHAUSTED — {len(skipped)} configurations not run:")
        for s_cfg in skipped:
            print(f"  {s_cfg}")
        print("Report this in your README. Which trials you could not afford is a finding, "
              "not an embarrassment.")


if __name__ == "__main__":
    main()
