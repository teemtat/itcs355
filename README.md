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
make pull-runs                # copy the runs back down from the bucket
make compare                  # ranks runs by score and by cost
make register RUN=3895bd37    # adds the lineage tags, promotes to Staging
make reload-check VERSION=1   # loads it back out of the registry
make teardown
```

The results table and my 200-word write-up are in
[`reports/lab2-comparison.md`](reports/lab2-comparison.md).

---

## Promoting a model to production

Before anything goes to production I'd want all of this to be true, checked by a script,
not by someone eyeballing it:

- all 8 lineage tags are on the version
- `reload_check.py` passes
- `metric_test` is within 0.01 of whatever is in production right now
- the commit is on main
- the study behind it changed more than one hyperparameter

**Who gets to press promote:** not me. I trained it, so of course I think it's good. The
person who gets called at 2am when it breaks should be the one who says yes. I open the
request, they approve it.

**Rollback:** point the `production` alias back at the old version. Should take under 5
minutes — no rebuilding, no retraining. That only works if the old version is still
sitting there, so I don't delete old versions, I just demote them.

Lab 4 turns that checklist into a CI gate. If I can't write a rule as a check, it's not
really a rule.

---

## Getting the registry back

`mlflow.db` and the run folders are gitignored. The runs are 114 MB, which shouldn't go
in git. So if you clone this repo you get no registry and no model files.

To build it back:

```bash
make restore-registry RUN=3895bd37
```

That pulls the runs out of the bucket, registers version 1 again with the same 8 tags,
then runs the reload check. I don't type the tags in — they get read off the run and off
`reports/lab2-jobs.jsonl`.

It stops instead of guessing. If `data/raw/sensors.csv` doesn't hash to what the run
actually used, then the DVC hash sitting on my disk isn't the version that trained the
model. Writing it down anyway would be worse than leaving it blank, because a wrong tag
looks fine until someone tries to rebuild the thing.

**What's bad about this:** the registry points at a folder path on my laptop. A real
registry keeps the files in object storage. I need to fix that in Lab 3.

---

## Lab 2 — what happened

**Which permission failed on my first job:** none. I expected one, the lab handout says to
expect one, but the job runs as `52907215794-compute@developer.gserviceaccount.com` and
that account could already read my bucket — same project, and it has Editor. My first job
died for a totally different reason: MLflow 3 won't use a file store unless you set
`MLFLOW_ALLOW_FILE_STORE=true`.

I did hit a permission error later, uploading to the Vertex model registry:

```
FAILED_PRECONDITION: Vertex AI Service Agent service-52907215794@gcp-sa-aiplatform
does not have permission to access Artifact Registry repository
projects/vertex-ai/locations/asia-southeast1/repositories/prediction
```

It reads like a permissions problem but it isn't one. Google's prebuilt serving images
live at `asia-docker.pkg.dev`, not at `asia-southeast1-docker.pkg.dev`, so that repo just
doesn't exist. I figured it out by uploading with one of my own images instead — same
service account, worked fine. So it was never about the account.

**The run I registered:** `3895bd37fc694c9982cc94ea2f209fb8`, registered as
`itcs355-6688143` version 1, promoted to Staging.

**Seed variance:** I reran my best config at 5 different model seeds, keeping the split the
same. Mean 0.8407, stdev 0.0014, spread 0.0033. In Lab 1 I changed the seed for the split
too and got a spread of 0.05. Those two numbers mean different things — one is "how shaky
is this model", the other is "how much does it matter which machines ended up in training".

**Cost to retrain:** 0.051 THB per run on a spot n1-standard-4. Once a week is 0.22 THB a
month. That's basically free, so money isn't the reason not to retrain more often — every
retrain is another model someone has to look at and approve, and that's the part that
actually costs something.

**Interruption:** I killed the study on purpose partway through to check the checkpoint
worked. It did — the 5 finished trials were in the bucket, and the next job skipped them
and carried on at trial 06. Logs are in
[`reports/lab2-interruption.md`](reports/lab2-interruption.md).

**What Lab 2 cost me:** 2.82 THB out of 150. The real number is closer to 3.1. One job I
submitted with `--no-wait`, so nothing ever wrote down how long it ran, and then
`make teardown` deleted it. So my own cost tracking has a hole in it exactly when I fire a
job and walk away.

---

## Notes

- `make reproduce` only needs Docker — it builds the data and trains inside the container, so it
  doesn't depend on your machine's Python at all.
- Image is pushed to Artifact Registry, digest-pinned. `dvc push` is done, remote is a private
  bucket (grader would need read access to `dvc pull`, but that's not needed for `make reproduce`).
- `make teardown` cancels anything still running and removes failed jobs. It keeps
  SUCCEEDED ones, because a registered version's `training_job_id` points at one;
  `make teardown PURGE=True` removes those too.
