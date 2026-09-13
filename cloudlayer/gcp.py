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

import subprocess
from pathlib import Path

from cloudlayer.base import CloudAdapter


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

    # submit_training / register_model  -> Lab 2 (Vertex custom training + Model Registry)
    # deploy / invoke                   -> Lab 3 (Vertex Endpoint)
    # emit_metric                       -> Lab 4 (Cloud Monitoring time series)
    # generate                          -> Lab 5 (managed LLM endpoint; read usageMetadata for tokens)
    # teardown                          -> Lab 5 (filter resources by label)
