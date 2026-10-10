#!/usr/bin/env bash
# Lab 4 — the monitoring around the staging service. Safe to re-run; each step updates
# what is already there.
#
#   bash infra/gcp/lab4-monitoring.sh <serving image digest ref> <alert email>
#
# Creates:
#   log-based metric  itcs355_requests_by_model_version   (dashboard: model version)
#   Cloud Run job     itcs355-lab4-drift                  (monitoring/drift.py, same image)
#   Cloud Scheduler   itcs355-lab4-drift                  (every 5 min -> the job)
#   email channel + alert policy on custom.googleapis.com/itcs355/drift/psi >= 0.05
#   dashboard         infra/gcp/dashboard.json
#
# `make teardown LAB=4` removes the schedule, the job, the policy and the service. The
# dashboard, channel and log metric cost nothing without traffic and stay as definitions.
set -euo pipefail

IMAGE="${1:?usage: lab4-monitoring.sh <image@sha256:...> <alert email>}"
EMAIL="${2:?alert email required}"
[[ "$IMAGE" == *@sha256:* ]] || { echo "pass a digest-pinned image"; exit 1; }

env_val() { sed -n "s/^$1=//p" cloud.env; }
PROJECT_ID=$(env_val PROJECT_ID); REGION=$(env_val REGION); BLOB_URI=$(env_val BLOB_URI)
NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
RUNTIME_SA="$NUMBER-compute@developer.gserviceaccount.com"
ENDPOINT=itcs355-staging
JOB=itcs355-lab4-drift
API=https://monitoring.googleapis.com/v3/projects/$PROJECT_ID
TOKEN=$(gcloud auth print-access-token)
rest() { curl -sS --fail-with-body -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" "$@"; }

echo "1. log-based metric"
gcloud logging metrics describe itcs355_requests_by_model_version --project "$PROJECT_ID" >/dev/null 2>&1 \
  && verb=update || verb=create
gcloud logging metrics "$verb" itcs355_requests_by_model_version --project "$PROJECT_ID" \
  --config-from-file=infra/gcp/log-metric-model-version.yaml --quiet >/dev/null

echo "2. drift job (image $IMAGE)"
# Reads the last 30 min of inputs from the staging service's logs, scores PSI/KS against
# the training data in the bucket, writes the scores as metrics. --no-fail: the alert is
# the metric policy's job, and a failed execution would only be retried.
gcloud run jobs deploy "$JOB" --project "$PROJECT_ID" --region "$REGION" --quiet \
  --image "$IMAGE" --service-account "$RUNTIME_SA" \
  --cpu 1 --memory 1Gi --max-retries 0 --task-timeout 300s \
  --labels "course=itcs355,student=$PROJECT_ID,lab=4" \
  --set-env-vars "CLOUD_PROVIDER=gcp,PROJECT_ID=$PROJECT_ID,REGION=$REGION,BLOB_URI=$BLOB_URI" \
  --command python \
  --args "-m,monitoring.drift,--endpoint,$ENDPOINT,--reference,data/raw/sensors.csv,--emit,--no-fail,--out,/tmp/drift.json" \
  >/dev/null

echo "3. schedule: every 5 minutes"
SCHED_ARGS=(--project "$PROJECT_ID" --location "$REGION" --schedule "*/5 * * * *" --time-zone UTC
  --uri "https://run.googleapis.com/v2/projects/$PROJECT_ID/locations/$REGION/jobs/$JOB:run"
  --http-method POST --oauth-service-account-email "$RUNTIME_SA" --quiet)
if gcloud scheduler jobs describe "itcs355-lab4-drift" --project "$PROJECT_ID" --location "$REGION" >/dev/null 2>&1; then
  gcloud scheduler jobs update http itcs355-lab4-drift "${SCHED_ARGS[@]}" >/dev/null
else
  gcloud scheduler jobs create http itcs355-lab4-drift "${SCHED_ARGS[@]}" >/dev/null
fi

echo "4. email channel -> $EMAIL"
CHANNEL=$(rest "$API/notificationChannels" | python3 -c "
import json, sys
for c in json.load(sys.stdin).get('notificationChannels', []):
    if c.get('type') == 'email' and c['labels'].get('email_address') == sys.argv[1]:
        print(c['name']); break" "$EMAIL")
if [ -z "$CHANNEL" ]; then
  CHANNEL=$(rest -X POST "$API/notificationChannels" -d "{\"type\":\"email\",\"displayName\":\"ITCS355 drift alerts\",\"labels\":{\"email_address\":\"$EMAIL\"}}" \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['name'])")
fi

echo "5. alert policy"
for old in $(rest "$API/alertPolicies?filter=user_labels.lab%3D%224%22" \
             | python3 -c "import json,sys; [print(p['name']) for p in json.load(sys.stdin).get('alertPolicies', [])]"); do
  rest -X DELETE "https://monitoring.googleapis.com/v3/$old" >/dev/null
done
sed "s#__CHANNEL__#$CHANNEL#" infra/gcp/drift-alert-policy.json \
  | rest -X POST "$API/alertPolicies" -d @- | python3 -c "import json,sys; print('   ', json.load(sys.stdin)['name'])"

echo "6. dashboard"
for old in $(gcloud monitoring dashboards list --project "$PROJECT_ID" \
             --filter="displayName:'ITCS355 staging'" --format='value(name)'); do
  gcloud monitoring dashboards delete "$old" --project "$PROJECT_ID" --quiet >/dev/null
done
gcloud monitoring dashboards create --project "$PROJECT_ID" \
  --config-from-file=infra/gcp/dashboard.json --format='value(name)'

echo "done. Run the job once now: gcloud run jobs execute $JOB --region $REGION --wait"
