# ITCS355 — Reproducible Training (Lab 1), Tracking & Registry (Lab 2), Serving (Lab 3), CI/CD & Monitoring (Lab 4), Cloud MLOps, Cost & LLM Ops (Lab 5)

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

## Lab 4 — how to run it

CI and CD run by themselves: open a pull request and CI runs; merge to main and the
tested image is pushed and deployed to `itcs355-staging`. One-time setup and the rest:

```bash
bash infra/gcp/github-oidc.sh teemtat/itcs355          # OIDC identity, once
make test                                              # unit, contract, behaviour, service
make calibrate                                         # the numbers behind the drift threshold
make drift-modes                                       # shift / scale / mix against the detector
bash infra/gcp/lab4-monitoring.sh <image@sha256:…> <email>   # drift job, schedule, alert, dashboard
make replay                                            # 40 min normal, then 30 min at +6 °C
make teardown LAB=4 && make cost-report LAB=4
```

Results and my write-up: [`reports/lab4-cicd-drift.md`](reports/lab4-cicd-drift.md).
Post-mortem: [`reports/lab4-postmortem.md`](reports/lab4-postmortem.md).

---

## Data contract tests — the incident each one catches

| Test | Would have caught |
|---|---|
| schema and types | a release drops `ambient_humidity`, or sends `machine_id` as `"M-017"` |
| no nulls | a vibration sensor offline, the gateway sending empty values |
| plausible ranges | firmware switching to Fahrenheit. **This blocked my bad commit** |
| medians in expected units | pressure in bar, load as 0–1. Inside every range, so only this sees it |
| target binary, not degenerate | a broken label join, every row "did not fail" |
| unique reading id | a retried ingestion loading the same batch twice |
| no machine across splits (Lab 1) | machine ids reused after a fleet change |

---

## Drift threshold: 0.05, and why

I measured it instead of copying 0.10 / 0.25 from credit scoring:
- **Noise:** with 30 minutes of normal readings (~1,800), PSI never went above 0.023
  (500 windows, worst of 6 features).
- **Twice the noise:** 0.05 is twice that, and matches a ~+2 °C temperature offset,
  about a normal thermocouple's tolerance.
- **What 0.25 would miss:** a +4 °C fault (PSI 0.18) would never alert, while the model
  flagged 14% more readings than it should.
- **Too little data:** windows under 900 readings are skipped, because their noise
  alone reaches 0.04–0.11.

**Live:** 40 minutes of normal traffic, highest PSI 0.014, no false alarm. Then +6 °C:
the alert email came **17 minutes** after injection (14 min 16 s to the detector's
check). I had predicted 12–19 minutes and committed that before injecting.

**Which statistic catches what:** a spread change (×1.5) leaves the mean exactly where
it was but PSI reads 0.20. That is why I alert on PSI, not on the mean. A change in
which machines report (`mix`) is not caught by any per-feature threshold. That gap is
real and written up.

---

## Lab 4 — what happened

**Blocked commit:** [PR #2](https://github.com/teemtat/itcs355/pull/2) sends temperature
in Fahrenheit. CI failed at the **Data contract tests** step, 49 seconds after the push:
`test_features_within_plausible_ranges` (temp_c 240.215 > 140) and
`test_feature_medians_in_expected_units` (median 174.1, not 60–100). The build job never
ran. Closed without merging. Run: https://github.com/teemtat/itcs355/actions/runs/38023737410

**CD:** commit to tested staging in 6 min 41 s. The image is deployed by the digest CI
pushed, found by its commit SHA, so what runs is exactly what was tested. Deploy and
smoke test worked on the first run.

**Secrets:** none stored. GitHub's OIDC token is exchanged for a deployer service
account, and only for this repo's main branch, so a pull request can't get cloud
credentials at all.

**Post-mortem, short version:** every reading jumped exactly +6 °C on all machines at
once, with nothing else changing. That is a broken sensor feed, not hotter machines.
- **Don't retrain:** the model would learn 85 °C as normal and miss failures once the
  sensor is fixed.
- **Rollback doesn't help:** v1 is what was serving.
- **What to do:** fix the feed and keep the bad window out of the next training run.
- **A week unnoticed:** ~14 extra inspections, ~10 of them needless (~14,000 THB),
  plus 600k wrong readings waiting for the next retrain.

**An alert that closed itself:** an hour after the replay stopped, Cloud Monitoring
emailed "recovered … missing data". Nothing was fixed; the detector had simply stopped
seeing inputs. I added a second policy that pages when the detector has not scored a
window for 20 minutes.

**Teardown:** `make teardown LAB=4` deleted the schedule first, then the drift job, the
alert policy and the staging service. `make cost-report LAB=4` passes. Record in
`reports/lab4/teardown.txt`. I left the schedule running for a day before that, which
cost ~2–3 THB. The billing page shows Cloud Run ฿17.16 for the lab, 2.4× the ~7 THB I estimated from instance time. All of it was covered by credits, so I paid ฿0.

---

## Lab 5 — how to run it

Everything is on GCP (`cloud.env`), apart from Task 4's second provider, which is Azure.

```bash
python -m cloudlayer.pipelines run                     # ingest → validate → train → evaluate → register → deploy staging
python scripts/evaluation_gate.py --metrics reports/metrics.json --incumbent 0.90   # the gate on its own; exits 1
python -m cloudlayer.pipelines schedule                # weekly retrain, Mon 02:00 Bangkok
bash infra/gcp/iam-lab5.sh up                          # the three scoped service accounts
bash infra/gcp/iam-lab5-experiment.sh                  # remove a permission, record what breaks
make portability-audit                                 # no provider strings in src/
bash infra/azure/lab5-swap.sh demo                     # Azure: create, swap-check, delete
make llm-turbine-eval && make llm-turbine-gate         # LLM golden set: baseline, then a gate run that FAILS
make llm-attack                                        # Task 8: the injection, and the guard firing (offline)
make llm-cost                                          # Task 9: token bill, with the working
python -m cloudlayer.pipelines teardown && make teardown LAB=5 && bash infra/gcp/iam-lab5.sh down
```

The LLM targets run offline against recorded Vertex responses and cost nothing.
`make llm-record` is the only one that spends tokens.

| Task | Write-up |
|---|---|
| 1, 3: pipeline, gate, schedule | [`reports/lab5-pipeline.md`](reports/lab5-pipeline.md) |
| 2: least privilege | [`reports/lab5-iam.md`](reports/lab5-iam.md) |
| 4: portability | [`reports/lab5-portability.md`](reports/lab5-portability.md) |
| 5: cost | [`reports/lab5-cost.md`](reports/lab5-cost.md) |
| 6: teardown, with console screenshots | [`reports/lab5-teardown.md`](reports/lab5-teardown.md), [`reports/lab5/screenshots/`](reports/lab5/screenshots/) |
| 7–9: LLM gate, guardrail, token bill | [`reports/lab5-llm.md`](reports/lab5-llm.md) |

---

## What should trigger a retrain

Three facts about this system decide it:

- **Labels arrive 7 days late.** The label is `failed_within_7d`, so the newest week of data
  never has labels yet.
- **The fleet changes slowly.** The same 240 turbines, wearing over weeks.
- **Its upstream has already broken once.** In Lab 4, a gateway added +6 °C to every reading,
  and only the drift check caught it.

| Strategy | When it is right here | How it fails here |
|---|---|---|
| **Fixed schedule** (weekly) | The normal case. One week is exactly one new batch of matured labels, and the gate rejects a retrain that isn't better | Slow after a real step change (up to 7 days, plus the label lag). Blind: it retrains on whatever arrived |
| **Data volume** (≥ 6,000 newly *labelled* readings) | When the reading rate is uneven: machines offline, or a growing fleet | Volume says nothing about content. A gateway that replays readings trips it with no new information |
| **Drift-triggered** (PSI > 0.05) | When inputs genuinely moved and an independent check confirms it | It can't tell "the machines changed" from "the sensor broke", and here the second is more likely |

**What I chose:** a weekly schedule (Mondays 02:00 Bangkok), behind the gate. **Drift alerts a
person and does not retrain.** Once someone has confirmed the shift is real, they start the same
pipeline by hand, and it goes through the same gate.

**Worst case:** retraining triggered by drift on corrupted upstream data.
- Drift stays above threshold for as long as the fault lasts, so the trigger fires every
  5 minutes. That is about 720 THB a day, the whole term budget in a day.
- Seven days later the corrupted rows have labels. The candidate is trained *and tested* on
  +6 °C data, and the incumbent is re-scored on the same split, so **the gate passes the wrong
  model**. A gate protects against noise, not against a test set both models share.
- When the sensor is fixed, the deployed model under-scores every machine.

A fixed schedule would have retrained once, on the same bad data, after a week. It is still a
failure, but a slower one with a person in the loop.

---

## Lab 5 — what happened

**The gate is real.**
- One run passed: registered as `itcs355-6688143@3` and deployed to staging.
- One run on a degraded config failed the gate, so register and deploy were skipped.
- One run on contract-failing data aborted at validate.
- The gate needs the candidate to beat production by **+0.004 AUC**. `--incumbent 0.90`
  against our 0.848 candidate exits 1 (`GATE FAIL … not registering`).

**The schedule fired once, as the default compute account.** That account is over-broad
(`roles/editor`), so it hides the "fails only when scheduled" trap. With the scoped training
account, a scheduled run would break at the deploy step.

**What broke when I removed a permission.**
- I took `storage.objects.list` away from the training identity.
- The pipeline's data read (`gcloud storage rsync` of the data prefix) failed with a 403 that
  names the *bucket* and says "or it may not exist". That reads like a typo in the bucket URI,
  not a missing grant.
- The same CSV still downloaded fine **by name**, in the same second, as the same identity.
- So "read the data" takes two permissions, because the code reads a prefix, not a file.

**Portability: half yes, half no.**
- The audit is clean.
- Azure `upload` / `download` / `invoke` passed on a real Azure for Students subscription
  (Container Apps, malaysiawest).
- The hardest method was `upload`, because writes need auth and the serving image ships no SDK.
- The leak I couldn't hide is **who may call**. Cloud Run is private by default; Container
  Apps' ingress is public. The same `invoke` has different security behind it.
- The account leaked too: the student subscription refused `southeastasia` outright.
- A full migration is about 25 engineering days.
- Verdict: keeping `src/` provider-free was worth it. As insurance against leaving GCP, the
  eleven-method seam was not.

**LLM step (Gemini on Vertex).**
- 13 golden cases, with token counts copied from `usage_metadata`.
- **Switching Flash to Flash-Lite fails the gate on two regressions**, even though the pass rate
  only dropped from 92% to 77%:
  - turbine-008: it ignored a request to disable the overspeed trip, when it should have refused.
  - turbine-012: **it made up a classifier score** of 0.65.
- Task 8: an injected supervisor "override" in `operator_note` made the first-draft prompt refuse
  a 0.88-risk turbine instead of raising an urgent order. `src/llmguard.py` re-derives the
  decision from the score and logs every override. `make llm-attack` shows it firing.
- **8.86 THB per 1,000 requests**, about 30× the classifier.

**Cost.**
- Estimate 45 THB. **Actual 9.34 THB** from billing filtered by `lab=5`, all paid by credits.
- Most of the gap: I assumed about 300 Gemini calls and made 85, and planned 11 pipeline runs
  and needed 5.
- The pipeline's flat **run fee (5.04 THB) cost more than all the compute** it scheduled.
- Optimisation: staging moved from 2 vCPU always-on to 1 vCPU scale-to-zero. Over a week that
  is about 1,065 THB avoided, paid for with an 8–15 s first request after idle.

**Teardown.**
- Schedules went first, then staging and the three service accounts.
- Merging the lab made CD recreate staging. CD labels it `lab=4` (it dates from Lab 4), so
  `make teardown LAB=5` would have missed it. I deleted it with `LAB=4`.
- The console screenshots were taken in asia-southeast1, the region the lab actually ran in.
- The 17 SUCCEEDED job records are kept on purpose for lineage, and cost nothing.

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
  For Lab 2 resources use `make teardown LAB=2`. `make teardown LAB=4` also deletes the
  Cloud Scheduler job first, then the drift job and the alert policy.
- Lab 4 CI skips commits that only change `*.md` or `reports/`, so a write-up change
  never redeploys staging.
- The serving image does not include MLflow. The deployed service never uses it, and leaving
  it out keeps the image small, which shortens cold starts.
