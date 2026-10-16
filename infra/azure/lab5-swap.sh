#!/usr/bin/env bash
# Lab 5 Task 4 — the swap on a REAL Azure subscription, then delete everything.
#
#   bash infra/azure/lab5-swap.sh demo    # up, make swap-check SECOND=azure, down (always)
#   bash infra/azure/lab5-swap.sh up      # create only
#   bash infra/azure/lab5-swap.sh down    # delete the resource group and confirm it is gone
#
# Needs: az CLI logged in (`az login`), the containerapp extension, Docker, and the AZURE_*
# lines in cloud.env (see cloud.env.example). Creates, all in one resource group tagged
# course=itcs355 lab=5:
#   storage account + container   (shared-key access DISABLED: Entra ID tokens only)
#   container registry, Basic     (the serving image, pushed from this laptop)
#   user-assigned identity        (AcrPull on the registry, Blob Data Reader on storage)
#   Container Apps env + app      (the same serving image Cloud Run runs, 0.5 vCPU / 1 GiB)
#
# Cost, ~30 minutes end to end, Southeast Asia, prices as listed on azure.microsoft.com
# (check them on the day): registry Basic ~USD 0.17/day, so ~6 THB if billed as a whole
# day; Container Apps inside its monthly free grant (180,000 vCPU-s); storage and requests
# well under 1 THB. Billed to the Azure for Students credit, not a card.
set -euo pipefail
cd "$(dirname "$0")/../.."

env_get() { sed -n "s/^$1=//p" cloud.env | sed 's/[[:space:]]*#.*//' | tail -1; }
RG=$(env_get AZURE_PROJECT_ID)
LOC=$(env_get AZURE_REGION)
BLOB=$(env_get AZURE_BLOB_URI)
REGISTRY=$(env_get AZURE_CONTAINER_REGISTRY)
PRIMARY_PROJECT=$(env_get PROJECT_ID)
for v in RG LOC BLOB REGISTRY; do
  [[ -n ${!v} ]] || { echo "cloud.env: AZURE_* slots unset (see cloud.env.example)"; exit 1; }
done
ACCOUNT=$(sed -E 's#https://([^.]+)\.blob\.core\.windows\.net/.*#\1#' <<<"$BLOB")
CONTAINER=$(sed -E 's#https://[^/]+/([^/]+).*#\1#' <<<"$BLOB")
ACR=${REGISTRY%%.*}
APP=${SERVICE:-itcs355-serve}
ENVNAME=itcs355-env
IDENTITY=itcs355-serve-id
TAG=swap-$(git rev-parse --short HEAD)
MODEL=${MODEL:-reports/registered-model.joblib}
[[ -f $MODEL ]] || MODEL=reports/model.joblib
TAGS=(course=itcs355 "student=$PRIMARY_PROJECT" lab=5)

up() {
  [[ -f $MODEL ]] || { echo "no model at $MODEL — set MODEL=path/to/model.joblib"; exit 1; }
  az account show --query "{subscription:name, user:user.name}" -o tsv
  echo "== resource providers (first run only takes a minute)"
  for ns in Microsoft.App Microsoft.ContainerRegistry Microsoft.Storage \
            Microsoft.ManagedIdentity Microsoft.OperationalInsights; do
    az provider register --namespace "$ns" --wait -o none
  done

  echo "== resource group $RG ($LOC)"
  az group create -n "$RG" -l "$LOC" --tags "${TAGS[@]}" -o none

  echo "== storage $ACCOUNT/$CONTAINER"
  az storage account create -n "$ACCOUNT" -g "$RG" -l "$LOC" --sku Standard_LRS --kind StorageV2 \
    --allow-blob-public-access false --allow-shared-key-access false --min-tls-version TLS1_2 \
    --tags "${TAGS[@]}" -o none
  SA_ID=$(az storage account show -n "$ACCOUNT" -g "$RG" --query id -o tsv)
  ME=$(az ad signed-in-user show --query id -o tsv)
  # Owning the subscription grants no DATA access: blob reads and writes need a data role,
  # even for you. This is the same lesson as Lab 5 Task 2, on a second provider.
  az role assignment create --assignee-object-id "$ME" --assignee-principal-type User \
    --role "Storage Blob Data Contributor" --scope "$SA_ID" -o none 2>/dev/null || true
  echo "   waiting for the role to propagate (up to 5 min)"
  for _ in $(seq 30); do
    az storage container create --account-name "$ACCOUNT" -n "$CONTAINER" --auth-mode login -o none \
      2>/dev/null && break
    sleep 10
  done

  echo "== registry $ACR and image itcs355-serve:$TAG"
  az acr create -n "$ACR" -g "$RG" -l "$LOC" --sku Basic --admin-enabled false --tags "${TAGS[@]}" -o none
  ACR_ID=$(az acr show -n "$ACR" --query id -o tsv)
  docker buildx build --platform linux/amd64 -f service/Dockerfile.serve \
    -t "$ACR.azurecr.io/itcs355-serve:$TAG" --load . -q
  az acr login -n "$ACR"
  docker push -q "$ACR.azurecr.io/itcs355-serve:$TAG"
  IMAGE=$(docker inspect --format '{{index .RepoDigests 0}}' "$ACR.azurecr.io/itcs355-serve:$TAG")
  echo "   $IMAGE"

  echo "== identity $IDENTITY"
  az identity create -n "$IDENTITY" -g "$RG" -l "$LOC" --tags "${TAGS[@]}" -o none
  UAMI_ID=$(az identity show -n "$IDENTITY" -g "$RG" --query id -o tsv)
  UAMI_PRINCIPAL=$(az identity show -n "$IDENTITY" -g "$RG" --query principalId -o tsv)
  UAMI_CLIENT=$(az identity show -n "$IDENTITY" -g "$RG" --query clientId -o tsv)
  for pair in "AcrPull:$ACR_ID" "Storage Blob Data Reader:$SA_ID"; do
    az role assignment create --assignee-object-id "$UAMI_PRINCIPAL" \
      --assignee-principal-type ServicePrincipal --role "${pair%%:*}" --scope "${pair#*:}" \
      -o none 2>/dev/null || true
  done

  echo "== model -> blob, through cloudlayer/azure.py"
  python - "$BLOB" "$MODEL" <<'PY'
import sys
from cloudlayer.azure import AzureAdapter
from src import config
cfg = config.Config(**{**config.load(strict=False).__dict__,
                       "provider": "azure", "blob_uri": sys.argv[1]})
print("  ", AzureAdapter(cfg).upload(sys.argv[2], "models/itcs355/1/model.joblib"))
PY

  echo "== Container Apps $ENVNAME/$APP"
  az containerapp env create -n "$ENVNAME" -g "$RG" -l "$LOC" --logs-destination none \
    --tags "${TAGS[@]}" -o none
  echo "   giving AcrPull a minute to propagate before the first pull"
  sleep 60
  az containerapp create -n "$APP" -g "$RG" --environment "$ENVNAME" --image "$IMAGE" \
    --user-assigned "$UAMI_ID" --registry-server "$ACR.azurecr.io" --registry-identity "$UAMI_ID" \
    --ingress external --target-port 8080 --cpu 0.5 --memory 1.0Gi \
    --min-replicas 1 --max-replicas 1 --tags "${TAGS[@]}" \
    --env-vars CLOUD_PROVIDER=azure "BLOB_URI=$BLOB" "MODEL_URI=$BLOB/models/itcs355/1" \
      MODEL_VERSION=1 "AZURE_CLIENT_ID=$UAMI_CLIENT" WEB_CONCURRENCY=1 -o none
  URL=https://$(az containerapp show -n "$APP" -g "$RG" --query properties.configuration.ingress.fqdn -o tsv)
  for _ in $(seq 30); do curl -sf "$URL/ready" >/dev/null && break; sleep 5; done
  echo "   $URL/ready -> $(curl -s "$URL/ready")"
}

down() {
  echo "== deleting resource group $RG (takes a few minutes)"
  if [[ $(az group exists -n "$RG") == true ]]; then az group delete -n "$RG" --yes; fi
  echo "   az group exists -n $RG -> $(az group exists -n "$RG")"
  echo "   anything left tagged course=itcs355 lab=5:"
  az resource list --tag lab=5 --query "[?tags.course=='itcs355'].[type,name]" -o tsv | sed 's/^/     /'
  echo "   (empty above = clean; deletion is asynchronous, so check the portal again tomorrow)"
}

demo() {
  trap down EXIT
  up
  mkdir -p reports/lab5/portability
  make swap-check SECOND=azure | tee reports/lab5/portability/swap-check-azure.txt
}

"${1:-demo}"
