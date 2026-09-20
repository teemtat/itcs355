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
import subprocess
import tempfile
import time
from datetime import datetime
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
        _run(["gcloud", "storage", "cp", uri, str(local_path), "--quiet"])

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

        out = _run(cmd)
        # gcloud prints projects/<n>/locations/<r>/models/<id>@<version>
        version = out.strip().rsplit("@", 1)[-1] if "@" in out else out.strip()
        if not version:
            raise RuntimeError(f"model uploaded but no version came back: {out!r}")
        return version

    def _serving_image(self) -> str:
        """Prebuilt scikit-learn serving container for this region.

        Pinned by minor version deliberately: "latest" would change what a registered
        model deserialises with, which is the failure Lab 2 Task 5 is looking for.
        """
        return (f"{self.cfg.region}-docker.pkg.dev/vertex-ai/prediction/"
                "sklearn-cpu.1-5:latest")


    # deploy / invoke                   -> Lab 3 (Vertex Endpoint)
    # emit_metric                       -> Lab 4 (Cloud Monitoring time series)
    # generate                          -> Lab 5 (managed LLM endpoint; read usageMetadata for tokens)
    # teardown                          -> Lab 5 (filter resources by label)
