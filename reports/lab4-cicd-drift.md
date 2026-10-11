# Lab 4 — CI/CD, monitoring, and drift

I automated the path from a commit to a deployed staging service, then watched the
running service and broke it on purpose to check that the watching works.

- **Pipeline:** GitHub Actions. `ci.yml` on every pull request and every push to main,
  `cd.yml` after a green CI on main.
- **Staging:** Cloud Run `itcs355-staging`, Singapore, 1 vCPU, scales to zero. Model
  `itcs355-6688143@1` (the Lab 2 model) from the Vertex registry.
- **Monitoring:** Cloud Monitoring dashboard and alert, both committed in `infra/gcp/`.
  Drift job on Cloud Run, started every 5 minutes by Cloud Scheduler.
- **Evidence:** everything below is in `reports/lab4/`.

---

## 1. Tests that can fail

Four kinds, each its own CI step, so the failing step's name says what kind of problem
it is.

| Kind | Where | What it protects against |
|---|---|---|
| Unit (14) | `tests/test_data.py -m "not contract"`, `tests/test_drift.py` | My code: the split, the PSI/KS maths, the stream generator, API bounds matching the contract |
| Data contract (7) | `tests/test_data.py -m contract` | The data itself: fails when a producer changes something, even if no code did |
| Model behaviour (7) | `tests/test_model_behaviour.py` | What the model does, whatever its AUC |
| Integration (4) | `tests/test_integration.py` against the built container | The image actually starts, loads the model, and answers with the right shape and version |

**Every test can fail for a real reason.** I fed the contract tests 8 kinds of broken
data, and each was caught by the test meant to catch it (dropped column, id as text,
nulls, Fahrenheit, bar for kPa, load as a 0–1 fraction, all-zero labels, a batch loaded
twice). The integration test fails if the container reports the wrong model version.

### The incident each data contract test would have caught

| Test | Production incident it catches |
|---|---|
| `test_schema_columns_present_and_typed` | A producer release drops `ambient_humidity`, or starts sending `machine_id` as `"M-017"`. The model would crash, or pandas would quietly make it an object column. |
| `test_no_nulls_in_required_columns` | A vibration sensor goes offline and the gateway sends empty values. The service would get NaN, and the forest would score it as if it were a real reading. |
| `test_features_within_plausible_ranges` | Gateway firmware switches to Fahrenheit (240 °F is above the 140 °C ceiling). **This is the one that blocked my bad commit.** |
| `test_feature_medians_in_expected_units` | A unit change that stays inside every range: pressure in bar (~3.2), load as a fraction (~0.63). The range test passes all of these. |
| `test_target_is_binary_and_not_degenerate` | The label join breaks and every row comes back "did not fail". The next retrain learns that nothing ever fails. |
| `test_identifier_is_unique` | A retried ingestion loads the same batch twice. Duplicated rows overweight those machines, and some land in both train and test. |
| `test_no_machine_leaks_across_splits` (Lab 1) | A producer reuses machine ids after a fleet change. The split would then put readings from the same physical machine on both sides, and the score would be too good to trust. |

### Behaviour tests and the latency budget

- **Healthy machine scores low.** A cool, new, lightly loaded machine must get a risk
  below 0.5.
- **Risk rises with wear and with temperature.** For temperature, the same readings
  10 °C hotter raise mean risk by more than 0.01. Across six seeds the rise was +0.030
  to +0.044.
- **Humidity does not drive risk.** Humidity plays no part in how failures happen here.
  Moving every row from 30% to 85% humidity must change mean risk by less than 0.03.
  Across six seeds it changed by at most 0.017.
- **Latency budget: 25 ms per one-row prediction.** It is derived from the 200 ms p95
  target in `loadtest/k6.js`. Lab 3 measured ~100 ms of that as network, and the queue
  at 10 users grows with how long scoring takes. One row takes ~4 ms locally. The test
  is a tripwire for a model that got several times slower, not a p95 forecast.
- **The fixture is the production configuration.** 200 trees, depth 8, predicting
  single-threaded, the same as `src/train.py`. The cheaper 120-tree model the scaffold
  used was not the one that serves.

---

## 2. CI pipeline

```
secret scan (full history) ─┐
lint → portability audit → unit → data contract → model behaviour → service ─┤
                                                                             └→ build both images (tag = commit SHA)
                                                                                → integration test against the container
                                                                                → push serving image        (main only)
                                                                                → cd.yml: deploy staging    (main only, green only)
```

Times from the run on main (`reports/lab4/cicd/runs.jsonl`):

| Step | Took |
|---|---:|
| secret scan | 7 s, in parallel |
| install (hash-checked) | 41 s |
| lint → service tests | 8 s for all six checks |
| build training + serving images | 2 min 08 s |
| integration test | 20 s |
| push image | 30 s |
| CD: find image, deploy, smoke test | 1 min 36 s |
| **commit to tested staging** | **6 min 41 s** |

- **Cheap checks run first.** The bad commit below failed 49 seconds after the push.
  Most of that was installing packages; the contract step itself took under a second.
  Nothing was built.
- **`pip install --require-hashes`.** The Lab 1 lock file already had hashes, so the
  TODO was just turning it on. A package whose bytes changed on PyPI now fails the
  install.
- **Images are tagged with the full commit SHA, never a floating tag.** CD looks the
  image up by that SHA and deploys it by digest
  (`itcs355-serve@sha256:847ed929…`). The image that reaches staging is the exact one
  CI tested. Nothing is rebuilt in between.
- **Docs-only commits don't redeploy.** Commits that only touch `*.md` or `reports/`
  do not trigger CI on main. A write-up change has nothing to deploy.

### Secrets: OIDC, no stored key

`infra/gcp/github-oidc.sh` sets it up once.
- **How it works:** GitHub signs a short-lived token for each workflow run. A Google
  workload identity pool exchanges it for the `gh-deployer` service account.
- **Who the pool trusts:** only tokens whose repository is `teemtat/itcs355` **and**
  whose ref is `refs/heads/main`. A pull request, even from a branch in this repo,
  cannot get cloud credentials at all.
- **What `gh-deployer` can do** (four roles plus one narrow binding):
  - push images (`artifactregistry.writer`)
  - deploy (`run.admin`, because `run.developer` cannot set a service's IAM)
  - call the private service for the smoke test (`run.invoker`)
  - read the model registry (`aiplatform.viewer`)
  - `serviceAccountUser` on the runtime account only
- **What GitHub stores:** two identifiers (`GCP_WIF_PROVIDER`, `GCP_DEPLOY_SA`). Neither
  is a credential. Nothing in the repository or the secret store could be leaked and
  reused.
- **The smoke-test token:** the service is private, so CD needs an identity token for
  its URL. `gcloud auth print-identity-token` cannot mint one for a federated identity,
  so CD gets it from a second auth step and passes it in `SERVICE_ID_TOKEN`.

---

## 3. The bad commit, blocked

[PR #2](https://github.com/teemtat/itcs355/pull/2) changes `scripts/make_dataset.py` so
temperature arrives in **Fahrenheit**. The column is still called `temp_c`, the values
are still numbers, and there are no nulls. It looks like what a gateway firmware update
would do.

- **Run:** https://github.com/teemtat/itcs355/actions/runs/38023737410 (`failure`)
- **Failed at step:** `Data contract tests`. `Unit tests`, lint and the secret scan
  all passed. `Model behaviour`, `Service tests` and the whole `build` job were
  skipped, so no image was built, pushed or deployed.
- **The tests that caught it:**

```
FAILED tests/test_data.py::test_features_within_plausible_ranges
  AssertionError: temp_c above plausible ceiling: 240.215
FAILED tests/test_data.py::test_feature_medians_in_expected_units
  AssertionError: temp_c median 174.055 outside [60.0, 100.0] — did the producer change units?
2 failed, 5 passed, 6 deselected in 0.41s
```

- Closed without merging at 04:23:54 UTC.
- **Files:** full failing log in `reports/lab4/blocked-commit/failed-step.log`, every
  step's result in `run.json`, and GitHub's failure email in `github-email.webp`.

Unit tests passed on the same commit. The code was fine; only the data was wrong. That
is the difference between the two kinds.

---

## 4. CD to staging

The merge to main (`102d3e7`) ran CI, then CD (`reports/lab4/cicd/cd-deploy-and-smoke.txt`):

```
04:25:49  deploying …/itcs355-serve@sha256:847ed929… (commit 102d3e75…)
04:26:40  itcs355-staging-v1-1cpu-042553
04:26:45  PASS  single        200  version=1  350 ms
04:26:45  PASS  batch         200  version=1  388 ms
04:26:45  PASS  out-of-range  422  version=1  377 ms
```

It worked on the first run. The common failure ("CI identity can build but not deploy")
didn't happen, because the deployer's roles were chosen from the commands `deploy()`
actually runs.

---

## 5. Dashboard

Committed as code in `infra/gcp/dashboard.json`, created by `infra/gcp/lab4-monitoring.sh`.
`monitoring/dashboard.json` is the provider-neutral version, mapping each panel to the
metric it uses.

| Panel | Metric |
|---|---|
| Request rate | `run.googleapis.com/request_count`, per second |
| Error rate, 4xx and 5xx separately | same metric by `response_code_class`, each divided by all requests |
| Latency p50 / p95 / p99 | `run.googleapis.com/request_latencies`, with the 100 ms endpoint SLO line |
| **Feature drift: PSI, rolling 30-min window** | `custom.googleapis.com/itcs355/drift/psi`, one line per feature, alert line at 0.05 |
| temp_c mean of the same window | `…/drift/mean` |
| Drift detector rows per window | `…/drift/window_rows`. If this goes to 0, the detector itself is down |
| **Model version in production** | log-based metric `itcs355_requests_by_model_version`, counted from the version in each request's log line |

During the exercise (`reports/lab4/drift-exercise/cloud-monitoring.json`):
- **Traffic:** 4,144 requests. 2xx 4,098, **4xx 46, 5xx 0**. The 4xx were deliberate:
  1% of replayed rows were out of range, so the 4xx line has something real on it.
- **Endpoint latency:** p50 14 ms, p95 16–22 ms. The first 10 minutes reached 50 ms
  (cold start from zero).
- **Model version:** all 4,198 logged `/predict` requests answered by version 1.

Screenshot: `reports/lab4/drift-exercise/dashboard.webp`, showing yesterday in Thai time.
- PSI for temp_c leaves the floor after 12:10 and climbs past the 0.05 line. The other
  five features stay flat.
- The window mean rises from ~80 to ~86, rows per window reach ~1,780, the 4xx line
  sits near 1%, and latency stays far under the 100 ms line.

---

## 6. SLO

In `monitoring/slo.yaml`:

| | Target | Window | When the budget is spent |
|---|---|---|---|
| Availability | 99.5% non-4xx success | 30 days | Freeze model and feature deploys; only rollbacks and fixes ship until it recovers. Page the owner at 50% burned in a day, not at 0. |
| Latency | p95 < 200 ms at the client (k6), 100 ms at the endpoint | 7 days | Move to 2 vCPU (Lab 3 showed it meets the target), then find what got slower. If that doesn't fix it, reopen the target with the dashboard owners. |
| Freshness | model ≤ 30 days old | continuous | Retrain only if the new data passes the contracts and the drift check. Otherwise keep the old model and record the breach. Lab 5's retrain runs every 28 days so a failed run has time to retry. |

- **Why 99.5% and not 99.9%:** one instance in one region. One bad deploy plus its
  rollback would use most of a 43-minute monthly budget.
- **Freshness and retraining:** the third answer is the same lesson as the post-mortem.
  A fresh model trained on broken data is worse than a stale one.

---

## 7. Drift detection

### How it works

- **What runs:** Cloud Scheduler (`*/5 * * * *`) starts the Cloud Run job
  `itcs355-lab4-drift`. It is the **same image** as the service, running
  `python -m monitoring.drift --endpoint itcs355-staging --emit`.
- **Where the inputs come from:** every request's log line now carries the rows the
  model scored (`service/app.py`). The job reads the last 30 minutes of those back from
  Cloud Logging (`adapter.recent_inputs`). A 422 never reached the model, so it never
  enters the window.
- **What it computes:** PSI and KS for each of the six features against the training
  data (`data/raw/sensors.csv` in the bucket). It writes them as metrics through
  `adapter.emit_metric`.
- **How the alert fires:** a Cloud Monitoring policy fires when any feature's PSI is
  above 0.05 and emails teemtat.wee@gmail.com.
- **Short windows are skipped:** below 900 rows the job skips instead of scoring. A
  half-empty window after a restart is too noisy to judge (see below).

### Why 0.05 (and not 0.10 / 0.25)

The usual 0.10 / 0.25 cut-offs come from credit scoring, which has stable features and
millions of rows. I measured my own case with `scripts/drift_calibrate.py`
(`reports/lab4/calibration.json`). "Production" there means new readings from the same
240 machines, compared with the training data.

**Noise, when nothing changed** (500 windows, worst of 6 features per window):

| Rows in window | Typical | 99th percentile | Worst seen |
|---:|---:|---:|---:|
| 300 | 0.047 | 0.078 | 0.108 |
| 900 | 0.018 | 0.036 | 0.038 |
| **1,800 (30 min)** | **0.010** | **0.019** | **0.023** |
| 3,600 | 0.006 | 0.011 | 0.013 |

**Effect: how big a temperature offset matters,** and what it does to the model:

| temp_c offset | PSI (1,800 rows) | Readings flagged (risk > 0.3) |
|---:|---:|---:|
| 0 | 0.004 | 10.1% |
| +2 °C | 0.052 | 10.7% |
| +3 °C | 0.102 | 11.1% |
| +4 °C | 0.179 | 11.5% |
| +6 °C | 0.387 | 12.2% |

**So 0.05:**
- It is **twice the worst noise** I saw at this window size, so normal variation
  should never page anyone.
- It matches an offset of about **+2 °C**. That is roughly the standard tolerance of an
  ordinary industrial thermocouple, so anything smaller I couldn't tell from a normal
  sensor anyway.
- At 0.25, a **+4 °C fault would never alert** (PSI 0.18), while the model flagged 14%
  more readings than it should.

The live run backed this up: 40 minutes of normal traffic, 7 scored checks, the highest
PSI on any feature 0.014. No false alarm.

**Why a 30-minute window:**
- 15 minutes (900 rows) would detect sooner, but its noise reaches 0.038, too close
  to 0.05.
- 60 minutes would be quieter, but would take twice as long to fill with drifted data.

### Which statistic catches which fault

Each mode applied to new readings from the same fleet (`make drift-modes`, outputs in
`reports/lab4/modes/`):

| Mode | temp_c mean | PSI | KS | Alert at 0.05? |
|---|---:|---:|---:|---|
| none | 79.56 | 0.001 | 0.010 | no (correct) |
| shift +6 °C | 85.56 | **0.378** | 0.251 | yes |
| scale ×1.5 | **79.56, unchanged** | **0.197** | 0.112 | yes |
| mix (20% of machines ×5) | 80.11 | 0.011 | 0.037 | **no** |

- **Scale is why I alert on PSI, not on the mean.** The mean doesn't move at all, yet
  PSI is 0.20. KS reacts much less (0.11): it looks for the single biggest gap between
  the two distributions, and spreading values out on both sides keeps that gap small.
- **Shift** is caught by everything, the mean included.
- **Mix is not caught, and that is a real gap.** Over-sampling a fifth of the machines
  nudges every feature a little (temp_c 0.011, vibration 0.010), and none comes near
  0.05.
  - A per-feature threshold cannot see it without becoming noisy.
  - What would see it is a check on *which machines* are reporting, e.g. readings per
    machine compared with the usual mix.
  - It matters because the realistic version of this, a fleet change, alters the risk
    mix without any single sensor looking odd.

---

## 8. The injected drift

**Prediction, committed before injecting** (commit `a861c9a`, 04:34 UTC):
- PSI crosses 0.05 once ~35% of the window is drifted, about 10.5 minutes after the
  injection.
- The next 5-minute check comes 0–5 minutes after that, then 1–3 minutes for
  Cloud Monitoring to alert.
- So **the alert should arrive 12–19 minutes after injection.**

**What I ran:** `scripts/replay_traffic.py`, 1 request/second to staging, rows in random
order from all 240 machines. 40 minutes of normal readings, then the same stream with
temp_c **+6 °C**, for 30 minutes.

| Time (UTC) | What happened | Where it's recorded |
|---|---|---|
| 04:30:59 | replay starts, normal readings | `replay-events.jsonl` |
| 04:35 – 04:45 | checks skip: 248, 546, 844 rows (< 900) | `drift-skips.txt` |
| 04:50 – 05:10 | 5 checks, all quiet, highest PSI 0.014 | `drift-checks.jsonl` |
| **05:10:59** | **injection: temp_c +6 °C from here on** | `replay-events.jsonl` |
| 05:15:16 | temp_c PSI 0.004 (14% of window drifted) | `drift-checks.jsonl` |
| 05:20:15 | temp_c PSI 0.029 (31%) | `drift-checks.jsonl` |
| **05:25:15** | **temp_c PSI 0.0655 (48%): detector over the line**; other features ≤ 0.011 | `drift-checks.jsonl`, metric |
| **05:28** | **Cloud Monitoring incident opens, email arrives** (12:28 Thai time) | `alert-firing-email.png` |
| 05:30:15 | 0.126 | |
| 05:35:15 | 0.226 | |
| 05:40:19 | 0.387 (window fully drifted, same as the offline +6 °C figure of 0.387) | |
| 05:40:59 | replay ends: 2,400 normal + 1,800 drifted requests sent | `replay-events.jsonl` |

**Detection time:**
- **14 min 16 s** from injection to the detector's own check
- **~17 minutes** from injection to the email in my inbox

Both are inside the 12–19 minute prediction.

Where the 17 minutes went:
- ~14 minutes for drifted readings to make up half the window. It crossed at 48%, a
  little later than the offline 35–40%, which is within the run-to-run noise of one
  window.
- ~3 minutes for Cloud Monitoring to evaluate the policy and send the email.

Post-mortem: [`reports/lab4-postmortem.md`](lab4-postmortem.md).

### What the "recovered" email taught me

At 06:30 UTC a second email said **"Alert recovered … custom/itcs355/drift/psi is
missing data"** (`reports/lab4/drift-exercise/alert-recovered-email.webp`). Nothing had
been fixed.
- **Why it closed:** the replay had stopped at 05:41. As the window emptied, the job
  went back to skipping (fewer than 900 rows) and wrote no PSI. Cloud Monitoring treats
  no data as no problem, so after an hour the incident closed itself.
- **Why that matters:** in production the same thing would happen if the sensor
  pipeline died, or if the drift job started crashing. The drift alert would go quiet
  exactly when nothing is being checked.
- **What I added:** a second policy, `infra/gcp/detector-blind-policy.json`. It pages
  when no temp_c PSI point has been written for 20 minutes.
  - I first tried to keep the drift incident open on missing data. Cloud Monitoring
    only allows that on conditions with a non-zero duration, and mine fires on a single
    point.
  - I checked the new policy against the API by creating and deleting it. It was added
    after the run, so it has not fired for real yet.

**Client latency from my laptop was slower today.** p95 was 226–248 ms at one request
per second, above Lab 3's 112–131 ms. The endpoint itself stayed at p95 16–22 ms, so
the extra time was on the network between my laptop and Singapore, not the service. The
SLO alert watches the endpoint for exactly this reason. The weekly k6 run is where the
client number would be judged.

---

## 9. Cost

**What I estimated:** about 7 THB. I worked it out from the instance time Cloud Run
itself reports (`run.googleapis.com/container/billable_instance_time`, 9–11 Oct), using
Lab 3's prices.
- **Staging:** 1.48 h on 1 vCPU / 2 GB, ≈ 4.7 THB. It scales to zero, so it costs
  nothing once traffic stops.
- **Drift job:** 0.91 h on 1 vCPU / 1 GB, ≈ 2.5 THB.
  - It ran every 5 minutes from 04:30 UTC on 10 Oct until teardown at 07:30 UTC on
    11 Oct, ~324 runs.
  - Only ~14 of those runs were the exercise. The rest happened because I left the
    schedule running for a day while waiting for screenshots, the "schedule that
    survives" problem the handout warns about.
  - It stayed cheap only because the job skips quickly when there is no traffic.
- **The `scamshield` service in the same project:** 186 s, negligible.
- **Scheduler, custom metrics, logs, alerting:** inside the free tiers at this volume.

**What the billing page says:** **Cloud Run ฿17.16** for 9–11 Oct, all covered by
credits, so **฿0.00 paid** (`reports/lab4/billing-9-11oct.webp`).
- Billing days are in US Pacific time, so the run on 10 Oct (UTC) appears under 9 Oct.
- Every other service shows ฿0.00.
- Over the last 90 days, all labs together come to ฿38.88 of usage and ฿0 paid
  (`reports/lab4/billing-90days.webp`).

**The actual bill is about 2.4× my estimate, and I can't fully explain why from the
instance time.** My guesses:
- Cloud Run jobs are priced differently from services.
- Each job run is billed for start-up time that the metric doesn't count.

The billing page is the number that counts. For Lab 5 the lesson is concrete: my
estimate counted only time spent serving, and missed the overhead around it.
The 10–11 Oct figures may still rise as late billing data arrives.

---

## 10. Teardown

`make teardown LAB=4` at 07:30 UTC on 11 Oct (`reports/lab4/teardown.txt`):

```
deleted   Cloud Scheduler job itcs355-lab4-drift
deleted   Cloud Run job itcs355-lab4-drift
deleted   alert policy 'itcs355 input drift: PSI above 0.05'
deleted   Cloud Run service itcs355-staging
PASS  nothing found under these tags
```

- **The schedule is deleted first.** A scheduler left behind keeps starting a job that
  reads logs from a service that no longer exists.
- **Checked by hand afterwards:** 0 scheduler jobs, 0 Cloud Run jobs, no Lab 4 service.
  (`scamshield` is my capstone, not this lab.)
- **Kept on purpose, all free with no traffic:**
  - the dashboard and the email channel
  - the log-based metric and the custom metric descriptors
  - the `gh-deployer` identity, which Lab 5's pipeline needs
  - the images in Artifact Registry
- **Staging can come back:** the next push to main that changes code will create a new
  staging service through CD. It scales to zero, so it costs nothing until it is used.

**I still need to check the billing page by hand.**
