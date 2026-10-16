"""Lab 5 — compile pipeline/pipeline.yaml into your provider's pipeline service.

This module is Layer 3, so provider SDKs are allowed here and nowhere else. The DAG stays
in YAML; only the translation lives here. That separation is what you argue for or against
in Lab 5 Task 4.

Implement ONE compile function, for your provider.

    make pipeline                                   # compile, submit, wait (GCP)
    PIPELINE_PARAMS='{"max_depth": 2}' make pipeline
    python -m cloudlayer.pipelines run --param weeks=1 --param corrupt=pressure_bar
    python -m cloudlayer.pipelines schedule         # the cron in pipeline.yaml
    python -m cloudlayer.pipelines schedule --once-in 6   # one-shot, to watch one fire
    python -m cloudlayer.pipelines schedules | teardown

The compile host needs pipeline/requirements-compile.txt (kfp, the Google pipeline
components, the Vertex SDK). The step image does not: it only runs pipeline/steps.py.

The second half of this file is what the steps call when they have to touch the provider:
reading the production version from the registry, registering with lineage, minting a
token to call staging. Those are pipeline concerns the eleven-method adapter does not have,
and they live here rather than in pipeline/steps.py so that file stays provider-neutral.
"""
from __future__ import annotations

import argparse
import contextlib
import inspect
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[1]
SPEC = REPO / "pipeline" / "pipeline.yaml"
EVIDENCE = REPO / "reports" / "lab5" / "pipeline"
IMAGE_FILE = EVIDENCE / "pipeline-image.txt"
SERVE_IMAGE_FILE = REPO / "reports" / "lab3" / "serve-image.txt"
SCHEDULE_PREFIX = "itcs355-lab5-"

# What each placeholder in a step command may be.
_TOKEN = re.compile(r"^\{\{\s*(outputs\.)?([a-z_][a-z0-9_]*)\s*\}\}$")
_CONDITION = re.compile(r"""^\s*([a-z_][a-z0-9_]*)\s*==\s*['"]([^'"]+)['"]\s*$""")


def _defaults() -> None:
    """Values pipeline.yaml references that cloud.env does not carry."""
    os.environ.setdefault("TRAIN_INSTANCE", "n1-standard-4")
    if not os.environ.get("PIPELINE_IMAGE") and IMAGE_FILE.exists():
        os.environ["PIPELINE_IMAGE"] = IMAGE_FILE.read_text().strip()
    if not os.environ.get("SERVE_IMAGE") and SERVE_IMAGE_FILE.exists():
        os.environ["SERVE_IMAGE"] = SERVE_IMAGE_FILE.read_text().strip()


def load_spec(path: Path = SPEC) -> dict[str, Any]:
    """Read the neutral DAG and expand ${VAR} references from the environment."""
    raw = path.read_text()
    for key, value in os.environ.items():
        raw = raw.replace(f"${{{key}}}", value)
    left = sorted(set(re.findall(r"\$\{([A-Z_]+)\}", raw)))
    if left:
        raise RuntimeError(f"pipeline.yaml references unset variables: {left}")
    return yaml.safe_load(raw)


def compile_aws(spec: dict[str, Any]):
    """SageMaker Pipelines.

    Each step becomes a ProcessingStep or TrainingStep; `condition` becomes a
    ConditionStep. Note that SageMaker's condition steps branch rather than abort, so
    `on_failure: abort` needs a FailStep on the else branch — an asymmetry worth
    mentioning in your Lab 5 write-up.
    """
    raise NotImplementedError("TODO Lab 5: build a sagemaker.workflow.pipeline.Pipeline")


def compile_azure(spec: dict[str, Any]):
    """Azure ML Pipelines.

    Each step becomes a command component; the DAG is expressed as a @pipeline function.
    Conditions use azure.ai.ml.dsl.condition, which is closer to this YAML than the other
    two providers' equivalents.
    """
    raise NotImplementedError("TODO Lab 5: build an azure.ai.ml pipeline job")


# --- GCP: YAML -> KFP -> Vertex -------------------------------------------------------

def _component_text(step: dict[str, Any], params: dict[str, Any]) -> tuple[str, list[str], list[str]]:
    """One step as a KFP component definition. Returns (yaml, inputs, outputs).

    The command is copied as it is, with each {{...}} swapped for KFP's placeholder. The
    image and command are the whole component: there is no Python in the DAG.
    """
    inputs, outputs, cmd = [], [], []
    for arg in step["command"]:
        m = _TOKEN.match(str(arg))
        if not m:
            cmd.append(str(arg))
        elif m.group(1):
            outputs.append(m.group(2))
            cmd.append({"outputPath": m.group(2)})
        else:
            name = m.group(2)
            if name != "run_id" and name not in params:
                raise ValueError(f"step {step['name']}: {{{{{name}}}}} is not a parameter")
            inputs.append(name)
            cmd.append({"inputValue": name})
    types = {bool: "Boolean", int: "Integer", float: "Float"}
    comp = {
        "name": step["name"],
        "inputs": [{"name": n, "type": "String" if n == "run_id" else
                    types.get(type(params[n]), "String")} for n in inputs],
        "outputs": [{"name": n, "type": "String"} for n in outputs],
        "implementation": {"container": {"image": step["image"], "command": cmd}},
    }
    return yaml.safe_dump(comp, sort_keys=False), inputs, outputs


def build_kfp(spec: dict[str, Any], cfg) -> Any:
    """The KFP pipeline function for this spec, ready for kfp.compiler."""
    from google_cloud_pipeline_components.v1.custom_job import (
        create_custom_training_job_from_component,
    )
    from kfp import components, dsl

    params: dict[str, Any] = spec.get("parameters", {})
    steps = spec["steps"]
    labels = cfg.tags(int(spec.get("tags", {}).get("lab", 5)))
    env = {k: getattr(cfg, a) for k, a in (
        ("CLOUD_PROVIDER", "provider"), ("PROJECT_ID", "project_id"), ("REGION", "region"),
        ("BLOB_URI", "blob_uri"), ("CONTAINER_REGISTRY", "container_registry"),
        ("MLFLOW_TRACKING_URI", "mlflow_tracking_uri"),
        ("MODEL_REGISTRY_NAME", "model_registry_name"), ("IDENTITY_REF", "identity_ref"))}

    producers: dict[str, str] = {}        # output name -> step that emits it
    built = {}
    for step in steps:
        text, inputs, outputs = _component_text(step, params)
        op = components.load_component_from_text(text)
        if step.get("spot") or step.get("instance"):
            # A plain container task cannot ask for spot. The Google component runs the
            # same container as a Vertex custom job, which can.
            op = create_custom_training_job_from_component(
                op, display_name=f"itcs355-lab5-{step['name']}",
                machine_type=step.get("instance", "n1-standard-4"),
                strategy="SPOT" if step.get("spot") else "STANDARD",
                boot_disk_type="pd-standard", boot_disk_size_gb=100,
                labels=labels,
                env=[{"name": k, "value": v} for k, v in
                     {**env, "PIPELINE_IMAGE": step["image"]}.items()],
            )
        built[step["name"]] = (op, inputs)
        for o in outputs:
            producers[o] = step["name"]

    def conditions_of(step, by_name) -> list[tuple[str, str, str]]:
        """A step's own condition, plus any it inherits from what it depends on."""
        own = []
        if step.get("condition"):
            m = _CONDITION.match(step["condition"])
            if not m or m.group(1) not in producers:
                raise ValueError(f"step {step['name']}: cannot compile condition "
                                 f"{step['condition']!r}")
            own.append((producers[m.group(1)], m.group(1), m.group(2)))
        for dep in step.get("depends_on", []):
            own += [c for c in conditions_of(by_name[dep], by_name) if c not in own]
        return own

    by_name = {s["name"]: s for s in steps}

    def pipeline_fn(*args, **kwargs):
        # KFP calls this positionally, in the order of the signature set below.
        kwargs.update(zip(params, args))
        run_id = dsl.PIPELINE_JOB_NAME_PLACEHOLDER
        tasks: dict[str, Any] = {}
        branch: contextlib.ExitStack | None = None
        open_conds: list = []
        for step in steps:
            conds = conditions_of(step, by_name)
            if conds != open_conds:
                # KFP forbids depending on a task inside a branch from outside it, so
                # consecutive steps sharing a condition share one dsl.If block.
                if branch:
                    branch.close()
                branch = contextlib.ExitStack()
                for src, out, value in conds:
                    branch.enter_context(dsl.If(tasks[src].outputs[out] == value,
                                                name=f"{out}-{value}"))
                open_conds = conds
            op, inputs = built[step["name"]]
            task = op(**{n: (run_id if n == "run_id" else kwargs[n]) for n in inputs})
            task.set_display_name(step["name"])
            task.set_caching_options(False)
            for dep in step.get("depends_on", []):
                task.after(tasks[dep])
            if step.get("retries"):
                task.set_retry(num_retries=int(step["retries"]), backoff_duration="30s")
            if not (step.get("spot") or step.get("instance")):
                task.set_cpu_limit("2").set_memory_limit("8G")
                for k, v in {**env, "PIPELINE_IMAGE": step["image"]}.items():
                    task.set_env_variable(k, v)
            tasks[step["name"]] = task
        if branch:
            branch.close()

    pipeline_fn.__signature__ = inspect.Signature([
        inspect.Parameter(k, inspect.Parameter.POSITIONAL_OR_KEYWORD, default=v, annotation=type(v))
        for k, v in params.items()])
    pipeline_fn.__annotations__ = {k: type(v) for k, v in params.items()}
    pipeline_fn.__name__ = spec["name"].replace("-", "_")
    return dsl.pipeline(name=spec["name"], description="ITCS355 Lab 5 retraining pipeline")(
        pipeline_fn)


def compile_gcp(spec: dict[str, Any], submit: bool = True, wait: bool = True,
                params: dict[str, Any] | None = None):
    """Vertex AI Pipelines.

    Kubeflow Pipelines underneath: steps become container components, conditions become
    dsl.If blocks. The compiled artifact is a JSON file you submit, so your build step
    produces a file rather than an object — different from the other two.

    `make pipeline` lands here with no arguments: compile, submit, wait, print the
    outcome. Run parameters come from PIPELINE_PARAMS (JSON) when called that way.
    """
    from kfp import compiler

    from src import config

    cfg = config.load()
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    out = EVIDENCE / "pipeline.json"
    compiler.Compiler().compile(build_kfp(spec, cfg), str(out))
    print(f"compiled {out}")
    if not submit:
        return str(out)
    if params is None:
        params = json.loads(os.environ.get("PIPELINE_PARAMS") or "{}")
    job = submit_gcp(cfg, out, params)
    return wait_gcp(job) if wait else job.resource_name


def _aiplatform(cfg):
    from google.cloud import aiplatform

    aiplatform.init(project=cfg.project_id, location=cfg.region)
    return aiplatform


def _service_account() -> str | None:
    # Unset means the project's Compute Engine default service account, for interactive
    # and scheduled runs alike. Say it, rather than let it be a surprise.
    return os.environ.get("PIPELINE_SA") or None


def _job(cfg, template: Path, params: dict[str, Any], display: str | None = None):
    aip = _aiplatform(cfg)
    return aip.PipelineJob(
        display_name=display or f"itcs355-lab5-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}",
        template_path=str(template),
        pipeline_root=f"{cfg.blob_uri.rstrip('/')}/lab5-pipeline/_root",
        parameter_values=params,
        labels=cfg.tags(5),
        enable_caching=False,
    )


def submit_gcp(cfg, template: Path, params: dict[str, Any]):
    job = _job(cfg, template, params)
    job.submit(service_account=_service_account())
    print(f"submitted {job.resource_name}")
    print(f"  params  {json.dumps(params)}")
    print(f"  console {job._dashboard_uri()}")
    return job


def describe_run(job) -> dict[str, Any]:
    """What the run did, step by step, as Vertex recorded it."""
    g = job.gca_resource
    steps = []
    for t in g.job_detail.task_details:
        if t.task_name.endswith("-driver") or t.task_name == job.name:
            continue
        steps.append({
            "task": t.task_name, "state": t.state.name,
            "start": t.start_time.isoformat() if t.start_time else None,
            "end": t.end_time.isoformat() if t.end_time else None,
            "error": t.error.message if t.error and t.error.message else "",
        })
    return {
        "run": job.resource_name, "display_name": g.display_name, "state": g.state.name,
        "create": g.create_time.isoformat() if g.create_time else None,
        "start": g.start_time.isoformat() if g.start_time else None,
        "end": g.end_time.isoformat() if g.end_time else None,
        "service_account": g.service_account,
        "schedule": getattr(g, "schedule_name", ""),
        "params": dict(g.runtime_config.parameter_values),
        "labels": dict(g.labels),
        "console": job._dashboard_uri(),
        "error": g.error.message if g.error and g.error.message else "",
        "steps": sorted(steps, key=lambda s: s["start"] or "~"),
    }


def wait_gcp(job) -> dict[str, Any]:
    """Block until the run ends; print and save what each step did."""
    try:
        job.wait()
    except RuntimeError as e:          # a failed run raises; it is still evidence
        print(f"run ended in failure: {str(e).splitlines()[0][:200]}")
    job._sync_gca_resource()
    rec = describe_run(job)
    for s in rec["steps"]:
        print(f"  {s['task']:28} {s['state']:10} {s['error'][:90]}")
    print(f"{rec['state']}  {rec['console']}")
    path = EVIDENCE / "runs" / f"{job.name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rec, indent=2, default=str))
    print(f"saved {path}")
    return rec


def schedule_gcp(cfg, template: Path, cron: str, params: dict[str, Any], name: str,
                 max_runs: int | None = None):
    job = _job(cfg, template, params, display=name)
    sched = job.create_schedule(display_name=name, cron=cron, max_concurrent_run_count=1,
                                max_run_count=max_runs, service_account=_service_account())
    print(f"schedule {sched.resource_name}\n  cron {cron}  max_runs {max_runs}")
    return sched


def list_schedules(cfg) -> list[Any]:
    from google.cloud.aiplatform import PipelineJobSchedule

    _aiplatform(cfg)
    return [s for s in PipelineJobSchedule.list()
            if s.display_name.startswith(SCHEDULE_PREFIX)]


def teardown(cfg) -> list[str]:
    """Vertex pipeline schedules are not Cloud Scheduler jobs, so the adapter's teardown
    does not see them. This deletes the ones this lab created."""
    acted = []
    for s in list_schedules(cfg):
        s.delete()
        acted.append(f"deleted   Vertex pipeline schedule {s.display_name}")
    return acted


COMPILERS = {"aws": compile_aws, "azure": compile_azure, "gcp": compile_gcp}


def compile_for(provider: str, spec: dict[str, Any] | None = None):
    _defaults()
    spec = spec or load_spec()
    try:
        compiler = COMPILERS[provider.lower()]
    except KeyError:
        raise ValueError(f"No pipeline compiler for {provider!r}") from None
    return compiler(spec)


# --- Called from inside the step image -------------------------------------------------
#
# These run in pipeline/steps.py, on the pipeline's own identity, with gcloud and nothing
# else from Google installed.

def _gcloud(*args: str) -> str:
    out = subprocess.run(["gcloud", *args], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"gcloud {' '.join(args[:3])} failed: {out.stderr.strip()[-600:]}")
    return out.stdout.strip()


def _require_gcp(cfg) -> None:
    if cfg.provider != "gcp":
        raise NotImplementedError(f"pipeline registry helpers exist for gcp only, not {cfg.provider}")


def _model_id(cfg, name: str) -> str | None:
    ids = _gcloud("ai", "models", "list", f"--region={cfg.region}", f"--project={cfg.project_id}",
                  f"--filter=displayName={name}", "--format=value(name)", "--quiet").split()
    if len(ids) > 1:
        raise RuntimeError(f"{len(ids)} models named {name!r}; the incumbent is ambiguous")
    return ids[0].rsplit("/", 1)[-1] if ids else None


def incumbent(cfg, name: str) -> dict[str, Any] | None:
    """The production version: the one the registry's `default` alias points at.

    `default` is what Vertex itself serves when nobody names a version. The pipeline moves
    it only when the gate passes, so it is always the last model that beat its predecessor.
    """
    _require_gcp(cfg)
    model_id = _model_id(cfg, name)
    if model_id is None:
        return None
    v = json.loads(_gcloud("ai", "models", "describe", f"{model_id}@default",
                           f"--region={cfg.region}", f"--project={cfg.project_id}",
                           "--format=json", "--quiet"))
    return {"model": name, "model_id": model_id, "version": v["versionId"],
            "aliases": v.get("versionAliases", []), "artifact_uri": v["artifactUri"]}


def _label(value: Any) -> str:
    return re.sub(r"[^a-z0-9_-]", "-", str(value).lower())[:63]


def register_version(cfg, artifact_uri: str, name: str, lineage: dict[str, Any],
                     gate: dict[str, Any]) -> str:
    """Upload a new version under `name`, carrying its lineage, and make it the default.

    Labels hold what fits a label (lowercase, 63 characters); the full lineage, image digest
    included, goes in the version description, which is free text.
    """
    from cloudlayer.factory import get_adapter

    _require_gcp(cfg)
    parent = _model_id(cfg, name)
    labels = {**cfg.tags(5),
              "pipeline_run": _label(lineage["pipeline_run"]),
              "git_commit": _label(lineage["git_commit"]),
              "data_fingerprint": _label(lineage["data_fingerprint"]),
              "gate": "pass"}
    description = json.dumps({**lineage, "gate": {k: gate.get(k) for k in
                              ("metric", "candidate", "incumbent", "delta", "reason")}})
    cmd = ["ai", "models", "upload", f"--region={cfg.region}", f"--project={cfg.project_id}",
           f"--display-name={name}", f"--artifact-uri={artifact_uri}",
           f"--container-image-uri={get_adapter(cfg)._serving_image()}",
           "--labels=" + ",".join(f"{k}={v}" for k, v in labels.items()),
           f"--version-description={description[:2000]}",
           # Aliases are unique within a model, so naming `default` here moves it off the
           # incumbent and onto this version.
           "--version-aliases=default", "--quiet"]
    if parent:
        cmd.append(f"--parent-model=projects/{cfg.project_id}/locations/{cfg.region}/models/{parent}")
    _gcloud(*cmd)
    model_id = parent or _model_id(cfg, name)
    newest = _gcloud("ai", "models", "list-version", model_id, f"--region={cfg.region}",
                     f"--project={cfg.project_id}", "--sort-by=~versionCreateTime",
                     "--limit=1", "--format=value(versionId)").strip()
    now = incumbent(cfg, name)
    if not now or now["version"] != newest:
        raise RuntimeError(f"uploaded version {newest} but default is {now and now['version']}")
    return newest


def identity_token(cfg, audience: str) -> str:
    """An ID token for calling a private Cloud Run service as whoever this step runs as.

    On Vertex that is a service account, and the metadata server mints tokens for any
    audience. `gcloud auth print-identity-token` would mint one for the wrong audience
    there; it is the fallback for a person at a laptop, where Cloud Run accepts it.
    """
    _require_gcp(cfg)
    url = ("http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/"
           f"default/identity?audience={audience}")
    try:
        req = urllib.request.Request(url, headers={"Metadata-Flavor": "Google"})
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.read().decode()
    except OSError:
        return _gcloud("auth", "print-identity-token")


# --- CLI ------------------------------------------------------------------------------

def _params(pairs: list[str], spec: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for pair in pairs:
        k, _, v = pair.partition("=")
        default = spec.get("parameters", {}).get(k)
        if default is None:
            raise SystemExit(f"unknown parameter {k!r}")
        out[k] = type(default)(v)
    return out


def main() -> int:
    from src import config

    ap = argparse.ArgumentParser(description="Lab 5 pipeline: compile, run, schedule")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("compile")
    r = sub.add_parser("run")
    r.add_argument("--param", action="append", default=[], help="name=value")
    r.add_argument("--no-wait", action="store_true")
    s = sub.add_parser("schedule")
    s.add_argument("--param", action="append", default=[])
    s.add_argument("--once-in", type=int, default=None,
                   help="a one-shot schedule firing in N minutes, to watch one run")
    w = sub.add_parser("wait")
    w.add_argument("run")
    sub.add_parser("schedules")
    sub.add_parser("teardown")
    args = ap.parse_args()

    _defaults()
    cfg = config.load()
    spec = load_spec()

    if args.cmd == "compile":
        compile_gcp(spec, submit=False)
    elif args.cmd == "run":
        compile_gcp(spec, wait=not args.no_wait, params=_params(args.param, spec))
    elif args.cmd == "wait":
        aip = _aiplatform(cfg)
        wait_gcp(aip.PipelineJob.get(args.run))
    elif args.cmd == "schedule":
        template = Path(compile_gcp(spec, submit=False))
        params = _params(args.param, spec)
        if args.once_in:
            at = datetime.now(timezone.utc) + timedelta(minutes=args.once_in)
            cron = f"TZ=UTC {at.minute} {at.hour} {at.day} {at.month} *"
            schedule_gcp(cfg, template, cron, params, f"{SCHEDULE_PREFIX}fire-once", max_runs=1)
        else:
            schedule_gcp(cfg, template, spec["schedule"], params, f"{SCHEDULE_PREFIX}retrain")
    elif args.cmd == "schedules":
        for sch in list_schedules(cfg):
            g = sch.gca_resource
            print(json.dumps({"name": sch.resource_name, "display_name": g.display_name,
                              "cron": g.cron, "state": g.state.name,
                              "started_runs": g.started_run_count,
                              "next_run": g.next_run_time.isoformat() if g.next_run_time else None,
                              "last_run": g.last_scheduled_run_response.run_response
                              if g.last_scheduled_run_response else None,
                              "service_account": g.create_pipeline_job_request.pipeline_job
                              .service_account}, indent=2, default=str))
    elif args.cmd == "teardown":
        for line in teardown(cfg):
            print(" ", line)
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(REPO))
    raise SystemExit(main())
