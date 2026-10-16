# ITCS355 Lab 5 — pipeline step image. Every step of pipeline/pipeline.yaml runs in this one
# image; the step is chosen by the command, never by different code baked into the DAG.
#
#   docker buildx build --platform linux/amd64 -f pipeline/Dockerfile \
#     --build-arg GIT_COMMIT=$(git rev-parse HEAD) -t itcs355-pipeline:<sha> --load .
#
# It is the Lab 1 training image plus two things the steps need:
#   * tests/ and service/: the validate step runs the Lab 4 contract tests, and one of them
#     checks the API's bounds against the data contract;
#   * the gcloud CLI: register and deploy change things in the cloud through cloudlayer/,
#     which drives gcloud. That makes the image ~600 MB larger than the training image.
#     The price of not writing a second, REST-only adapter is a heavier image.

# Same base, by digest, as the training image.
FROM python:3.11-slim@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534 AS builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /build
COPY requirements.txt ./
RUN pip install --require-hashes --no-deps --prefix=/install -r requirements.txt

# gcloud, pinned by version AND by hash. The tarball bundles its own Python, so it does not
# touch the one the steps run on.
FROM python:3.11-slim@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534 AS gcloud
ARG GCLOUD_VERSION=583.0.0
ARG GCLOUD_SHA256=84c5e4798836bda13aa82c3e84fa1acd0c4e4ca5318f7141052e3a5a26a7cc97
ADD --checksum=sha256:${GCLOUD_SHA256} \
    https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-cli-${GCLOUD_VERSION}-linux-x86_64.tar.gz \
    /tmp/gcloud.tar.gz
RUN tar -xzf /tmp/gcloud.tar.gz -C /opt && rm /tmp/gcloud.tar.gz \
 && rm -rf /opt/google-cloud-sdk/.install/.backup /opt/google-cloud-sdk/bin/anthoscli


FROM python:3.11-slim@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534 AS runtime
RUN useradd --create-home --uid 10001 runner
ARG GIT_COMMIT=unknown
LABEL org.opencontainers.image.revision=$GIT_COMMIT
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    GIT_PYTHON_REFRESH=quiet \
    MLFLOW_DISABLE_AGENT_HINT=1 \
    MLFLOW_ALLOW_FILE_STORE=true \
    GIT_COMMIT=$GIT_COMMIT \
    CLOUDSDK_CONFIG=/tmp/gcloud \
    CLOUDSDK_CORE_DISABLE_PROMPTS=1 \
    PATH=/opt/google-cloud-sdk/bin:$PATH

COPY --from=builder /install /usr/local
COPY --from=gcloud /opt/google-cloud-sdk /opt/google-cloud-sdk
WORKDIR /app
COPY --chown=runner:runner src/ ./src/
COPY --chown=runner:runner cloudlayer/ ./cloudlayer/
COPY --chown=runner:runner scripts/ ./scripts/
COPY --chown=runner:runner tests/ ./tests/
COPY --chown=runner:runner service/ ./service/
COPY --chown=runner:runner pipeline/ ./pipeline/
RUN mkdir -p /app/data/raw && chown -R runner:runner /app/data

# Credentials never enter a layer: on Vertex the step runs as the pipeline's service
# account, which gcloud picks up from the metadata server.
USER runner
ENTRYPOINT []
CMD ["python", "-m", "pipeline.steps", "--help"]
