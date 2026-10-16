"""Lab 5 — the registration gate.

Compares a candidate against the currently registered production model. Exits non-zero
when the candidate should NOT be registered, which is what makes the pipeline condition
real rather than decorative.

    python scripts/evaluation_gate.py --metrics reports/metrics.json --incumbent 0.90
    python scripts/evaluation_gate.py --metrics run/metrics.json --data run/data/raw/sensors.csv

Exit codes: 0 register, 1 do not register, 2 the gate itself could not decide.

With --data and no --incumbent, the incumbent is the registry's production version
(cloudlayer.pipelines.incumbent), and it is RE-SCORED on the candidate's own test split
rather than trusted at the number it was registered with. A registered metric was measured
on whatever data existed then; comparing that number to a candidate measured on this
week's data compares two different test sets, and the difference between them can be
larger than the margin. Same rows, both models: then the difference is about the models.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Measured, not chosen (reports/lab5-pipeline.md, "Gate margin"). On the 3-week data the
# pipeline trains on, with the split fixed:
#   * the same config retrained with five different random_state values moves
#     test ROC AUC by sd 0.0003-0.0008;
#   * a 300-sample bootstrap of the test set puts the sd of the PAIRED difference
#     (candidate minus incumbent, same rows) at 0.0017.
# 0.004 is about 2.3 of the larger sd, so a one-sided chance of promoting a candidate that
# is really no better is about 1%. It is still small enough that one more week of data
# (+0.0053 measured) clears it. A margin of zero promotes noise; a margin of 0.01 would
# have blocked every improvement this system has produced.
MIN_IMPROVEMENT = 0.004

# The worst Lab 2 trial scored 0.8245 val ROC AUC, and every configuration in the study
# cleared 0.82. A candidate below 0.80 is not a weaker model, it is a broken run (wrong
# data, wrong labels), and it must not register even when there is no incumbent.
MIN_ABSOLUTE = 0.80


def score_incumbent(model_path: Path, data_path: Path, seed: int, metric: str) -> float:
    import joblib
    from sklearn.metrics import average_precision_score, roc_auc_score

    from src import data

    split, kind = metric.split("_", 1)
    parts = dict(zip(("train", "val", "test"), data.split(data.load_raw(data_path), seed=seed)))
    part = parts[split]
    proba = joblib.load(model_path).predict_proba(part[data.FEATURES])[:, 1]
    fn = roc_auc_score if kind == "roc_auc" else average_precision_score
    return float(fn(part[data.TARGET], proba))


def registry_incumbent(args, seed: int) -> tuple[float | None, dict]:
    """Score the registry's production version on the candidate's test split."""
    from cloudlayer import pipelines
    from cloudlayer.factory import get_adapter
    from src import config

    cfg = config.load()
    inc = pipelines.incumbent(cfg, cfg.model_registry_name)
    if inc is None:
        return None, {"source": "registry", "found": False}
    local = Path(tempfile.mkdtemp()) / "model.joblib"
    get_adapter(cfg).download(inc["artifact_uri"].rstrip("/") + "/model.joblib", str(local))
    score = score_incumbent(local, args.data, seed, args.metric)
    return score, {"source": "registry", "found": True, **inc}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--metrics", type=Path, required=True)
    ap.add_argument("--metric", default="test_roc_auc")
    ap.add_argument("--incumbent", type=float, default=None,
                    help="current production score; omit to read from the registry")
    ap.add_argument("--data", type=Path, default=None,
                    help="the candidate's dataset, to re-score the registry incumbent on")
    ap.add_argument("--report", type=Path, default=None, help="write the decision as JSON")
    args = ap.parse_args()

    payload = json.loads(args.metrics.read_text())
    candidate = float(payload[args.metric])
    report = {"metric": args.metric, "candidate": candidate,
              "min_absolute": MIN_ABSOLUTE, "min_improvement": MIN_IMPROVEMENT}

    def decide(decision: str, reason: str) -> int:
        print(f"GATE {decision.upper()}  {reason}")
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps({**report, "decision": decision,
                                               "reason": reason}, indent=2))
        return 0 if decision == "pass" else 1

    print(f"candidate {args.metric} = {candidate:.4f}")
    if candidate < MIN_ABSOLUTE:
        return decide("fail", f"below absolute floor {MIN_ABSOLUTE}")

    incumbent = args.incumbent
    if incumbent is not None:
        report["incumbent_source"] = {"source": "--incumbent"}
    elif args.data is not None:
        try:
            incumbent, source = registry_incumbent(args, int(payload["seed"]))
        except Exception as e:  # noqa: BLE001 — any failure here means "cannot decide"
            print(f"GATE ERROR  could not read the incumbent: {e}")
            return 2
        report["incumbent_source"] = source
        if incumbent is None:
            # A first run legitimately has none: the absolute floor is the only bar.
            return decide("pass", "no production version in the registry; first registration")
        print(f"incumbent = {source['model']}@{source['version']} re-scored on this split")
    else:
        print("GATE ERROR  no --incumbent and no --data to score the registry's version on")
        return 2

    delta = candidate - incumbent
    report.update(incumbent=incumbent, delta=delta)
    print(f"incumbent {incumbent:.4f}  delta {delta:+.4f}  required {MIN_IMPROVEMENT:+.4f}")
    if delta < MIN_IMPROVEMENT:
        return decide("fail", f"improvement {delta:+.4f} is below the {MIN_IMPROVEMENT} "
                              "margin, so not distinguishable from noise; not registering")
    return decide("pass", f"beats the incumbent by {delta:+.4f}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 — a crash must not exit 1, which means "rejected"
        print(f"GATE ERROR  {type(e).__name__}: {e}")
        raise SystemExit(2) from e
