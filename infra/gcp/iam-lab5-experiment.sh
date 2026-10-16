#!/usr/bin/env bash
# Lab 5 Task 2 — the removal experiment. Run after `iam-lab5.sh up --impersonate user:<you>`.
#
#   bash infra/gcp/iam-lab5-experiment.sh            # both experiments
#   bash infra/gcp/iam-lab5-experiment.sh A|B        # one
#
# Every command runs AS the lab identity (gcloud impersonation; the adapter's gcloud calls
# inherit CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT). Raw output, stderr included, goes to
# reports/lab5/iam/ with a UTC timestamp per attempt. IAM changes take up to ~7 minutes to
# propagate, so each "after" step retries until the outcome changes or 8 minutes pass.
#
# A  training: remove storage.objects.list on itcs355/data/ (keep get), run the step's
#    data read through the adapter (download_prefix -> gcloud storage rsync).
# B  CI: remove actAs on the serving SA, deploy a staging-shaped service as that SA.
#    Uses Google's public hello image, scale-to-zero, deleted at the end (~0 THB).
set -uo pipefail

WHICH="${1:-AB}"
ENV_FILE="${CLOUD_ENV:-cloud.env}"
env_val() { sed -n "s/^$1=//p" "$ENV_FILE"; }
PROJECT_ID=$(env_val PROJECT_ID); REGION=$(env_val REGION); BLOB_URI=$(env_val BLOB_URI)
TRAIN="itcs355-lab5-train@$PROJECT_ID.iam.gserviceaccount.com"
SERVE="itcs355-lab5-serve@$PROJECT_ID.iam.gserviceaccount.com"
CI="itcs355-lab5-ci@$PROJECT_ID.iam.gserviceaccount.com"
PY="${PYTHON:-$( [[ -x .venv/bin/python ]] && echo .venv/bin/python || echo python3)}"   # PYTHON=... to override
OUT=reports/lab5/iam
mkdir -p "$OUT"
IAM="bash infra/gcp/iam-lab5.sh"

# $1 log file, $2 identity ("" = me), rest: command. Appends everything, returns its status.
as() {
  local log="$1" who="$2"; shift 2
  {
    echo "### $(date -u +%FT%TZ)  as ${who:-$(gcloud config get-value account 2>/dev/null)}"
    echo "\$ $*"
  } >>"$log"
  if [[ -n "$who" ]]; then
    CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT="$who" "$@" >>"$log" 2>&1
  else
    "$@" >>"$log" 2>&1
  fi
  local rc=$?
  echo "[exit $rc]" >>"$log"; echo >>"$log"
  return $rc
}

# $1 want "fail"|"pass", $2 log, rest as for `as`. Retries for IAM propagation.
until_outcome() {
  local want="$1"; shift
  for i in $(seq 1 16); do
    if as "$@"; then got=pass; else got=fail; fi
    echo "  attempt $i: $got"
    [[ "$got" == "$want" ]] && return 0
    sleep 30
  done
  echo "  outcome never became $want"; return 1
}

pull_data() {   # the training step's own read path, through the adapter
  local dest; dest=$(mktemp -d)
  "$PY" -c "
from src import config
from cloudlayer.factory import get_adapter
n = get_adapter(config.load()).download_prefix('data', '$dest')
print('download_prefix(data) ->', n, 'files')"
}
get_one() {     # one named object: storage.objects.get only
  local dest; dest=$(mktemp -d)
  "$PY" -c "
from src import config
from cloudlayer.factory import get_adapter
get_adapter(config.load()).download('$BLOB_URI/data/raw/sensors.csv', '$dest/sensors.csv')
print('download(sensors.csv) ok')"
}

experiment_a() {
  local log="$OUT/A-train-list.log"; : >"$log"
  echo "A. training identity, storage.objects.list on itcs355/data/"
  echo "== baseline, every grant present" >>"$log"
  until_outcome pass "$log" "$TRAIN" pull_data || return 1
  echo "== scoping holds: outside its prefixes the training SA gets nothing" >>"$log"
  as "$log" "$TRAIN" gcloud storage cat "$BLOB_URI/lab2-registry/v1/model.joblib" --range=0-15 || true
  as "$log" "$TRAIN" gcloud storage cp "$ENV_FILE.example" "$BLOB_URI/data/raw/should-not-exist.txt" || true
  echo "== REMOVE lab5-train-list-data" >>"$log"
  as "$log" "" $IAM revoke lab5-train-list-data
  until_outcome fail "$log" "$TRAIN" pull_data || return 1
  echo "== same identity, same moment: one named object still downloads" >>"$log"
  as "$log" "$TRAIN" get_one || true
  echo "== RESTORE lab5-train-list-data" >>"$log"
  as "$log" "" $IAM grant lab5-train-list-data
  until_outcome pass "$log" "$TRAIN" pull_data
}

PROBE=itcs355-lab5-iamprobe
deploy_probe() {
  gcloud run deploy "$PROBE" --project "$PROJECT_ID" --region "$REGION" \
    --image us-docker.pkg.dev/cloudrun/container/hello --service-account "$SERVE" \
    --no-allow-unauthenticated --min-instances 0 --max-instances 1 \
    --labels course=itcs355,lab=5 --quiet
}

experiment_b() {
  local log="$OUT/B-ci-actas.log"; : >"$log"
  echo "B. CI identity, iam.serviceAccountUser (actAs) on the serving SA"
  echo "== REMOVE actAs on $SERVE for $CI" >>"$log"
  as "$log" "" $IAM revoke "roles/iam.serviceAccountUser;$SERVE"
  until_outcome fail "$log" "$CI" deploy_probe || return 1
  echo "== RESTORE" >>"$log"
  as "$log" "" $IAM grant "roles/iam.serviceAccountUser;$SERVE"
  until_outcome pass "$log" "$CI" deploy_probe || return 1
  echo "== which identity the revision runs as" >>"$log"
  as "$log" "" gcloud run services describe "$PROBE" --project "$PROJECT_ID" --region "$REGION" \
    --format='value(spec.template.spec.serviceAccountName)'
  echo "== its stdout reached Cloud Logging, though the serving SA holds no logging role" >>"$log"
  sleep 20
  as "$log" "" gcloud logging read --project "$PROJECT_ID" --freshness=30m --limit=5 \
    "resource.type=cloud_run_revision AND resource.labels.service_name=$PROBE" \
    --format='value(timestamp,logName,textPayload)'
  echo "== clean up" >>"$log"
  as "$log" "" gcloud run services delete "$PROBE" --project "$PROJECT_ID" --region "$REGION" --quiet
}

[[ "$WHICH" == *A* ]] && experiment_a
[[ "$WHICH" == *B* ]] && experiment_b
echo "logs in $OUT/"
