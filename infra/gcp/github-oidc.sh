#!/usr/bin/env bash
# Lab 4 — let GitHub Actions deploy without a stored key. Run once:
#
#   bash infra/gcp/github-oidc.sh teemtat/itcs355
#
# GitHub signs a short-lived OIDC token for each workflow run. This pool trusts those
# tokens only when they come from REPO's main branch, and lets them act as one service
# account with four roles. A pull request, even from a branch in this repository, gets
# no cloud credentials at all. Nothing secret is created, so nothing can leak: the two
# values printed at the end are identifiers, stored as GitHub secrets only to keep them
# out of logs.
#
# Provider-specific by design: it lives in infra/gcp/, not in src/.
set -euo pipefail

REPO="${1:?usage: github-oidc.sh <owner/repo>}"
PROJECT_ID=$(sed -n 's/^PROJECT_ID=//p' cloud.env)
POOL=github
PROVIDER=github-oidc
SA_NAME=gh-deployer
SA="$SA_NAME@$PROJECT_ID.iam.gserviceaccount.com"
NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
RUNTIME_SA="$NUMBER-compute@developer.gserviceaccount.com"   # what Cloud Run runs as

gcloud services enable iamcredentials.googleapis.com sts.googleapis.com \
  cloudscheduler.googleapis.com --project "$PROJECT_ID"

gcloud iam workload-identity-pools describe "$POOL" --location=global --project "$PROJECT_ID" >/dev/null 2>&1 \
  || gcloud iam workload-identity-pools create "$POOL" --location=global --project "$PROJECT_ID" \
       --display-name="GitHub Actions"

gcloud iam workload-identity-pools providers describe "$PROVIDER" --workload-identity-pool="$POOL" \
  --location=global --project "$PROJECT_ID" >/dev/null 2>&1 \
  || gcloud iam workload-identity-pools providers create-oidc "$PROVIDER" \
       --workload-identity-pool="$POOL" --location=global --project "$PROJECT_ID" \
       --issuer-uri="https://token.actions.githubusercontent.com" \
       --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
       --attribute-condition="assertion.repository=='$REPO' && assertion.ref=='refs/heads/main'"

gcloud iam service-accounts describe "$SA" --project "$PROJECT_ID" >/dev/null 2>&1 \
  || gcloud iam service-accounts create "$SA_NAME" --project "$PROJECT_ID" \
       --display-name="GitHub Actions deployer (Lab 4)"

# What the pipeline does, and nothing else:
#   artifactregistry.writer  push the tested image
#   run.admin                deploy the staging revision (developer cannot set its IAM)
#   run.invoker              smoke-test the private service
#   aiplatform.viewer        resolve name@version in the model registry to its artifact
for role in roles/artifactregistry.writer roles/run.admin roles/run.invoker roles/aiplatform.viewer; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$SA" \
    --role="$role" --condition=None --quiet >/dev/null
done
# Deploying a revision that runs as the runtime account needs actAs on that account only.
gcloud iam service-accounts add-iam-policy-binding "$RUNTIME_SA" --project "$PROJECT_ID" \
  --member="serviceAccount:$SA" --role=roles/iam.serviceAccountUser --quiet >/dev/null

# The federated identity (this repo, any run the condition admits) may act as the SA.
gcloud iam service-accounts add-iam-policy-binding "$SA" --project "$PROJECT_ID" \
  --role=roles/iam.workloadIdentityUser --quiet >/dev/null \
  --member="principalSet://iam.googleapis.com/projects/$NUMBER/locations/global/workloadIdentityPools/$POOL/attribute.repository/$REPO"

echo "GCP_WIF_PROVIDER=projects/$NUMBER/locations/global/workloadIdentityPools/$POOL/providers/$PROVIDER"
echo "GCP_DEPLOY_SA=$SA"
