"""Fail the build if provider-specific strings leak into src/.

Layer 1 (src/) must be provider-neutral. Layer 3 (cloudlayer/) is where SDKs live.
This script is what makes that rule real rather than aspirational, and Lab 5 grades it.

Two checks:

  1. Pattern scan. URI schemes, hostnames, SDK imports, CLI invocations and mount paths
     that only one provider uses. These are leaks wherever they appear outside cloudlayer/.
  2. cloud.env resolution. Every value in cloud.env (project, bucket, registry, region,
     identity) must reach the code THROUGH cloud.env. A literal copy of one in code is a
     reference that will not move when cloud.env does, which is exactly how a swap fails
     while the pattern scan passes. Also checks that the slots are all set and that the
     URI forms agree with CLOUD_PROVIDER.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCANNED = ["src", "service", "monitoring", "tests"]
ENV_FILE = ROOT / "cloud.env"

FORBIDDEN = [
    (r"s3://", "S3 URI"),
    (r"gs://", "GCS URI"),
    (r"abfss://", "ADLS URI"),
    (r"amazonaws\.com", "AWS hostname"),
    (r"blob\.core\.windows\.net", "Azure hostname"),
    (r"googleapis\.com", "GCP hostname"),
    (r"azurecr\.io", "ACR hostname"),
    (r"dkr\.ecr\.", "ECR hostname"),
    (r"pkg\.dev", "Artifact Registry hostname"),
    (r"\bimport boto3\b", "AWS SDK"),
    (r"\bfrom azure\b", "Azure SDK"),
    (r"\bfrom google\.cloud\b", "GCP SDK"),
    (r"^\s*[\"']?/(home|Users)/", "absolute path from a developer machine"),
    # Lab 5 additions: the leaks the first list does not see.
    (r"\bimport (google|vertexai|sagemaker|botocore|azure)\b", "provider SDK"),
    (r"\bfrom (boto3|botocore|sagemaker|vertexai)\b", "provider SDK"),
    (r"\.run\.app\b", "Cloud Run hostname"),
    (r"azurecontainerapps\.io|azurewebsites\.net|inference\.ml\.azure\.com", "Azure hostname"),
    (r"metadata\.google\.internal|169\.254\.169\.254", "instance metadata endpoint"),
    (r"[\"'(]/gcs/", "GCS FUSE mount path"),
    (r"\[\s*[\"'](gcloud|gsutil|az|aws)[\"']", "provider CLI invocation"),
]

# The slots whose VALUES identify an external resource. CLOUD_PROVIDER is a selector,
# MLFLOW_TRACKING_URI defaults to a local sqlite file, BUDGET_LIMIT_THB is a number.
RESOLVED_SLOTS = ("PROJECT_ID", "REGION", "BLOB_URI", "CONTAINER_REGISTRY",
                  "MODEL_REGISTRY_NAME", "IDENTITY_REF")
SCHEME_FOR = {"gcp": "gs://", "aws": "s3://", "azure": "https://"}


def _cloud_env() -> dict[str, str]:
    """cloud.env as written, not as exported: the point is to see the file's values."""
    if not ENV_FILE.exists():
        return {}
    out = {}
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value.split(" #")[0].strip()
    return out


def _needles(env: dict[str, str]) -> list[tuple[str, str]]:
    """Literal strings that must only ever arrive through cloud.env."""
    needles: list[tuple[str, str]] = []
    for slot in RESOLVED_SLOTS:
        value = env.get(slot, "")
        if len(value) >= 6:
            needles.append((value, slot))
    # The bucket on its own: gs://bucket/prefix -> bucket. A hardcoded bucket name is the
    # commonest form of this leak, and it never contains the scheme.
    blob = env.get("BLOB_URI", "")
    if "://" in blob:
        bucket = blob.split("://", 1)[1].split("/", 1)[0]
        if len(bucket) >= 6:
            needles.append((bucket, "BLOB_URI bucket"))
    return needles


def scan(needles: list[tuple[str, str]]) -> list[str]:
    hits: list[str] = []
    for folder in SCANNED:
        for path in sorted((ROOT / folder).rglob("*.py")):
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue  # comments may name providers; code may not
                rel = path.relative_to(ROOT)
                for pattern, label in FORBIDDEN:
                    if re.search(pattern, line, flags=re.MULTILINE):
                        hits.append(f"{rel}:{lineno}  {label}  ->  {line.strip()[:80]}")
                for value, slot in needles:
                    if value in line:
                        hits.append(f"{rel}:{lineno}  literal {slot} from cloud.env  ->  "
                                    f"{line.strip()[:80]}")
    return hits


def resolution(env: dict[str, str]) -> list[str]:
    """Does cloud.env carry every external reference, in the provider's form?"""
    if not env:
        return []
    problems = []
    for slot in ("CLOUD_PROVIDER", *RESOLVED_SLOTS):
        if not env.get(slot) and not os.environ.get(slot):
            problems.append(f"cloud.env: {slot} is unset — the code would fall back to a default")
    provider = env.get("CLOUD_PROVIDER", "").lower()
    scheme = SCHEME_FOR.get(provider)
    blob = env.get("BLOB_URI", "")
    if scheme and blob and not blob.startswith(scheme):
        problems.append(f"cloud.env: BLOB_URI {blob!r} is not a {provider} URI ({scheme}...)")
    return problems


def main() -> int:
    env = _cloud_env()
    hits = scan(_needles(env))
    problems = resolution(env)

    if hits or problems:
        print("PORTABILITY AUDIT FAILED — provider details leaked outside cloudlayer/\n")
        for h in hits + problems:
            print("  " + h)
        print("\nMove each reference into cloudlayer/ or into cloud.env.")
        return 1

    print(f"PORTABILITY AUDIT PASSED — {', '.join(SCANNED)} contain no provider-specific strings")
    if env:
        print(f"  and no literal copy of the {len(_needles(env))} cloud.env values; "
              f"all {1 + len(RESOLVED_SLOTS)} slots resolve through cloud.env "
              f"({env.get('CLOUD_PROVIDER')})")
    else:
        print("  (no cloud.env here, so the resolution check was skipped)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
