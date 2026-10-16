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
| `roles/storage.objectViewer` (get) | the object `itcs355/data` and objects under `itcs355/data/` | ingest/validate/train read the dataset (`data/raw/sensors.csv`) |
| `roles/storage.objectViewer` (list) | list requests with prefix `itcs355/data` or `itcs355/data/…` | the adapter reads data with `download_prefix` (`gcloud storage rsync`), and GCS FUSE lists directories. A get alone is not enough (Experiment A) |
| `roles/storage.objectUser` | objects under, and lists of, `itcs355/lab5/` | pipeline root, checkpoints, model and `metrics.json`, the registry artifact (`itcs355/lab5/registry/`). It needs delete as well as create, because checkpoints and the pipeline root are overwritten |
| `roles/aiplatform.user` | project | the pipeline's own steps create custom jobs, write ML Metadata/metrics, and `register` uploads a model version. Vertex has no per-resource IAM for these, so this is the narrowest grant available |
| `roles/iam.serviceAccountUser` | on **itself** only | a pipeline step that launches a custom job "as" the pipeline SA needs actAs on that SA |

It cannot read the Lab 2/3 registries, push images, touch Cloud Run, or write to `itcs355/data/`.

### `itcs355-lab5-serve@itcs355-6688143.iam.gserviceaccount.com`: Cloud Run staging runtime SA

| Permission | Scope | Why it is needed |
|---|---|---|
| `roles/storage.objectViewer` (get only) | objects under `itcs355/lab2-registry/` and `itcs355/lab5/registry/` | the container fetches `MODEL_URI` at start-up with a metadata-server token (`_download_rest`). It fetches one named object, so no list grant |
| *(no logging or monitoring role)* | | stdout and stderr are collected by Cloud Run itself, and the Lab 4 dashboard metric is log-based, so nothing writes the Logging or Monitoring API as this SA. Experiment B confirms the logs arrive |
| *(no Artifact Registry role)* | | Cloud Run pulls the image as its service agent, not as the runtime SA |

### `itcs355-lab5-ci@itcs355-6688143.iam.gserviceaccount.com`: GitHub Actions (OIDC)

| Permission | Scope | Why it is needed |
|---|---|---|
| `roles/artifactregistry.writer` | repository `itcs355` only | push the tested serving and training images. `gh-deployer` had this on the whole project |
| `roles/aiplatform.user` | project | submit the PipelineJob, and resolve `name@version` in the model registry at deploy |
| `roles/run.developer` | project | deploy the staging revision and invoke it for the smoke test (includes `run.routes.invoke`). Experiment B deployed with only this role. This replaces `run.admin` and `run.invoker`, so CI can no longer change any service's IAM policy. It cannot be scoped to `itcs355-staging`, because teardown deletes that service and the first deploy re-creates it |
| `roles/iam.serviceAccountUser` | on `itcs355-lab5-train` and `itcs355-lab5-serve` only | submitting a pipeline that runs as the training SA, and deploying a revision that runs as the serving SA, are both "act as" that SA (Experiment B). It does **not** have actAs on the default compute SA, which is the one `scamshield` runs as |
| `roles/iam.workloadIdentityUser` granted *to* `principalSet://…/workloadIdentityPools/github/attribute.repository/teemtat/itcs355` | on this SA | the existing `github-oidc` provider (main branch of `teemtat/itcs355` only) may become this SA. The capstone's `scamshield-oidc` is not referenced |

Temporary, and only for this experiment: `roles/iam.serviceAccountTokenCreator` for
`user:teemtat.wee@gmail.com` on each of the three SAs (`up --impersonate`). Owner does not include
`getAccessToken` on a service account. `down` removes these grants with everything else.

## 3. The removal experiment

Run on 2026-10-16 (UTC) with `infra/gcp/iam-lab5-experiment.sh`. Every command ran **as the lab
identity** through gcloud impersonation, and the adapter's own gcloud calls inherited it. The
quotes below are cut from the raw logs in [reports/lab5/iam/](lab5/iam/).

### A. Training identity without `storage.objects.list` on `itcs355/data/` (believed needed)

The training step reads one CSV, so "read data" sounds like `storage.objects.get` and nothing
else. I removed only the list grant (`iam-lab5.sh revoke lab5-train-list-data`) and ran the
step's real read path, `GCPAdapter.download_prefix("data", …)`, which calls
`gcloud storage rsync`. Log: [A-train-list.log](lab5/iam/A-train-list.log).

| UTC | As | Step | Result |
|---|---|---|---|
| 03:12:59 | train | `download_prefix(data)`, all grants present | `-> 1 files`, exit 0 |
| 03:13:19 | train | read `itcs355/lab2-registry/v1/model.joblib` (outside its prefixes) | `403 … does not have storage.objects.get access`, exit 1 |
| 03:13:22 | train | write `itcs355/data/raw/should-not-exist.txt` (a read-only prefix) | `does not have storage.objects.create access`, exit 1 |
| 03:13:32 | me | **revoke `lab5-train-list-data`** | |
| 03:13:37 | train | `download_prefix(data)` | **exit 1**, see below |
| 03:13:40 | train | `download("…/data/raw/sensors.csv")`, the same file by name | `ok`, exit 0 |
| 03:13:49 | me | **restore `lab5-train-list-data`** | |
| 03:13:53 | train | `download_prefix(data)` | `-> 1 files`, exit 0 |

What broke, verbatim:

```
RuntimeError: gcloud storage rsync --recursive gs://itcs355-6688143/itcs355/data /var/folders/…/tmp.OxZwz6fRJG --quiet failed (1):
ERROR: (gcloud.storage.rsync) [itcs355-lab5-train@itcs355-6688143.iam.gserviceaccount.com] does not have permission
to access b instance [itcs355-6688143] (or it may not exist): … does not have storage.objects.list access to the
Google Cloud Storage bucket. Permission 'storage.objects.list' denied on resource
'//storage.googleapis.com/projects/_/buckets/itcs355-6688143' (or it may not exist).
```

The **ingest/train data read** broke. At the same moment and as the same identity, the one CSV
still downloaded by name. So "read the data" means two permissions, because the code reads a
*prefix*, not a file. Note also that the error names the **bucket** and says "or it may not
exist", which reads like a typo in `BLOB_URI` rather than a missing grant. Bucket-level IAM took
effect in under 5 seconds in both directions.

**Two failures before the experiment.** The first two baseline runs failed with every grant I
had *intended* in place. Each failure showed that my condition was narrower than what gcloud
actually requests:

1. [A0-baseline-condition-too-tight.log](lab5/iam/A0-baseline-condition-too-tight.log). Before
   rsync lists anything, it GETs the bare name `itcs355/data` to ask whether the source is an
   object or a prefix. The condition `startsWith(".../objects/itcs355/data/")` excludes that
   name, so the GET returned 403 instead of 404, and rsync aborted:
   `Permission 'storage.objects.get' denied on resource '…/objects/itcs355/data'`.
   Fix: `dir()` in the script also admits the exact bare name.
2. [A0b-baseline-list-prefix-denied.log](lab5/iam/A0b-baseline-list-prefix-denied.log) and
   [A1-list-prefix-probe.log](lab5/iam/A1-list-prefix-probe.log). `--log-http` shows gcloud
   lists with `prefix=itcs355%2Fdata&delimiter=%2F`, without the slash, even for
   `gcloud storage ls gs://…/itcs355/data/`. A list condition of
   `objectListPrefix.startsWith("itcs355/data/")` therefore never matches anything gcloud sends.
   Fix: `lst()` admits `== "itcs355/data"` as well. The cost is that a listing can reveal the
   *names* of siblings such as `itcs355/data-old/`, but not their contents, because `get` is
   still scoped.

That is the practical lesson of the task: a least-privilege condition written from the docs was
wrong twice, and only running the real code path as the real identity found it. GCS FUSE in a
Vertex job makes the same "object or prefix?" probes, so the identical denial would have appeared
as an empty mount or "No such file" inside the training container.

The bindings in [01b-fix-condition.txt](lab5/iam/01b-fix-condition.txt) and
[01c-fix-list-condition.txt](lab5/iam/01c-fix-list-condition.txt) are the live ones.
[03-show.txt](lab5/iam/03-show.txt) lists each SA's grants.

### B. CI identity without actAs on the serving SA (believed needed)

CI holds `roles/run.developer`, which looks like everything a deploy needs. I removed CI's
`iam.serviceAccountUser` on `itcs355-lab5-serve`, then had CI deploy a staging-shaped service
(`--service-account itcs355-lab5-serve`, private, scale to zero, Google's `hello` image). Log:
[B-ci-actas.log](lab5/iam/B-ci-actas.log).

| UTC | As | Step | Result |
|---|---|---|---|
| 03:14:07 | me | **revoke actAs on serve for CI** (policy now shows only my TokenCreator) | |
| 03:14:13 | ci | `gcloud run deploy itcs355-lab5-iamprobe …` | **succeeded**, revision `-00001-cpb` |
| 03:14:56 | ci | same | **succeeded** |
| 03:15:41 | ci | same | **denied**, see below |
| 03:15:49 | me | **restore** | |
| 03:15:55 | ci | same | succeeded; the revision runs as `itcs355-lab5-serve@…` |
| 03:16:33 | me | delete the probe service | deleted |

What broke, verbatim:

```
ERROR: (gcloud.run.deploy) PERMISSION_DENIED: Permission 'iam.serviceaccounts.actAs' denied on service account
itcs355-lab5-serve@itcs355-6688143.iam.gserviceaccount.com (or it may not exist).
```

The **deploy-to-staging** step broke. The run permission was fine; the identity hand-off was
not. CI may create a revision, but not one that runs *as someone else*, without actAs on that
someone.

The finding I did not expect: **the removal took about 94 seconds to bite**. Two deploys
succeeded after the policy no longer contained the binding. The bucket change in A applied in
seconds, but a service-account policy change did not. If I had run the test once, I would have
concluded that actAs is unnecessary and deleted a permission that deploys need. Revoking a
leaked credential's access is not instant either.

Two things this run confirmed:

- `run.developer` is enough to deploy and smoke-test. Lab 4 gave `gh-deployer` `run.admin` plus
  `run.invoker`, and neither is needed.
- The serving SA holds **no** logging role, yet the container's stdout reached Cloud Logging
  (`run.googleapis.com/stderr  … Hello from Cloud Run! The container started successfully…`).
  "Write logs" for a Cloud Run runtime needs no grant, so none was given.

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
- Teardown: `bash infra/gcp/iam-lab5.sh down` removes every binding above (including the temporary
  TokenCreator grants on my user) and then the three SAs. Run it after the pipeline and staging
  no longer need them. IAM costs nothing, so leaving them until then is free.
