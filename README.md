# ITCS355 — Reproducible Training (Lab 1), Tracking & Registry (Lab 2), Serving (Lab 3)

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
make tune-remote              # 16 trials + 5 seeds, spot machine
make pull-runs                # copy the runs back from the bucket
make compare                  # ranks by score and by cost
make register RUN=3895bd37    # adds lineage tags, promotes to Staging
make reload-check VERSION=1   # loads it back out of the registry
make teardown
```

Results and my write-up: [`reports/lab2-comparison.md`](reports/lab2-comparison.md)

---

## Promoting a model to production

Checks that must pass first, run by a script:

- all 8 lineage tags on the version
- `reload_check.py` passes
- `metric_test` within 0.01 of the current production version
- the commit is on main
- the study varied more than one hyperparameter

**Who can promote:** the person who owns the serving system, not the person who trained
the model. The author opens the request, the owner approves it.

**Evidence the owner should require:** the checks above, plus the comparison report for
the study behind the version.

**Rollback:** point the `production` alias at the previous version. Under 5 minutes, no
rebuild. Old versions are demoted, not deleted.

Lab 4 turns these checks into a CI gate.

---

## Getting the registry back

`mlflow.db` and the run folders are gitignored — 114 MB. A fresh clone has no registry.

```bash
make restore-registry RUN=3895bd37
```

It pulls the runs from the bucket, registers version 1 with the same 8 tags, then runs the
reload check. The tags are read off the run and off `reports/lab2-jobs.jsonl`.

It fails instead of guessing: if `data/raw/sensors.csv` does not hash to what the run
used, the DVC hash here is not the version that trained the model.

**Limitation:** the registry source is a folder path on my laptop, not object storage.
Fixing that is Lab 3.

---

## Lab 2 — what happened

**Permission that failed on the first job:** none. The job runs as the default compute
service account, which already had read access to the bucket. The first job failed because
MLflow 3 rejects a file store unless `MLFLOW_ALLOW_FILE_STORE=true` is set.

A permission error did appear later, uploading to the Vertex model registry:

```
FAILED_PRECONDITION: Vertex AI Service Agent ... does not have permission to access
Artifact Registry repository .../asia-southeast1/repositories/prediction
```

Cause: prebuilt serving images are at `asia-docker.pkg.dev`, not
`asia-southeast1-docker.pkg.dev`. The repository in the message does not exist. Uploading
with an image from my own registry worked under the same service account.

**Run registered:** `3895bd37fc694c9982cc94ea2f209fb8`, version 1, Staging.

**Seed variance:** best config rerun at 5 seeds with the split fixed. stdev 0.0014, spread
0.0033. Lab 1 varied the split as well and got a spread of 0.05.

**Retrain cost:** 0.051 THB per run. Weekly is 0.22 THB per month.

**Interruption:** I cancelled the study after 5 trials. The checkpoint in the bucket
survived, and the next job skipped those 5 and resumed at trial 06. Logs in
[`reports/lab2-interruption.md`](reports/lab2-interruption.md).

**Cost:** 2.82 THB of 150 recorded. Actual is closer to 3.1 — one job ran with
`--no-wait`, so its duration was never written down, and `make teardown` deleted it.

---

## Lab 3 — how to run it

```bash
make serve-image-push                    # build, push, digest goes to reports/lab3/serve-image.txt
make deploy VERSION=1 INSTANCE=2cpu-4gi  # Cloud Run, model pulled from the registry by version
make smoke                               # 3 known payloads: single, batch, one that must 422
make loadtest LABEL=2cpu                 # k6 at 1, 10, 50 concurrent users
make teardown                            # deletes the service. Run it the moment you finish
```

Canary run: `python scripts/canary_watch.py --stable <v1 revision> --canary <v2 revision> --split 90 --tag run90`

Results and my write-up: [`reports/lab3-load.md`](reports/lab3-load.md)

---

## Why Cloud Run and not a Vertex endpoint

A Vertex endpoint only forwards one predict route, so `/predict/batch` would be unreachable
without changing the service. Cloud Run forwards every path as-is and splits traffic
between revisions natively. It also takes about 40 seconds to deploy instead of 15+ minutes.

The model still comes from the Vertex registry. `deploy()` looks up `name@version`, finds
the `gs://` folder the registry recorded, and passes it to the container. The container
downloads it once at startup through the adapter. That fixes the Lab 2 limitation: the
served model comes from object storage, not a folder on my laptop.

Where the platform difference lives: the startup probe points at `/ready` and liveness at
`/health`. That is configured in `cloudlayer/gcp.py`, not in the Dockerfile. The image is
the same one that would go to any provider.

---

## Latency target

**p95 under 200 ms for one-row `/predict`, 10 users at once, measured from my laptop.**

I committed it in `loadtest/k6.js` (commit `5242792`, 11:49) before the first load test
started (~11:51). 200 ms because a dashboard update feels instant below that. 10 because that is
roughly how many dashboards would be open at peak.

---

## Lab 3 — what happened

**Did it meet the target:** not on 1 vCPU (p95 305 ms at 10 users). Yes on 2 vCPU
(179 ms). Config that meets it: `2cpu-4gi`, one instance always on.

**Breaking point:** 9 users on 1 vCPU, 18 on 2 vCPU. No errors at any level. It just gets
slower. Once it is overloaded, the server's own timer still says ~8 ms while the client
waits 400+ ms, because requests queue before they reach the app. Only the client-side
number shows it.

**Batch:** one call with 100 rows takes about the same time as one call with 1 row. So 100
rows in one batch is ~87× faster than 100 separate calls. Most of the cost is the round
trip, not the model.

**Payload size:** up to the 100-row cap, the model is still most of the server time.
Parsing and converting only overtake it at about 1,000 rows (~150 KB), which the API
does not allow anyway.

**Bigger instance:** 2× the price, 2× the throughput. Cost per prediction is the same. The
extra money buys headroom, not efficiency.

**Cold start:** 8–15 seconds when scaled to zero. Most of it is loading the model. That is
why I keep one instance warm.

**Canary:** v2 is a model that looks fine on AUC (−0.005) but pushes every probability up.
At 90/10, the detector caught it in **196 s** by watching the average predicted
probability for the whole endpoint. It never looks at which version answered. Rollback took
6 s. At 50/50 it caught it in 34 s, but half the users got the bad model while it did.
Log loss never showed anything, too diluted at 10%.

**Evidence traffic moved:** every response carries its model version, and Cloud Monitoring
counts requests per revision. Both show v2 at ~10% during the canary and 0 after the
rollback. Chart: [`reports/lab3/canary/canary.png`](reports/lab3/canary/canary.png)

**Cost per 1,000 predictions:** 0.076 THB, assuming 25% utilisation. At 5% it is 0.38 THB,
so this number is only as good as that guess. The warm instance is 152 THB a day whether
anyone calls it or not. A daily batch job would be cheaper at any volume this endpoint can
handle. The endpoint is only worth it if predictions are needed right away.

**Bug found:** registering version 2 failed. Lab 2's `register_model` passed a bare model
id as `--parent-model`, and Vertex needs the full resource name. It also returned the wrong
version number. Both are fixed in `cloudlayer/gcp.py`.

**Teardown:** done at 06:08 UTC, 0 services left, `make cost-report` passes. Record in
[`reports/lab3/teardown.txt`](reports/lab3/teardown.txt).

---

## Notes

- `make reproduce` only needs Docker — it builds the data and trains inside the container, so it
  doesn't depend on your machine's Python at all.
- Image is pushed to Artifact Registry, digest-pinned. `dvc push` is done, remote is a private
  bucket (grader would need read access to `dvc pull`, but that's not needed for `make reproduce`).
- `make teardown` cancels anything still running and removes failed jobs. It keeps
  SUCCEEDED ones, because a registered version's `training_job_id` points at one;
  `make teardown PURGE=True` removes those too.
- Since Lab 3, `make teardown` defaults to `LAB=3` and also deletes the Cloud Run service.
  For Lab 2 resources use `make teardown LAB=2`.
- The serving image does not include MLflow. The deployed service never uses it, and leaving
  it out keeps the image small, which shortens cold starts.
