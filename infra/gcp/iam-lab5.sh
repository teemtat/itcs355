#!/usr/bin/env bash
# Lab 5 Task 2 — three scoped identities in place of the default compute SA's roles/editor.
# Safe to re-run; every step checks or adds, nothing is replaced.
#
#   bash infra/gcp/iam-lab5.sh up   [--impersonate user:me@example.com]
#   bash infra/gcp/iam-lab5.sh down             # remove every binding below, then the SAs
#   bash infra/gcp/iam-lab5.sh show             # print what each SA holds, where
#   bash infra/gcp/iam-lab5.sh revoke|grant <pattern>   # one grant, for the removal test
#                                               e.g. revoke 'lab5-train-read-data'
#
# Identities (reports/lab5-iam.md has the why for each line):
#   itcs355-lab5-train  Vertex pipeline/job SA. Reads itcs355/data/, writes itcs355/lab5/.
#   itcs355-lab5-serve  Cloud Run staging runtime SA. Reads registered model artifacts.
#   itcs355-lab5-ci     GitHub Actions (OIDC). Pushes to the itcs355 repo, submits the
#                       pipeline, deploys staging, and may act as the other two only.
#
# Bucket grants are IAM conditions on object-name prefixes (the bucket has uniform
# bucket-level access, so conditions apply). A list call is checked against the bucket,
# not an object, so listing is admitted separately through the objectListPrefix attribute.
# For the data prefix that is its own binding (lab5-train-list-data), so the removal
# experiment can take it away without touching object reads.
#
# Deliberately untouched: the capstone's scamshield-deployer / scamshield-oidc / scamshield,
# Lab 4's gh-deployer, and the default compute SA (scamshield still runs as it).
#
# Provider-specific by design: it lives in infra/gcp/, not in src/.
set -euo pipefail

CMD="${1:-up}"; shift || true
IMPERSONATOR="${IMPERSONATOR:-}"
[[ "${1:-}" == "--impersonate" ]] && IMPERSONATOR="${2:?--impersonate user:<email>}"

ENV_FILE="${CLOUD_ENV:-cloud.env}"
env_val() { sed -n "s/^$1=//p" "$ENV_FILE"; }
PROJECT_ID=$(env_val PROJECT_ID); REGION=$(env_val REGION)
BLOB_URI=$(env_val BLOB_URI)                      # gs://itcs355-6688143/itcs355
BUCKET=$(echo "${BLOB_URI#gs://}" | cut -d/ -f1)
ROOT=$(echo "${BLOB_URI#gs://}" | cut -d/ -f2-)   # itcs355
AR_REPO=$(env_val CONTAINER_REGISTRY | awk -F/ '{print $NF}')
NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
GH_REPO="${GH_REPO:-teemtat/itcs355}"
WIF_MEMBER="principalSet://iam.googleapis.com/projects/$NUMBER/locations/global/workloadIdentityPools/github/attribute.repository/$GH_REPO"

sa() { echo "itcs355-lab5-$1@$PROJECT_ID.iam.gserviceaccount.com"; }
TRAIN=$(sa train); SERVE=$(sa serve); CI=$(sa ci)

# Prefixes, relative to the bucket. Serving reads the Lab 2 registry (the model staging
# serves today) and the Lab 5 registry the pipeline writes to; nothing else.
DATA_PREFIX="$ROOT/data/"
WORK_PREFIX="$ROOT/lab5/"
SERVE_PREFIXES=("$ROOT/lab2-registry/" "$ROOT/lab5/registry/")

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

# One condition file per grant. $1 title, $2 CEL expression.
cond() {
  local f="$TMP/$(echo "$1" | tr -c 'a-z0-9' _).yaml"
  printf 'title: %s\nexpression: >-\n  %s\n' "$1" "$2" > "$f"; echo "$f"
}
obj() { echo "resource.name.startsWith(\"projects/_/buckets/$BUCKET/objects/$1\")"; }
# A "directory": the prefix itself as an object name, and everything under it. rsync and
# GCS FUSE first GET the bare name (e.g. itcs355/data, no slash) to ask "object or
# prefix?"; a 403 there, unlike a 404, aborts them. Found by the baseline run, see
# reports/lab5/iam/A0-baseline-condition-too-tight.log.
dir() { local d="${1%/}"; echo "resource.name == \"projects/_/buckets/$BUCKET/objects/$d\" || $(obj "$d/")"; }
# gcloud lists with the prefix minus its slash (prefix=itcs355/data&delimiter=/), so the
# bare name must be admitted too. It also matches names that merely start with "data"
# (itcs355/data-old/...), but only their names: get is still scoped by dir().
# Found on the second baseline run, see reports/lab5/iam/A1-list-prefix-probe.log.
lst() {
  local a='api.getAttribute("storage.googleapis.com/objectListPrefix", "")' d="${1%/}"
  echo "$a == \"$d\" || $a.startsWith(\"$d/\")"
}

serve_expr() {
  local e="" p
  for p in "${SERVE_PREFIXES[@]}"; do e+="${e:+ || }$(obj "$p")"; done
  echo "$e"
}

# Every grant, once, as data: kind;member;role;target;condition-title;expression
grants() {
  cat <<EOF
bucket;$TRAIN;roles/storage.objectViewer;$BUCKET;lab5-train-read-data;$(dir "$DATA_PREFIX")
bucket;$TRAIN;roles/storage.objectViewer;$BUCKET;lab5-train-list-data;$(lst "$DATA_PREFIX")
bucket;$TRAIN;roles/storage.objectUser;$BUCKET;lab5-train-write-work;$(dir "$WORK_PREFIX") || $(lst "$WORK_PREFIX")
project;$TRAIN;roles/aiplatform.user;$PROJECT_ID;;
sa;$TRAIN;roles/iam.serviceAccountUser;$TRAIN;;
bucket;$SERVE;roles/storage.objectViewer;$BUCKET;lab5-serve-read-model;$(serve_expr)
repo;$CI;roles/artifactregistry.writer;$AR_REPO;;
project;$CI;roles/aiplatform.user;$PROJECT_ID;;
project;$CI;roles/run.developer;$PROJECT_ID;;
sa;$CI;roles/iam.serviceAccountUser;$TRAIN;;
sa;$CI;roles/iam.serviceAccountUser;$SERVE;;
sa;$WIF_MEMBER;roles/iam.workloadIdentityUser;$CI;;
EOF
  # Only so the removal experiment can be run as each identity from a laptop. Owner does
  # not include getAccessToken on a service account.
  if [[ -n "$IMPERSONATOR" ]]; then
    for s in "$TRAIN" "$SERVE" "$CI"; do
      echo "sa;$IMPERSONATOR;roles/iam.serviceAccountTokenCreator;$s;;"
    done
  fi
}

member_of() { [[ "$1" == *:* ]] && echo "$1" || echo "serviceAccount:$1"; }

# $1 add|remove, then one grant line.
apply() {
  local verb="$1" kind member role target title expr
  IFS=';' read -r kind member role target title expr <<<"$2"
  member=$(member_of "$member")
  local c=(--condition=None)
  [[ -n "$title" ]] && c=(--condition-from-file="$(cond "$title" "$expr")")
  echo "  $verb $kind $target  $role  ${member#*:}${title:+  [$title]}"
  case "$kind" in
    bucket)  gcloud storage buckets "$verb-iam-policy-binding" "gs://$target" \
               --member="$member" --role="$role" "${c[@]}" >/dev/null ;;
    project) gcloud projects "$verb-iam-policy-binding" "$target" \
               --member="$member" --role="$role" "${c[@]}" --quiet >/dev/null ;;
    repo)    gcloud artifacts repositories "$verb-iam-policy-binding" "$target" \
               --location="$REGION" --project="$PROJECT_ID" \
               --member="$member" --role="$role" --quiet >/dev/null ;;
    sa)      gcloud iam service-accounts "$verb-iam-policy-binding" "$target" \
               --project="$PROJECT_ID" --member="$member" --role="$role" --quiet >/dev/null ;;
  esac
}

up() {
  echo "service accounts"
  for id in train serve ci; do
    gcloud iam service-accounts describe "$(sa $id)" --project "$PROJECT_ID" >/dev/null 2>&1 \
      || gcloud iam service-accounts create "itcs355-lab5-$id" --project "$PROJECT_ID" \
           --display-name="ITCS355 Lab 5 $id identity" --quiet
  done
  # A new SA takes a few seconds to be visible to every IAM policy that names it.
  sleep 10
  echo "bindings"
  while IFS= read -r g; do apply add "$g"; done < <(grants)
  echo
  echo "TRAIN_SA=$TRAIN"
  echo "SERVE_SA=$SERVE"
  echo "CI_SA=$CI"
}

down() {
  echo "bindings"
  while IFS= read -r g; do apply remove "$g" || true; done < <(grants)
  echo "service accounts"
  for s in "$TRAIN" "$SERVE" "$CI"; do
    gcloud iam service-accounts delete "$s" --project "$PROJECT_ID" --quiet || true
  done
}

show() {
  for s in "$TRAIN" "$SERVE" "$CI"; do
    echo "== $s"
    echo "-- project"
    gcloud projects get-iam-policy "$PROJECT_ID" --flatten='bindings[].members' \
      --filter="bindings.members:serviceAccount:$s" --format='value(bindings.role)'
    echo "-- bucket gs://$BUCKET"
    gcloud storage buckets get-iam-policy "gs://$BUCKET" --format=json | python3 -c "
import json, sys
for b in json.load(sys.stdin).get('bindings', []):
    if 'serviceAccount:$s' in b['members']:
        print(b['role'], '|', b.get('condition', {}).get('title', ''))"
    echo "-- repo $AR_REPO"
    gcloud artifacts repositories get-iam-policy "$AR_REPO" --location "$REGION" \
      --project "$PROJECT_ID" --flatten='bindings[].members' \
      --filter="bindings.members:serviceAccount:$s" --format='value(bindings.role)'
    echo "-- may act as / be acted as"
    for t in "$TRAIN" "$SERVE" "$CI"; do
      gcloud iam service-accounts get-iam-policy "$t" --project "$PROJECT_ID" \
        --flatten='bindings[].members' --format='value(bindings.role,bindings.members)' \
        | awk -v s="$s" -v t="$t" '$2 ~ s {print "  " $1 " on " t}'
    done
  done
}

# The removal experiment: take one grant out (or put it back) by matching its line.
one() {
  local verb="$1" pat="${2:?pattern, e.g. lab5-train-read-data}" n=0 g
  while IFS= read -r g; do
    [[ "$g" == *"$pat"* ]] || continue
    apply "$verb" "$g"; n=$((n + 1))
  done < <(grants)
  [[ $n -eq 1 ]] || { echo "pattern matched $n grants, want exactly 1"; exit 1; }
}

case "$CMD" in
  up) up ;; down) down ;; show) show ;;
  revoke) one remove "${1:-}" ;; grant) one add "${1:-}" ;;
  *) echo "usage: iam-lab5.sh up|down|show [--impersonate user:<email>]"; exit 2 ;;
esac
