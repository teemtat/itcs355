"""Lab 5 — the six pipeline steps, as one entry point inside the step image.

    python -m pipeline.steps ingest   --run-id R --weeks 3
    python -m pipeline.steps validate --run-id R
    python -m pipeline.steps train    --run-id R --n-estimators 100 --max-depth 4
    python -m pipeline.steps evaluate --run-id R --decision-out /path
    python -m pipeline.steps register --run-id R
    python -m pipeline.steps deploy   --run-id R --endpoint itcs355-staging --serve-image ...

This is the step logic. pipeline/pipeline.yaml only says which of these runs after which, and
cloudlayer/pipelines.py only translates that into a provider's DAG. Nothing here imports a
provider SDK: steps hand data to each other through BLOB_URI, which the platform mounts as
an ordinary directory (adapter.mount_path), and the two steps that change something in the
cloud go through the adapter.

Every run gets its own directory, BLOB_URI/lab5-pipeline/<run-id>/, so a run's data, model,
metrics and gate decision stay together as that run's lineage.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from cloudlayer.factory import get_adapter  # noqa: E402
from src import config, data  # noqa: E402

PREFIX = "lab5-pipeline"
SEED = 20260101


def _run_dir(adapter, run_id: str) -> Path:
    # PIPELINE_LOCAL_ROOT runs the steps on a laptop against a plain directory.
    local = os.environ.get("PIPELINE_LOCAL_ROOT")
    path = Path(local) / run_id if local else Path(adapter.mount_path(f"{PREFIX}/{run_id}"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _run_uri(cfg, run_id: str, sub: str = "") -> str:
    base = f"{cfg.blob_uri.rstrip('/')}/{PREFIX}/{run_id}"
    return f"{base}/{sub}" if sub else base


def _write(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2))
    print(f"wrote {path}")


# --- ingest -------------------------------------------------------------------------

CORRUPTIONS = {
    "none": "no change",
    # The upstream producer switches pressure from kPa to bar. Every row is still a
    # number, nothing is null, and the model would train on it without complaint.
    "pressure_bar": "pressure_kpa divided by 100 (producer switched to bar)",
    # A sensor feed drops out for a third of the fleet.
    "temp_nulls": "temp_c missing for machines 0-79",
}


def ingest(args) -> int:
    """The fleet's readings for this retrain: the original week plus `weeks - 1` newer weeks.

    In production this pulls from the sensor store. Here the store is the Lab 1 generator,
    and a later week is the same fleet with a new reading seed, which is exactly how
    Lab 4 built its production stream. Machine ids are the same every week, so the grouped
    split puts each machine in the same partition, and the incumbent is never scored on a
    machine it trained on.
    """
    import pandas as pd
    import make_dataset as md

    cfg = config.load()
    run = _run_dir(get_adapter(cfg), args.run_id)
    parts = [md.build(SEED)]
    for week in range(1, args.weeks):
        later = md.build(SEED, reading_seed=100 + week)
        later["reading_id"] += week * len(parts[0])
        parts.append(later)
    df = pd.concat(parts, ignore_index=True)

    if args.corrupt == "pressure_bar":
        df["pressure_kpa"] = (df["pressure_kpa"] / 100).round(5)
    elif args.corrupt == "temp_nulls":
        df.loc[df["machine_id"] < 80, "temp_c"] = None
    elif args.corrupt != "none":
        raise SystemExit(f"unknown corruption {args.corrupt!r}; known: {sorted(CORRUPTIONS)}")

    out = run / "data" / "raw" / "sensors.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    _write(run / "ingest.json", {
        "weeks": args.weeks, "rows": len(df), "machines": int(df["machine_id"].nunique()),
        "corruption": CORRUPTIONS[args.corrupt], "fingerprint": data.data_fingerprint(out),
    })
    print(f"ingested {len(df)} rows ({args.weeks} week(s)), corruption: {CORRUPTIONS[args.corrupt]}")
    if args.run_id_out:
        Path(args.run_id_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.run_id_out).write_text(args.run_id)
    return 0


# --- validate -----------------------------------------------------------------------

def validate(args) -> int:
    """Run the Lab 4 data-contract tests against THIS run's data. Non-zero aborts the run.

    tests/test_data.py reads data/raw/sensors.csv under the repo root and SKIPS when it is
    missing, and a skipped contract is a passed step. So the step refuses to run without
    the file, and counts skips as failures, rather than trusting pytest's exit code alone.
    """
    cfg = config.load()
    run = _run_dir(get_adapter(cfg), args.run_id)
    src = run / "data" / "raw" / "sensors.csv"
    if not src.exists():
        print(f"VALIDATE FAIL  no data at {src}")
        return 1
    dest = ROOT / "data" / "raw" / "sensors.csv"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)

    junit = Path(tempfile.mkdtemp()) / "contract.xml"
    code = subprocess.run(
        ["pytest", "-q", "-m", "contract", "tests/test_data.py", f"--junitxml={junit}"],
        cwd=ROOT,
    ).returncode
    suite = ET.parse(junit).getroot()
    suite = suite if suite.tag == "testsuite" else suite.find("testsuite")
    counts = {k: int(suite.get(k, 0)) for k in ("tests", "failures", "errors", "skipped")}
    _write(run / "validate.json", {"pytest_exit": code, **counts})
    if code != 0 or counts["skipped"] or not counts["tests"]:
        print(f"VALIDATE FAIL  {counts}: aborting before train. A model trained on data "
              "that fails its own contract is one nobody should trust.")
        return 1
    print(f"VALIDATE PASS  {counts}")
    return 0


# --- train --------------------------------------------------------------------------

def train(args) -> int:
    """src.train, unchanged, against this run's data. Then serialise the model it logged.

    The tracking store is local to the container: MLflow's sqlite store on a FUSE mount
    is asking for a locked database. What the pipeline keeps is the model, the metrics
    and the lineage, all written to the run directory.
    """
    import joblib
    import mlflow
    from mlflow import MlflowClient

    cfg = config.load()
    run = _run_dir(get_adapter(cfg), args.run_id)
    tmp = Path(tempfile.mkdtemp())
    env = {**os.environ, "DATA_DIR": str(run / "data"), "PYTHONPATH": str(ROOT),
           "MLFLOW_TRACKING_URI": f"sqlite:///{tmp}/mlflow.db"}
    cmd = [sys.executable, "-m", "src.train",
           "--n-estimators", str(args.n_estimators), "--max-depth", str(args.max_depth),
           "--min-samples-leaf", str(args.min_samples_leaf), "--seed", str(args.seed),
           "--experiment", "itcs355-lab5", "--run-name", args.run_id,
           "--metrics-out", str(run / "metrics.json")]
    subprocess.run(cmd, cwd=tmp, env=env, check=True)

    mlflow.set_tracking_uri(env["MLFLOW_TRACKING_URI"])
    client = MlflowClient()
    exp = client.get_experiment_by_name("itcs355-lab5")
    logged = client.search_logged_models(experiment_ids=[exp.experiment_id])
    model = mlflow.sklearn.load_model(f"models:/{logged[-1].model_id}")
    (run / "model").mkdir(exist_ok=True)
    joblib.dump(model, run / "model" / "model.joblib")

    metrics = json.loads((run / "metrics.json").read_text())
    _write(run / "lineage.json", {
        "pipeline_run": args.run_id,
        "git_commit": os.environ.get("GIT_COMMIT", "unknown"),
        "image": os.environ.get("PIPELINE_IMAGE", "unknown"),
        "data_fingerprint": metrics["data_fingerprint"],
        "data_uri": _run_uri(cfg, args.run_id, "data/raw/sensors.csv"),
        "seed": args.seed,
        "params": {"n_estimators": args.n_estimators, "max_depth": args.max_depth,
                   "min_samples_leaf": args.min_samples_leaf},
        "metrics": {k: v for k, v in metrics.items() if k.endswith("_auc")},
    })
    print(f"model: {model}")
    return 0


# --- evaluate -----------------------------------------------------------------------

def evaluate(args) -> int:
    """scripts/evaluation_gate.py decides. Its exit code becomes the branch condition.

    1 is a decision ("do not register") and the run carries on to skip register and
    deploy. Anything else non-zero is the gate breaking, and the step fails: a gate that
    cannot read the incumbent must not be read as either a pass or a fail.
    """
    cfg = config.load()
    run = _run_dir(get_adapter(cfg), args.run_id)
    (run / "gate.json").unlink(missing_ok=True)
    code = subprocess.run(
        [sys.executable, "scripts/evaluation_gate.py",
         "--metrics", str(run / "metrics.json"),
         "--data", str(run / "data" / "raw" / "sensors.csv"),
         "--report", str(run / "gate.json")],
        cwd=ROOT,
    ).returncode
    # A Python crash also exits 1, so the exit code alone cannot tell "rejected" from
    # "broken". The decision counts only when gate.json agrees with it.
    report = run / "gate.json"
    written = json.loads(report.read_text()).get("decision") if report.exists() else None
    decision = {0: "pass", 1: "fail"}.get(code)
    if decision is None or written != decision:
        print(f"gate errored (exit {code}, gate.json says {written!r}); failing the step "
              "rather than guessing")
        return code or 2
    Path(args.decision_out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.decision_out).write_text(decision)
    print(f"gate_decision = {decision}")
    return 0


# --- register -----------------------------------------------------------------------

def register(args) -> int:
    from cloudlayer import pipelines

    cfg = config.load()
    run = _run_dir(get_adapter(cfg), args.run_id)
    gate = json.loads((run / "gate.json").read_text())
    if gate.get("decision") != "pass":
        # Belt and braces: the DAG's condition should have stopped us already.
        print(f"REFUSING to register: gate.json says {gate.get('decision')!r}")
        return 1
    lineage = json.loads((run / "lineage.json").read_text())
    version = pipelines.register_version(
        cfg, _run_uri(cfg, args.run_id, "model"), cfg.model_registry_name, lineage, gate)
    _write(run / "registered.json", {"model": cfg.model_registry_name, "version": version,
                                     "artifact": _run_uri(cfg, args.run_id, "model")})
    print(f"registered {cfg.model_registry_name} version {version}")
    return 0


# --- deploy -------------------------------------------------------------------------

SMOKE_ROW = {"temp_c": 78.4, "vibration_mm_s": 3.1, "pressure_kpa": 315.2,
             "hours_since_service": 4200, "load_pct": 68.0, "ambient_humidity": 55.0}


def deploy(args) -> int:
    """Deploy the version this run registered to staging, then call it once.

    Staging scales to zero (min 0): a warm minimum instance would bill around the clock
    for a service only the pipeline calls.
    """
    from cloudlayer import pipelines

    cfg = config.load()
    adapter = get_adapter(cfg)
    run = _run_dir(adapter, args.run_id)
    reg = json.loads((run / "registered.json").read_text())
    ref = f"{reg['model']}@{reg['version']}"
    revision = adapter.deploy(ref, args.endpoint, args.instance, image=args.serve_image,
                              min_instances=0, max_instances=1, lab=5)
    url = adapter.endpoint_url(args.endpoint)
    os.environ["SERVICE_ID_TOKEN"] = pipelines.identity_token(cfg, url)
    reply = adapter.invoke(args.endpoint, SMOKE_ROW, url=url)
    _write(run / "deployed.json", {"endpoint": args.endpoint, "revision": revision,
                                   "url": url, "model_ref": ref, "smoke": reply})
    served = str(reply.get("model_version"))
    if reply["status"] != 200 or served != str(reg["version"]):
        print(f"SMOKE FAIL  status {reply['status']}, serving version {served}, "
              f"expected {reg['version']}")
        return 1
    print(f"deployed {ref} to {args.endpoint} ({revision}); smoke 200, "
          f"version {served}, {reply['latency_ms']:.0f} ms")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="step", required=True)

    def step(name):
        p = sub.add_parser(name)
        p.add_argument("--run-id", required=True)
        return p

    p = step("ingest")
    p.add_argument("--weeks", type=int, default=1)
    p.add_argument("--corrupt", default="none", choices=sorted(CORRUPTIONS))
    p.add_argument("--run-id-out", default=None)
    step("validate")
    p = step("train")
    p.add_argument("--n-estimators", type=int, default=100)
    p.add_argument("--max-depth", type=int, default=4)
    p.add_argument("--min-samples-leaf", type=int, default=5)
    p.add_argument("--seed", type=int, default=SEED)
    p = step("evaluate")
    p.add_argument("--decision-out", required=True)
    step("register")
    p = step("deploy")
    p.add_argument("--endpoint", default="itcs355-staging")
    p.add_argument("--instance", default="1cpu-2gi")
    p.add_argument("--serve-image", required=True)
    args = ap.parse_args()
    return globals()[args.step](args)


if __name__ == "__main__":
    raise SystemExit(main())
