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

## Lab 2 — how to run it

```bash
make tune-remote              # 16 trials + 5 seeds, on a spot machine
make pull-runs                # copy the runs back from the bucket
make compare                  # ranks by score and by cost
make register RUN=3895bd37    # adds lineage tags, promotes to Staging
make reload-check VERSION=1   # loads it back out of the registry
make teardown
```

Results and my write-up: [`reports/lab2-comparison.md`](reports/lab2-comparison.md)

---

## Promoting a model to production

Has to pass all of this first, by script, not by eye:

- all 8 lineage tags on the version
- `reload_check.py` passes
- `metric_test` within 0.01 of what's in production now
- the commit is on main
- the study changed more than one hyperparameter

**Who presses promote:** not me. I trained it, so of course I like it. The person who gets
called at 2am when it breaks should be the one who says yes.

**Rollback:** point the `production` alias back at the old version. Under 5 minutes, no
rebuild. Only works if the old version is still there, so I demote old ones, never delete.

Lab 4 turns that list into a CI gate.

---

## Getting the registry back

`mlflow.db` and the run folders are gitignored — 114 MB shouldn't go in git. So a fresh
clone has no registry.

```bash
make restore-registry RUN=3895bd37
```

Pulls the runs from the bucket, registers version 1 again with the same 8 tags, runs the
reload check. I don't type the tags in, they get read off the run.

It stops instead of guessing. If `data/raw/sensors.csv` doesn't hash to what the run used,
the DVC hash here isn't the one that trained the model. A wrong tag looks fine right up
until someone tries to rebuild.

**Bad part:** the registry points at a folder on my laptop. A real one keeps the files in
object storage. That's a Lab 3 problem.

---

## Lab 2 — what happened

**Which permission failed on my first job:** none. The job runs as the default compute
account, which could already read my bucket. It died on something else — MLflow 3 won't
use a file store unless you set `MLFLOW_ALLOW_FILE_STORE=true`.

One did show up later, uploading to the Vertex registry:

```
FAILED_PRECONDITION: Vertex AI Service Agent ... does not have permission to access
Artifact Registry repository .../asia-southeast1/repositories/prediction
```

Not actually a permissions problem. Google's prebuilt images live at
`asia-docker.pkg.dev`, not `asia-southeast1-docker.pkg.dev`, so that repo doesn't exist. I
proved it by uploading with one of my own images — same account, worked fine.

**Run I registered:** `3895bd37fc694c9982cc94ea2f209fb8`, version 1, Staging.

**Seed variance:** best config rerun at 5 seeds, same split — stdev 0.0014, spread 0.0033.
In Lab 1 I changed the split too and got 0.05. Different questions: how shaky the model is,
versus how much it matters which machines landed in training.

**Retrain cost:** 0.051 THB a run, so weekly is 0.22 THB a month. Basically free — cost
isn't what stops me retraining more often, it's that every retrain needs someone to check
and approve it.

**Interruption:** I killed the study halfway on purpose. The 5 finished trials survived in
the bucket, and the next job skipped them and carried on at trial 06. Logs in
[`reports/lab2-interruption.md`](reports/lab2-interruption.md).

**What it cost:** 2.82 THB out of 150. Really about 3.1 — one job I ran with `--no-wait`
never got its duration written down, then `make teardown` deleted it.

---

## Notes

- `make reproduce` only needs Docker — it builds the data and trains inside the container, so it
  doesn't depend on your machine's Python at all.
- Image is pushed to Artifact Registry, digest-pinned. `dvc push` is done, remote is a private
  bucket (grader would need read access to `dvc pull`, but that's not needed for `make reproduce`).
- `make teardown` cancels anything still running and removes failed jobs. It keeps
  SUCCEEDED ones, because a registered version's `training_job_id` points at one;
  `make teardown PURGE=True` removes those too.
