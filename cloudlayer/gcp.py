"""GCP adapter. Implements upload/download/push_image for Lab 1.

Lab 1 drives the gcloud and docker CLIs rather than the Python SDK. Both are already
prerequisites (`make cloud-check` checks gcloud), and it keeps google-cloud-* out of
the training image's lock file — the image never talks to GCP.

Notes:
  * BLOB_URI looks like gs://bucket/prefix — parsed here, never in src/.
  * Artifact Registry paths are region-scoped:
        <region>-docker.pkg.dev/<project>/<repo>/<image>
    Not gcr.io; that is a different service.
  * push_image returns the digest reference, not the tag.
  * GCP calls them labels, not tags, and they must be lowercase with no spaces.
    cfg.tags(1) already satisfies that constraint.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from cloudlayer.base import CloudAdapter

# Vertex mounts every bucket the job can read at /gcs/<bucket>/<object path>, so a job
# reads BLOB_URI through an ordinary filesystem path and the training image needs no
# Google SDK at all. Nothing else in the repo may know this prefix.
GCS_FUSE_ROOT = "/gcs"

TERMINAL_STATES = {
    "JOB_STATE_SUCCEEDED",
    "JOB_STATE_FAILED",
    "JOB_STATE_CANCELLED",
    "JOB_STATE_EXPIRED",
}


def _run(cmd: list[str]) -> str:
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed ({out.returncode}):\n{out.stderr.strip()}")
    return out.stdout.strip()


class GcpAdapter(CloudAdapter):
    def _blob_uri(self, key: str) -> str:
        base = self.cfg.blob_uri.rstrip("/")
        if not base.startswith("gs://"):
            raise ValueError(f"BLOB_URI must be gs://bucket/prefix for GCP, got {base!r}")
        return f"{base}/{key.lstrip('/')}"

    def upload(self, local_path: str, key: str) -> str:
        uri = self._blob_uri(key)
        _run(["gcloud", "storage", "cp", str(local_path), uri, "--quiet"])
        return uri

    def download(self, uri: str, local_path: str) -> None:
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        if shutil.which("gcloud"):
            _run(["gcloud", "storage", "cp", uri, str(local_path), "--quiet"])
        else:
            self._download_rest(uri, local_path)

    def _download_rest(self, uri: str, local_path: str) -> None:
        """Download without gcloud or an SDK — the serving container has neither.

        Inside Cloud Run (or any GCP compute) the metadata server hands out a token for
        the runtime service account; the object comes from the JSON API with it. Lab 3's
        container reaches this through download(), so service/ never sees a gs:// detail.
        """
        if not uri.startswith("gs://"):
            raise ValueError(f"expected gs://bucket/object, got {uri!r}")
        bucket, _, obj = uri.removeprefix("gs://").partition("/")
        meta = urllib.request.Request(
            "http://metadata.google.internal/computeMetadata/v1/instance/"
            "service-accounts/default/token",
            headers={"Metadata-Flavor": "Google"},
        )
        with urllib.request.urlopen(meta, timeout=10) as r:
            token = json.load(r)["access_token"]
        url = (f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/"
               f"{urllib.parse.quote(obj, safe='')}?alt=media")
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=60) as r, open(local_path, "wb") as fh:
            shutil.copyfileobj(r, fh)

    def download_prefix(self, key: str, local_dir: str) -> int:
        dest = Path(local_dir)
        dest.mkdir(parents=True, exist_ok=True)
        src = self._blob_uri(key).rstrip("/")
        # rsync, not cp: the study is rerun and this is called again.
        _run(["gcloud", "storage", "rsync", "--recursive", src, str(dest), "--quiet"])
        return sum(1 for _ in dest.rglob("*") if _.is_file())

    def push_image(self, local_tag: str) -> str:
        registry = self.cfg.container_registry.rstrip("/")
        if ".pkg.dev/" not in registry:
            raise ValueError(
                f"CONTAINER_REGISTRY must be <region>-docker.pkg.dev/<project>/<repo>, got {registry!r}"
            )
        host = registry.split("/", 1)[0]
        name = local_tag.rsplit("/", 1)[-1]          # "itcs355-lab1:<sha>"
        remote = f"{registry}/{name}"
        repo = remote.rsplit(":", 1)[0]

        _run(["gcloud", "auth", "configure-docker", host, "--quiet"])
        _run(["docker", "tag", local_tag, remote])
        _run(["docker", "push", remote])

        # The pushed digest is recorded in RepoDigests; pick the entry for this repo.
        digests = _run(["docker", "inspect", "--format",
                        "{{range .RepoDigests}}{{println .}}{{end}}", remote]).splitlines()
        for ref in digests:
            if ref.startswith(repo + "@sha256:"):
                return ref
        raise RuntimeError(f"pushed {remote} but found no digest for {repo} in {digests}")

    # --- Lab 2 ---------------------------------------------------------------

    def mount_path(self, key: str = "") -> str:
        """Translate BLOB_URI (+ key) into the path Vertex mounts inside the job.

        gs://bucket/prefix + "data" -> /gcs/bucket/prefix/data
        """
        base = self.cfg.blob_uri.rstrip("/")
        if not base.startswith("gs://"):
            raise ValueError(f"BLOB_URI must be gs://bucket/prefix for GCP, got {base!r}")
        path = f"{GCS_FUSE_ROOT}/{base.removeprefix('gs://')}"
        return f"{path}/{key.lstrip('/')}" if key else path

    def submit_training(self, image_uri: str, args: dict[str, Any]) -> str:
        """Submit a Vertex custom training job. Returns the job resource name.

        Four things, as every provider wants: an image, a command, an instance type, and
        an identity. The identity is implicit here — the job runs as the project's
        Compute Engine default service account unless `service_account` is passed — and
        that is exactly where the first submission fails, because it is NOT the identity
        that submitted the job.

        Expected keys in `args`:
            command       list[str]  entrypoint override (default: the image's own)
            job_args      list[str]  arguments after the command
            machine_type  str        default n1-standard-4
            spot          bool       default True — discounted compute, Lab 2 requires it
            env           dict       environment variables for the container
            display_name  str        shown in the console
            lab           int        which lab this resource belongs to, for teardown
        """
        spec: dict[str, Any] = {
            "workerPoolSpecs": [{
                "machineSpec": {"machineType": args.get("machine_type", "n1-standard-4")},
                "replicaCount": 1,
                "containerSpec": {
                    "imageUri": image_uri,
                    "env": [{"name": k, "value": str(v)} for k, v in args.get("env", {}).items()],
                },
            }],
            # SPOT is the whole point of Task 2: roughly a third of on-demand, in exchange
            # for the platform reclaiming the machine whenever it wants. Checkpoint or lose.
            "scheduling": {"strategy": "SPOT" if args.get("spot", True) else "STANDARD"},
        }
        container = spec["workerPoolSpecs"][0]["containerSpec"]
        if args.get("command"):
            container["command"] = list(args["command"])
        if args.get("job_args"):
            container["args"] = [str(a) for a in args["job_args"]]
        if args.get("service_account"):
            spec["serviceAccount"] = args["service_account"]

        labels = self.cfg.tags(args.get("lab", 2))
        display_name = args.get("display_name", f"itcs355-lab{labels['lab']}")

        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            yaml.safe_dump(spec, fh, sort_keys=False)
            config_path = fh.name

        name = _run([
            "gcloud", "ai", "custom-jobs", "create",
            f"--region={self.cfg.region}",
            f"--project={self.cfg.project_id}",
            f"--display-name={display_name}",
            f"--config={config_path}",
            "--labels=" + ",".join(f"{k}={v}" for k, v in labels.items()),
            "--format=value(name)",
            "--quiet",
        ])
        # gcloud prints the resource name on stdout, but older versions print it only in
        # the human-readable banner on stderr. Fail loudly rather than return something
        # that is not a job id.
        if "/customJobs/" not in name:
            raise RuntimeError(f"could not read a job name out of gcloud output: {name!r}")
        return name.strip()

    def wait_training(self, job_id: str) -> dict[str, Any]:
        """Poll until the job reaches a terminal state. Returns what the run cost you.

        Returns state, the three timestamps Vertex records, and wall-clock seconds
        measured from startTime — not from createTime, because queueing for a spot
        machine is not compute you were billed for.
        """
        poll_s = 20
        while True:
            raw = _run([
                "gcloud", "ai", "custom-jobs", "describe", job_id,
                f"--region={self.cfg.region}",
                f"--project={self.cfg.project_id}",
                "--format=json",
            ])
            job = json.loads(raw)
            state = job.get("state", "JOB_STATE_UNSPECIFIED")
            if state in TERMINAL_STATES:
                break
            print(f"  {state} ... polling again in {poll_s}s")
            time.sleep(poll_s)

        def _ts(key: str):
            value = job.get(key)
            return datetime.fromisoformat(value) if value else None

        start, end = _ts("startTime"), _ts("endTime")
        duration_s = (end - start).total_seconds() if start and end else None
        worker = job["jobSpec"]["workerPoolSpecs"][0]
        return {
            "job_id": job_id,
            "state": state,
            "succeeded": state == "JOB_STATE_SUCCEEDED",
            "create_time": job.get("createTime"),
            "start_time": job.get("startTime"),
            "end_time": job.get("endTime"),
            "duration_s": duration_s,
            "machine_type": worker["machineSpec"]["machineType"],
            "spot": job["jobSpec"].get("scheduling", {}).get("strategy") == "SPOT",
            "error": job.get("error", {}).get("message", ""),
        }

    def register_model(self, model_uri: str, name: str) -> str:
        """Upload a model to the Vertex AI Model Registry. Returns its version id.

        `model_uri` is a gs:// DIRECTORY containing the serialised model, named the way
        the prebuilt serving container expects (model.joblib for scikit-learn). Vertex
        wants a serving container even when nothing is being served yet, because a model
        in its registry is defined as something deployable.

        Uploading a second version under the same display name requires the parent
        model's id, otherwise you silently get a second, unrelated model rather than
        version 2 of the first — which is how registries quietly stop being registries.
        """
        labels = self.cfg.tags(2)
        existing = _run([
            "gcloud", "ai", "models", "list",
            f"--region={self.cfg.region}", f"--project={self.cfg.project_id}",
            f"--filter=displayName={name}", "--format=value(name)", "--quiet",
        ]).splitlines()

        cmd = [
            "gcloud", "ai", "models", "upload",
            f"--region={self.cfg.region}", f"--project={self.cfg.project_id}",
            f"--display-name={name}",
            f"--artifact-uri={model_uri}",
            f"--container-image-uri={self._serving_image()}",
            "--labels=" + ",".join(f"{k}={v}" for k, v in labels.items()),
            "--format=value(model)", "--quiet",
        ]
        if existing:
            cmd.append(f"--parent-model={existing[0].strip()}")

        out = _run(cmd).strip()
        if not out:
            raise RuntimeError("model upload returned nothing")
        # gcloud prints the model resource name, sometimes with @<version> and sometimes
        # without. The caller was promised a VERSION, so ask for it rather than parse it
        # out of whatever this gcloud release happens to print.
        if "@" in out:
            return out.rsplit("@", 1)[-1]
        return _run([
            "gcloud", "ai", "models", "describe", out,
            f"--region={self.cfg.region}", f"--project={self.cfg.project_id}",
            "--format=value(versionId)",
        ]).strip()

    # Prebuilt serving containers are published to three MULTI-REGION hosts, not to
    # per-region ones. Asking for asia-southeast1-docker.pkg.dev/vertex-ai/prediction
    # fails as FAILED_PRECONDITION naming the Vertex service agent, which reads exactly
    # like an IAM problem and is not one — the repository does not exist. Uploading with
    # an image from our own registry succeeds with the same agent, which is how to tell
    # the two apart.
    _CONTAINER_HOSTS = {"us": "us", "northamerica": "us", "southamerica": "us",
                        "europe": "europe", "me": "europe",
                        "asia": "asia", "australia": "asia"}

    def _serving_image(self) -> str:
        """Prebuilt scikit-learn serving container nearest this region.

        Pinned by minor version deliberately: "latest" on the sklearn MINOR version
        would change what a registered model deserialises with, which is the failure
        Lab 2 Task 5 is looking for.
        """
        continent = self.cfg.region.split("-")[0]
        host = self._CONTAINER_HOSTS.get(continent, "us")
        return f"{host}-docker.pkg.dev/vertex-ai/prediction/sklearn-cpu.1-5:latest"


    # --- Lab 3 ---------------------------------------------------------------
    #
    # Target: Cloud Run, not a Vertex endpoint. Cloud Run forwards every path unchanged
    # (so /predict/batch is reachable, which a Vertex endpoint's single predict route is
    # not), splits traffic across revisions natively, and bills per instance-second.
    # The model still comes from the Vertex Model Registry by version: deploy() resolves
    # name@version to the artifact the registry recorded and hands the container that
    # location. The container fetches it once, at startup, through download() above.

    _INSTANCE = re.compile(r"^(\d+)cpu-(\d+)gi$")

    def _model_artifact(self, name: str, version: str) -> str:
        ids = _run([
            "gcloud", "ai", "models", "list",
            f"--region={self.cfg.region}", f"--project={self.cfg.project_id}",
            f"--filter=displayName={name}", "--format=value(name)", "--quiet",
        ]).splitlines()
        if len(ids) != 1:
            raise RuntimeError(f"expected one registered model named {name!r}, found {len(ids)}")
        model_id = ids[0].strip().rsplit("/", 1)[-1]
        uri = _run([
            "gcloud", "ai", "models", "describe", f"{model_id}@{version}",
            f"--region={self.cfg.region}", f"--project={self.cfg.project_id}",
            "--format=value(artifactUri)", "--quiet",
        ]).strip()
        if not uri.startswith("gs://"):
            raise RuntimeError(f"{name}@{version} has no gs:// artifact (got {uri!r})")
        return uri

    def deploy(self, model_ref: str, endpoint: str, instance: str, *,
               image: str | None = None, traffic: bool = True, tag: str | None = None,
               min_instances: int = 1, max_instances: int = 1) -> str:
        """Deploy registry version `model_ref` ("name@version") as a new revision of the
        Cloud Run service `endpoint`. Returns the revision name.

        instance      "<n>cpu-<m>gi", e.g. "1cpu-2gi". One uvicorn worker per vCPU.
        traffic       False deploys the revision with no traffic (a canary waiting for
                      set_traffic); True sends it 100%.
        min/max       1/1 by default: one always-warm instance, so a load test measures
                      one instance's capacity rather than the autoscaler's, and p99 is
                      not a cold start. Scale-to-zero is measured separately (min 0).

        Probes are where this platform's route difference lives: the startup probe hits
        /ready, so no traffic reaches a revision whose model has not loaded; liveness
        hits /health, so a slow request never gets a healthy container killed.
        """
        name, _, version = model_ref.partition("@")
        if not version:
            raise ValueError(f"model_ref must be name@version, got {model_ref!r}")
        m = self._INSTANCE.match(instance)
        if not m:
            raise ValueError(f"instance must look like 1cpu-2gi, got {instance!r}")
        cpu, mem = m.group(1), m.group(2)
        image = image or os.environ.get("SERVE_IMAGE", "")
        if "@sha256:" not in image:
            raise ValueError(f"deploy a digest-pinned image, got {image!r}")

        artifact = self._model_artifact(name, version)
        labels = self.cfg.tags(3)
        env = {
            "CLOUD_PROVIDER": "gcp",
            "MODEL_URI": artifact,
            "MODEL_VERSION": version,
            "MODEL_REF": model_ref,
            "WEB_CONCURRENCY": cpu,
        }
        suffix = f"v{version}-{cpu}cpu-{time.strftime('%H%M%S')}"
        cmd = [
            "gcloud", "run", "deploy", endpoint,
            f"--image={image}",
            f"--region={self.cfg.region}", f"--project={self.cfg.project_id}",
            f"--cpu={cpu}", f"--memory={mem}Gi",
            "--concurrency=80",
            f"--min-instances={min_instances}", f"--max-instances={max_instances}",
            # Instance-based billing: CPU stays allocated between requests. The cost
            # figures in Task 5 assume exactly this, so it is set, not defaulted.
            "--no-cpu-throttling",
            "--no-allow-unauthenticated",
            f"--revision-suffix={suffix}",
            "--set-env-vars=" + ",".join(f"{k}={v}" for k, v in env.items()),
            "--labels=" + ",".join(f"{k}={v}" for k, v in labels.items()),
            "--startup-probe=httpGet.path=/ready,periodSeconds=2,failureThreshold=90,timeoutSeconds=2",
            "--liveness-probe=httpGet.path=/health,periodSeconds=15,timeoutSeconds=3",
            "--quiet", "--format=value(status.latestCreatedRevisionName)",
        ]
        if not traffic:
            cmd.append("--no-traffic")
        if tag:
            cmd.append(f"--tag={tag}")
        return _run(cmd).strip() or f"{endpoint}-{suffix}"

    def endpoint_url(self, endpoint: str, tag: str | None = None) -> str:
        url = _run([
            "gcloud", "run", "services", "describe", endpoint,
            f"--region={self.cfg.region}", f"--project={self.cfg.project_id}",
            "--format=value(status.url)", "--quiet",
        ]).strip()
        if tag:  # https://svc-xyz.a.run.app -> https://tag---svc-xyz.a.run.app
            url = url.replace("https://", f"https://{tag}---", 1)
        return url

    _token: tuple[float, str] | None = None

    def auth_header(self) -> dict[str, str]:
        """The service is not public. An identity token for the caller, cached ~50 min."""
        now = time.time()
        if not self._token or now - self._token[0] > 3000:
            self._token = (now, _run(["gcloud", "auth", "print-identity-token"]).strip())
        return {"Authorization": f"Bearer {self._token[1]}"}

    def invoke(self, endpoint: str, payload: dict[str, Any], *,
               url: str | None = None) -> dict[str, Any]:
        """POST one payload. A payload with "rows" goes to /predict/batch.

        Returns the status, the body, the model version the SERVICE reported (header),
        and client-side latency — the caller decides what counts as a failure.
        """
        base = url or self.endpoint_url(endpoint)
        path = "/predict/batch" if "rows" in payload else "/predict"
        req = urllib.request.Request(
            base + path, data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json", **self.auth_header()},
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                status, headers, raw = r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            status, headers, raw = e.code, e.headers, e.read()
        latency_ms = (time.perf_counter() - started) * 1000
        try:
            body = json.loads(raw)
        except ValueError:
            body = {"raw": raw.decode(errors="replace")[:500]}
        return {
            "status": status,
            "body": body,
            "model_version": headers.get("x-model-version"),
            "request_id": headers.get("x-request-id"),
            "server_timing": headers.get("server-timing"),
            "latency_ms": round(latency_ms, 2),
        }

    def traffic(self, endpoint: str) -> list[dict[str, Any]]:
        raw = _run([
            "gcloud", "run", "services", "describe", endpoint,
            f"--region={self.cfg.region}", f"--project={self.cfg.project_id}",
            "--format=json(status.traffic)", "--quiet",
        ])
        return json.loads(raw).get("status", {}).get("traffic", [])

    def set_traffic(self, endpoint: str, split: dict[str, int]) -> dict[str, Any]:
        """Move traffic between revisions: {"<revision>": 90, "<revision>": 10}.

        Returns the split the platform reports AFTER the change, with the UTC time the
        call returned — rollback evidence is this record, not the command that was run.
        """
        if sum(split.values()) != 100:
            raise ValueError(f"traffic must sum to 100, got {split}")
        _run([
            "gcloud", "run", "services", "update-traffic", endpoint,
            f"--region={self.cfg.region}", f"--project={self.cfg.project_id}",
            "--to-revisions=" + ",".join(f"{k}={v}" for k, v in split.items()),
            "--quiet",
        ])
        return {"at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "requested": split, "reported": self.traffic(endpoint)}

    def _serving_services(self, lab: str) -> list[str]:
        return _run([
            "gcloud", "run", "services", "list",
            f"--region={self.cfg.region}", f"--project={self.cfg.project_id}",
            f"--filter=metadata.labels.lab={lab}", "--format=value(metadata.name)", "--quiet",
        ]).split()

    def teardown(self, tags: dict[str, str], purge: bool = False) -> list[str]:
        """Stop and remove what these tags cover. Returns what was acted on.

        What actually costs money is a job that is still running, so anything not in a
        terminal state is cancelled first.

        Finished jobs are metadata, not compute, and they are free. They are also the
        far end of a lineage edge: a registered model version carries training_job_id,
        and deleting the job it names turns "which job produced this model" into a
        dangling reference. So a SUCCEEDED job is kept unless you ask for `purge`, and
        failed or cancelled ones — which nothing points at — are removed.

        Deletion is asynchronous. A successful return means accepted, not gone.

        Lab 3: a Cloud Run service carrying the lab label is deleted outright. It has a
        warm minimum instance, so it bills by the hour whether or not anyone calls it,
        and nothing points at it — the registry version it served stays registered.
        """
        acted: list[str] = []
        for svc in self._serving_services(tags.get("lab", "")):
            _run(["gcloud", "run", "services", "delete", svc,
                  f"--region={self.cfg.region}", f"--project={self.cfg.project_id}",
                  "--quiet"])
            acted.append(f"deleted   Cloud Run service {svc}")

        listed = _run([
            "gcloud", "ai", "custom-jobs", "list",
            f"--region={self.cfg.region}", f"--project={self.cfg.project_id}",
            "--format=value(name,state,labels.lab)", "--quiet",
        ]).splitlines()

        for line in listed:
            parts = line.split("\t")
            if len(parts) < 3 or parts[2] != tags.get("lab"):
                continue
            name, state = parts[0], parts[1]
            if state not in TERMINAL_STATES:
                _run(["gcloud", "ai", "custom-jobs", "cancel", name,
                      f"--region={self.cfg.region}", f"--project={self.cfg.project_id}",
                      "--quiet"])
                acted.append(f"cancelled {name.rsplit('/', 1)[-1]} ({state})")
                continue
            if state == "JOB_STATE_SUCCEEDED" and not purge:
                acted.append(f"kept      {name.rsplit('/', 1)[-1]} (SUCCEEDED — lineage)")
                continue
            self._delete_custom_job(name)
            acted.append(f"deleted   {name.rsplit('/', 1)[-1]} ({state})")
        return acted

    def _delete_custom_job(self, name: str) -> None:
        """Delete a custom job.

        `gcloud ai custom-jobs` can cancel but not delete — the verb only exists on the
        REST surface. This is the one place in the adapter that reaches past the CLI,
        and it is why the seam exists: the caller asked to tear down, not to know that
        one of eleven operations is missing from a command-line tool.
        """
        token = _run(["gcloud", "auth", "print-access-token"])
        endpoint = f"https://{self.cfg.region}-aiplatform.googleapis.com/v1/{name}"
        out = subprocess.run(
            ["curl", "-sS", "-X", "DELETE", endpoint,
             "-H", f"Authorization: Bearer {token}"],
            capture_output=True, text=True,
        )
        if out.returncode != 0 or '"error"' in out.stdout:
            raise RuntimeError(f"delete {name} failed: {out.stdout or out.stderr}")

    # emit_metric                       -> Lab 4 (Cloud Monitoring time series)
    # generate                          -> Lab 5 (managed LLM endpoint; read usageMetadata for tokens)
    # teardown                          -> Lab 5 (filter resources by label)
