#!/usr/bin/env bash
# Lab 5 Task 4 — the swap, against emulators. No Azure account, no az CLI, no cost.
#
#   bash infra/azure/emulator-demo.sh [path/to/model.joblib]
#
# Azurite (Microsoft's blob emulator) stands in for the storage account; the serving image,
# run locally, stands in for the Container Apps app. The container reads its model THROUGH
# cloudlayer/azure.py from Azurite, exactly as the deployed app reads it from blob storage
# with its managed identity, and swap-check then drives upload/download/invoke.
#
# What this cannot show: managed-identity auth, Container Apps' ingress, and real latency.
# infra/azure/lab5-swap.sh does those on a real subscription.
set -euo pipefail
cd "$(dirname "$0")/../.."

MODEL=${1:-reports/model.joblib}
AZURITE_IMAGE=mcr.microsoft.com/azure-storage/azurite:3.35.0
NET=itcs355-swap
TAG=swap-$(git rev-parse --short HEAD)
ACCOUNT=devstoreaccount1                 # Azurite's fixed account name
CONTAINER=itcs355

[[ -f $MODEL ]] || { echo "no model at $MODEL — run 'make serve' once, or pass a path"; exit 1; }

cleanup() { docker rm -f itcs355-azurite itcs355-serve-azure >/dev/null 2>&1 || true
            docker network rm "$NET" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup
docker network create "$NET" >/dev/null

echo "== Azurite (blob only)"
docker run -d --name itcs355-azurite --network "$NET" -p 127.0.0.1:10000:10000 "$AZURITE_IMAGE" \
  azurite-blob --blobHost 0.0.0.0 --skipApiVersionCheck >/dev/null
# The emulator's account key is a public constant that ships inside the emulator. Read it
# from there rather than writing it into the repository, where the secret scanner (rightly)
# cannot tell it from a real one.
AZURE_STORAGE_KEY=$(docker exec itcs355-azurite sh -c \
  "grep -ho 'EMULATOR_ACCOUNT_KEY_STR = \"[^\"]*\"' /opt/azurite/dist/src/blob/utils/constants.js" \
  | cut -d'"' -f2)
export AZURE_STORAGE_KEY
for _ in $(seq 30); do curl -s -o /dev/null http://127.0.0.1:10000/ && break; sleep 1; done

HOST_BLOB=http://127.0.0.1:10000/$ACCOUNT/$CONTAINER/itcs355     # as the laptop sees it
NET_BLOB=http://itcs355-azurite:10000/$ACCOUNT/$CONTAINER/itcs355  # as the container sees it

echo "== container + model, through the adapter"
python - "$HOST_BLOB" "$MODEL" <<'PY'
import sys
from cloudlayer.azure import AzureAdapter
from src import config
blob, model = sys.argv[1], sys.argv[2]
cfg = config.Config(**{**config.load(strict=False).__dict__, "provider": "azure", "blob_uri": blob})
a = AzureAdapter(cfg)
container = blob.rsplit("/", 1)[0]
with a._request("PUT", container + "?restype=container", b""):   # infra, not a seam method
    pass
print("  uploaded", a.upload(model, "models/itcs355/1/model.joblib"))
PY

echo "== serving image $TAG (linux/amd64, the image Cloud Run runs)"
docker buildx build --platform linux/amd64 -f service/Dockerfile.serve -t "itcs355-serve:$TAG" --load . -q >/dev/null
docker run -d --name itcs355-serve-azure --network "$NET" --platform linux/amd64 -p 127.0.0.1:8081:8080 \
  -e CLOUD_PROVIDER=azure -e BLOB_URI="$NET_BLOB" -e AZURE_STORAGE_KEY \
  -e MODEL_URI="$NET_BLOB/models/itcs355/1" -e MODEL_VERSION=1 -e WEB_CONCURRENCY=1 \
  "itcs355-serve:$TAG" >/dev/null
for _ in $(seq 60); do curl -sf http://127.0.0.1:8081/ready >/dev/null && break; sleep 1; done
echo "  /ready -> $(curl -s http://127.0.0.1:8081/ready)"
echo "  model load, from the container's own log:"
docker logs itcs355-serve-azure 2>&1 | grep -m1 model_load | sed 's/^/    /'

echo "== swap-check"
AZURE_PROJECT_ID=itcs355-emulator AZURE_REGION=local AZURE_BLOB_URI="$HOST_BLOB" \
AZURE_CONTAINER_REGISTRY=none AZURE_IDENTITY_REF=none AZURE_ENDPOINT_URL=http://127.0.0.1:8081 \
  python scripts/portability_swap_check.py --second-provider azure
