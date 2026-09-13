# ITCS355 Lab 1 — training image
#
# Base image pinned BY DIGEST. Tags move; a digest does not.
#   The digest is the multi-arch index of python:3.11-slim (3.11.16, Debian trixie) as
#   resolved on 2026-09-13, so `--platform linux/amd64` always selects the same amd64
#   manifest. The tag is kept for readability only — with a digest present, Docker
#   ignores it. Refresh deliberately, never implicitly:
#       docker buildx imagetools inspect python:3.11-slim   # "Digest:" line
FROM python:3.11-slim@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534 AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# Dependencies first so this layer caches independently of your source.
COPY requirements.txt ./
# --require-hashes turns a silently-substituted package into a build failure.
# --no-deps: the lock already lists every transitive dependency, so pip must not resolve.
RUN pip install --require-hashes --no-deps --prefix=/install -r requirements.txt


FROM python:3.11-slim@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534 AS runtime

# Non-root. A training container has no reason to run as root, and graders check.
RUN useradd --create-home --uid 10001 runner
# No git in the image: the commit SHA arrives as GIT_COMMIT, so silence GitPython's probe.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    GIT_PYTHON_REFRESH=quiet \
    MLFLOW_DISABLE_AGENT_HINT=1

COPY --from=builder /install /usr/local
WORKDIR /app
COPY --chown=runner:runner src/ ./src/
COPY --chown=runner:runner cloudlayer/ ./cloudlayer/
COPY --chown=runner:runner scripts/ ./scripts/

USER runner

# Credentials NEVER enter an image layer. They arrive at runtime from SECRET_STORE_PATH
# or from the platform's identity. If you find yourself adding an ARG for a key, stop.
ENTRYPOINT ["python", "-m", "src.train"]
CMD ["--n-estimators", "200", "--max-depth", "8"]
