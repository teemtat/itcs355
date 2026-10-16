# Lab 5 Task 2 — Least privilege, properly

Project `itcs355-6688143`, region `asia-southeast1`. Script: [infra/gcp/iam-lab5.sh](../infra/gcp/iam-lab5.sh)
(`up` / `down` / `show` / `revoke|grant <pattern>`). Experiment: [infra/gcp/iam-lab5-experiment.sh](../infra/gcp/iam-lab5-experiment.sh).
Raw output: [reports/lab5/iam/](lab5/iam/).

## 1. What Labs 1–4 actually ran as

| Step | Where it runs | Identity | What that identity holds |
|---|---|---|---|
| `make train-remote`, `tune-remote`, `register`, `deploy`, `teardown` (laptop) | gcloud CLI via `cloudlayer/gcp.py` | `user:teemtat.wee@gmail.com` (`IDENTITY_REF`) | `roles/owner` on the project |
| Vertex custom jobs (Lab 2) | Vertex AI | `52907215794-compute@developer.gserviceaccount.com` (default compute SA). `submit_training` sets `serviceAccount` only when it is passed, and it never was | `roles/editor` on the project |
| Cloud Run `itcs355-staging` (Labs 3–4) | Cloud Run | same default compute SA. `deploy()` passes no `--service-account` | `roles/editor` |
| Drift job + Cloud Scheduler (Lab 4) | Cloud Run job, scheduler OIDC | same default compute SA (`infra/gcp/lab4-monitoring.sh`) | `roles/editor` |
| CI image push, CD deploy + smoke (Lab 4) | GitHub Actions via WIF pool `github`, provider `github-oidc` | `gh-deployer@…` | project-level `artifactregistry.writer`, `run.admin`, `run.invoker`, `aiplatform.viewer`, plus actAs on the default compute SA |

So in practice every workload ran as a project **editor**, and CI could administer every Cloud
Run service in the project, the capstone's `scamshield` included.

Not changed here, on purpose: the default compute SA keeps `roles/editor`, because the capstone
service `scamshield` still runs as it, and `gh-deployer` keeps its roles because the live CD
workflow still names it in `secrets.GCP_DEPLOY_SA`. Both are listed as follow-ups at the end.

## 2. The three identities

The bucket uses uniform bucket-level access, so bucket grants carry IAM conditions on the
object-name prefix. A *list* request is checked against the bucket rather than an object, so
`resource.name` cannot scope it. It is scoped through the `storage.googleapis.com/objectListPrefix`
request attribute instead.

### `itcs355-lab5-train@itcs355-6688143.iam.gserviceaccount.com`: Vertex pipeline and job SA

| Permission | Scope | Why it is needed |
|---|---|---|
| `roles/storage.objectViewer` (get) | objects under `itcs355/data/` | ingest/validate/train read the dataset (`data/raw/sensors.csv`) |
| `roles/storage.objectViewer` (list) | list requests with prefix `itcs355/data/` | the adapter reads data with `download_prefix` (`gcloud storage rsync`), and GCS FUSE lists directories. A get alone is not enough (Experiment A) |
| `roles/storage.objectUser` | objects under, and lists of, `itcs355/lab5/` | pipeline root, checkpoints, model and `metrics.json`, the registry artifact (`itcs355/lab5/registry/`). It needs delete as well as create, because checkpoints and the pipeline root are overwritten |
| `roles/aiplatform.user` | project | the pipeline's own steps create custom jobs, write ML Metadata/metrics, and `register` uploads a model version. Vertex has no per-resource IAM for these, so this is the narrowest grant available |
| `roles/iam.serviceAccountUser` | on **itself** only | a pipeline step that launches a custom job "as" the pipeline SA needs actAs on that SA |

It cannot read the Lab 2/3 registries, push images, touch Cloud Run, or write to `itcs355/data/`.

### `itcs355-lab5-serve@itcs355-6688143.iam.gserviceaccount.com`: Cloud Run staging runtime SA

| Permission | Scope | Why it is needed |
|---|---|---|
| `roles/storage.objectViewer` (get only) | objects under `itcs355/lab2-registry/` and `itcs355/lab5/registry/` | the container fetches `MODEL_URI` at start-up with a metadata-server token (`_download_rest`). It fetches one named object, so no list grant |
| *(no logging or monitoring role)* | | stdout and stderr are collected by Cloud Run itself, and the Lab 4 dashboard metric is log-based, so nothing writes the Logging or Monitoring API as this SA. Experiment B checks this |
| *(no Artifact Registry role)* | | Cloud Run pulls the image as its service agent, not as the runtime SA |

### `itcs355-lab5-ci@itcs355-6688143.iam.gserviceaccount.com`: GitHub Actions (OIDC)

| Permission | Scope | Why it is needed |
|---|---|---|
| `roles/artifactregistry.writer` | repository `itcs355` only | push the tested serving and training images. `gh-deployer` had this on the whole project |
| `roles/aiplatform.user` | project | submit the PipelineJob, and resolve `name@version` in the model registry at deploy |
| `roles/run.developer` | project | deploy the staging revision and invoke it for the smoke test (includes `run.routes.invoke`). This replaces `run.admin` and `run.invoker`, so CI can no longer change any service's IAM policy. It cannot be scoped to `itcs355-staging`, because teardown deletes that service and the first deploy re-creates it |
| `roles/iam.serviceAccountUser` | on `itcs355-lab5-train` and `itcs355-lab5-serve` only | submitting a pipeline that runs as the training SA, and deploying a revision that runs as the serving SA, are both "act as" that SA (Experiment B). It does **not** have actAs on the default compute SA, which is the one `scamshield` runs as |
| `roles/iam.workloadIdentityUser` granted *to* `principalSet://…/workloadIdentityPools/github/attribute.repository/teemtat/itcs355` | on this SA | the existing `github-oidc` provider (main branch of `teemtat/itcs355` only) may become this SA. The capstone's `scamshield-oidc` is not referenced |

Temporary, and only for this experiment: `roles/iam.serviceAccountTokenCreator` for
`user:teemtat.wee@gmail.com` on each of the three SAs (`up --impersonate`). Owner does not include
`getAccessToken` on a service account. `down` removes these grants with everything else.

## 3. The removal experiment

PENDING: the run of `infra/gcp/iam-lab5-experiment.sh` has not happened yet. This section is filled
from `reports/lab5/iam/A-train-list.log` and `B-ci-actas.log` after it runs, not before.

**A. Training identity without `storage.objects.list` on its data prefix.** Belief: needed.
The training step reads one CSV, so "read data" sounds like `get`. The adapter's read path is
`download_prefix` → `gcloud storage rsync`, which lists first.

**B. CI identity without actAs on the serving SA.** Belief: needed. CI holds `run.developer`,
which looks like it is enough to deploy. The experiment deploys a revision whose runtime identity
is the serving SA.

## 4. Identity propagation: why a scheduled run is not your run

When you run `make pipeline` (or `PipelineJob.submit()`) from the laptop, the **caller** is
`user:teemtat.wee@gmail.com`, an owner. If `service_account` is left out, every step runs as the
default compute SA, which is an editor. Both hold everything, so nothing ever fails.

A scheduled run has no laptop. Vertex Schedules (or Cloud Scheduler → the API) start the
PipelineJob from a stored request, and the steps run as whatever `service_account` that request
names. The caller is the platform's service agent, or the scheduler's `--oauth-service-account-email`.
Three things follow:

1. A permission your user has and the pipeline SA lacks shows up only on the schedule. That is
   the "fails only when scheduled" row in the lab's failure table.
2. Whoever *creates* the schedule needs actAs on the SA the schedule will run as. That is why CI
   holds `serviceAccountUser` on the training SA and on nothing else.
3. Credentials propagate by reference, not by copy. Each hop (GitHub → WIF → `itcs355-lab5-ci`
   → pipeline as `itcs355-lab5-train` → revision as `itcs355-lab5-serve`) is a separate actAs
   check. Experiment B is the CI → serve hop failing.

To surface the problem before the schedule does, run the pipeline once **as the training SA**
(`--impersonate-service-account` or `service_account=` on submit), not as yourself.

## 5. How to wire these SAs in (for the pipeline and coordinator sessions)

| Step | What to set | Value |
|---|---|---|
| Vertex PipelineJob (manual and scheduled) | `aiplatform.PipelineJob(...).submit(service_account=...)`, or `create_schedule(..., service_account=...)` | `itcs355-lab5-train@itcs355-6688143.iam.gserviceaccount.com` |
| Pipeline root and outputs | `pipeline_root=` and every artifact or metrics path | under `gs://itcs355-6688143/itcs355/lab5/` (the training SA can write nothing else) |
| `register` step | model artifact upload destination | under `gs://itcs355-6688143/itcs355/lab5/registry/` (serving can read it) |
| Custom jobs launched outside the pipeline | `submit_training(..., args={"service_account": ...})`, which becomes the job spec's `serviceAccount` | training SA, as above |
| Cloud Run staging | `gcloud run deploy ... --service-account=...`. `GCPAdapter.deploy` does not pass this today, so add it there, or run `gcloud run services update itcs355-staging --service-account=...` after deploying | `itcs355-lab5-serve@itcs355-6688143.iam.gserviceaccount.com` |
| GitHub Actions | `google-github-actions/auth` `service_account:` (the `GCP_DEPLOY_SA` secret). `workload_identity_provider` is unchanged | `itcs355-lab5-ci@itcs355-6688143.iam.gserviceaccount.com` |
| `deploy_staging` *inside* the pipeline | runs as the training SA, which deliberately has no Cloud Run role | either let CI deploy after the pipeline's `register` succeeds (recommended), or add `roles/run.developer` plus actAs on the serving SA to the training SA in `iam-lab5.sh` and say so here |

Reproduce or remove:

```bash
bash infra/gcp/iam-lab5.sh up --impersonate user:teemtat.wee@gmail.com
bash infra/gcp/iam-lab5.sh show
bash infra/gcp/iam-lab5.sh down
```

## 6. Follow-ups not done here

- The default compute SA still holds `roles/editor`. Once `scamshield` runs as its own SA, remove
  that grant. Until then, any workload that forgets `--service-account` is an editor again.
- `gh-deployer` (Lab 4) still holds project-level `run.admin`, `run.invoker`,
  `artifactregistry.writer` and `aiplatform.viewer`. Retire it once the CD secret points at
  `itcs355-lab5-ci`.
