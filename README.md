# ITCS355 — Reproducible Training (Lab 1) and Tracking & Registry (Lab 2)

> **Course materials live in [`course/`](course/README.md)** — syllabus, slides, the faculty
> specification, all five lab handouts, and the project brief. Every document is Markdown and
> renders on GitHub, diagrams included. New to the repo? Start with the
> [portability reference](course/reference/cloud-portability-reference.md).
> Keep this block when you edit the rest of this file; it is not part of the Lab 1 deliverable.

This model predicts if a machine will fail within 7 days, using sensor readings. The point of
the lab isn't the model itself — it's making sure anyone can clone this repo and get the exact
same result I got.

---

## How to run it

You need Docker, git, and make. Nothing else — no Python setup, no cloud login.

```bash
make reproduce
```

expected test_roc_auc: 0.8482 ± 0.0010

Check the result matches:

```bash
make verify
```

Takes about 3–4 minutes the first time (building the image), then ~10 seconds after that.

---

## What's in the data

240 machines, 25 readings each, 6 sensor features. Target is whether the machine fails within
7 days (about 12% of rows are a "yes"). Data is fake/generated (`scripts/make_dataset.py`), and
tracked with DVC so the exact version is recorded, not just the file.

One important thing: I split train/val/test **by machine**, not by row. If rows from the same
machine end up in both train and test, the model can basically cheat by recognizing the machine
instead of actually learning anything. There's a test for this in `tests/test_data.py`.

---

## Why ± 0.0010

I ran the same seed twice — got the exact same number both times. Ran it on a different CPU
(arm64 vs amd64) — difference was 0.000013. Then I changed the seed across 5 runs and the score
moved by as much as 0.05. So 0.0010 is generous enough to cover small CPU differences, but way
too tight to hide an actual bug like a missing seed.

---

## Runs I tracked

Did 10 runs total, logged in MLflow with params, both val/test scores, commit hash, and the
data version:

- 5 runs changing `max_depth` (3, 5, 8, 12, 16) — deeper trees start overfitting, best was depth 5
- 5 runs changing only the seed — scores ranged from 0.823 to 0.873, which is where the ± above
  comes from

---

## If I had to drop one thing (hashes / digest pin / seed)

I'd drop the hashes first. The version numbers (`==`) already lock which package version gets
installed — hashes just add protection against a file getting swapped out, which is rare. Losing
the digest pin or the seed breaks things way more often: the base image gets rebuilt without any
commit from me, and without a fixed seed my score bounces around by 0.05, way more than my
tolerance.

---

## Promoting a model to production

**What must be true** — all of these, checked automatically, not by eye:

- all eight lineage tags present on the model version
- `reload_check.py` passes against the version as it sits in the registry
- `metric_test` within 0.01 of the current production version, or a written exception
- the version's `git_commit` exists on the main branch
- the study behind it varied more than one hyperparameter

**Who may do it** — not the person who trained it. The author opens the request; whoever
owns the serving system approves it, because they are the one who gets called when it
misbehaves. Separating those two roles is the point: the person who spent a week on a
model is the worst placed to judge whether it is worth the risk of shipping.

**What the rollback is** — repoint the `production` alias at the previous version. Under
five minutes, no rebuild, no retrain. This only works while the previous version's
artifacts are still in the registry, so old versions are demoted, never deleted.

Lab 4 turns the first block into a CI gate. If a rule cannot be expressed as a check, it
is not a rule.

---

## Lab 2 evidence

**The permission that failed on first submission** — none. The job's runtime identity
(`52907215794-compute@developer.gserviceaccount.com`) already had bucket access: the
bucket is in the same project and the default compute service account holds project
Editor. The first job failed for an unrelated reason — MLflow 3 refuses a file-store
backend unless `MLFLOW_ALLOW_FILE_STORE=true`.

A permission error did appear later, uploading to the Vertex Model Registry:

```
FAILED_PRECONDITION: Vertex AI Service Agent service-52907215794@gcp-sa-aiplatform
does not have permission to access Artifact Registry repository
projects/vertex-ai/locations/asia-southeast1/repositories/prediction
```

It is not an IAM problem. Prebuilt serving containers are published to multi-region hosts
(`asia-docker.pkg.dev`), not per-region ones, so that repository does not exist. Uploading
with an image from our own registry succeeded under the same service agent, which is what
separated the two explanations.

**Chosen run** — `3895bd37fc694c9982cc94ea2f209fb8`, registered as `itcs355-6688143`
version 1, promoted to Staging.

**Seed variance** — five model seeds with the split held fixed: mean 0.8407, stdev 0.0014,
spread 0.0033. Varying the split as well, as Lab 1 did, gave a spread of 0.05. The two
numbers answer different questions: how stable this model is, and how much the answer
depends on which machines landed in training.

**Retraining cost** — 0.051 THB per run on n1-standard-4 spot. Weekly retraining is 0.22
THB/month. At that price compute is not what limits retraining frequency; every retrain
produces a model someone has to evaluate and promote, and that review is the real cost.

**Total Lab 2 spend** — 2.82 THB recorded against a 150 THB budget. The true figure is
nearer 3.1 THB: one job was submitted with `--no-wait`, so nothing ever wrote its duration
back, and `make teardown` then deleted the job. Cost accounting that depends on the
submitting process staying alive loses data exactly when a job is fired and forgotten.

---

## Notes

- `make reproduce` only needs Docker — it builds the data and trains inside the container, so it
  doesn't depend on your machine's Python at all.
- Image is pushed to Artifact Registry, digest-pinned. `dvc push` is done, remote is a private
  bucket (grader would need read access to `dvc pull`, but that's not needed for `make reproduce`).
- `make teardown` cancels anything still running and removes failed jobs. It keeps
  SUCCEEDED ones, because a registered version's `training_job_id` points at one;
  `make teardown PURGE=True` removes those too.
