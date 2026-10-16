# Lab 5 — Managed pipeline and scheduled retraining (Tasks 1 and 3)

Everything below ran on 2026-10-16 (UTC) in project `itcs355-6688143`, region
asia-southeast1, on **Vertex AI Pipelines**. The cost estimate was committed first
(`d16efa2`, `reports/lab5-cost.md` §1), before any pipeline code.

**Summary**
- One triggered execution runs `ingest → validate → train (spot) → evaluate → [gate] →
  register → deploy to staging`.
- **Passing run:** gate PASS (+0.0053 over the production model). Registered as
  **version 3** and deployed to Cloud Run `itcs355-staging`, where a smoke test got HTTP 200
  from version 3.
- **Gate-blocked run:** a degraded config lost by −0.0062. Register and deploy_staging were
  **never created**: no new registry version, no new revision.
- **Validate abort:** a kPa→bar units change failed the data contract. Train and everything
  after it never ran.
- **Scheduled run:** fired at 04:25:02 UTC as the Compute Engine default service account.
  It retrained on unchanged data, the gate rejected it (delta +0.0000), and nothing was
  registered.

---

## 1. How it is built

| Piece | File | What it does |
|---|---|---|
| The DAG | `pipeline/pipeline.yaml` | Steps, order, run parameters, the condition, the schedule. No provider names. |
| Step logic | `pipeline/steps.py` | The six steps as one CLI (`python -m pipeline.steps <step>`), inside the image. Provider-neutral: steps hand data to each other through `BLOB_URI/lab5-pipeline/<run-id>/`, which Vertex mounts as a directory (`adapter.mount_path`). |
| Step image | `pipeline/Dockerfile` | The Lab 1 training image (same base digest, same hash-locked requirements), plus `tests/` and `service/` for validate, plus gcloud 583.0.0 pinned by sha256 for register and deploy. Pushed as `itcs355-pipeline@sha256:e6bcdd12…` (`reports/lab5/pipeline/pipeline-image.txt`). |
| Translation | `cloudlayer/pipelines.py` | Layer 3. Each YAML step becomes a KFP container component from its image and command. `condition:` becomes a `dsl.If`. `spot: true` wraps the step in Google's custom-job component with `strategy=SPOT`. Submits, waits and schedules with the Vertex SDK. Also holds the registry helpers the steps call. |
| The gate | `scripts/evaluation_gate.py` | Decides; see §2. |

**Run it.** You need `pipeline/requirements-compile.txt` (kfp 2.17.0, google-cloud-pipeline-components
2.22.0, google-cloud-aiplatform 1.165.1) on the machine that compiles, but not in the image:

```bash
make pipeline                                                    # defaults: pass path
python -m cloudlayer.pipelines run --param max_depth=2 --param n_estimators=10   # gate fails
python -m cloudlayer.pipelines run --param corrupt=pressure_bar  # validate aborts
python -m cloudlayer.pipelines schedules                         # what is scheduled
```

**Step logic is in the image.** Every command in the YAML is
`python -m pipeline.steps <step> --run-id … [args]`. Retargeting to SageMaker or Azure ML
changes only `cloudlayer/pipelines.py`. The YAML, the image and the steps stay as they are.

**Lineage per run.** Each run's directory holds its data, `ingest.json`, `validate.json`,
`metrics.json`, `model/model.joblib`, `lineage.json` and `gate.json`. A run that registers
also holds `registered.json` and `deployed.json`. The registered version's labels are
`course`, `student`, `lab=5`, `pipeline_run`, `git_commit`, `data_fingerprint` and
`gate=pass`. Its version description carries the full lineage, with the image digest and the
gate's numbers.

---

## 2. The gate

**What it compares.** The **production model** is the registry version with the `default`
alias. That's what Vertex itself uses when no version is named, and the pipeline moves the
alias only when the gate passes. The gate downloads that version and **re-scores it on the
candidate's own test split**. Both models are judged on the same rows, so the difference is
between the models and not between two test sets. (The incumbent's registered 0.8533 was
measured on week-1 data. On the 3-week split it scores 0.8584.)

**The rule.** Register only if `candidate ≥ 0.80` and `candidate − incumbent ≥ 0.004`,
measured as test ROC AUC.

**Why 0.004 (measured on the 3-week data, split fixed):**

| Noise source | SD of test ROC AUC |
|---|---|
| Same config, 5 different `random_state` | 0.0003 – 0.0008 |
| Bootstrap of the test set (300 resamples), **paired** difference candidate − incumbent | **0.0017** |

- 0.004 ≈ 2.3 × the larger SD, about a 1% one-sided chance of promoting a model that is
  not really better.
- It is still small enough to clear for real improvements: two more weeks of data gave
  +0.0053.
- A margin of 0 would promote noise. A margin of 0.01 would have blocked every improvement
  this system has produced.

**Why 0.80 as the floor.** The worst Lab 2 trial scored 0.8245 val ROC AUC. Below 0.80 is a
broken run, not a weaker model, and it must not register even when there is no incumbent.

**Why it can't fail open:**
- Exit codes are 0 = register, 1 = don't, 2 = can't decide. A Python crash also exits 1, so
  the step counts a decision only when `gate.json` agrees with the exit code. Otherwise the
  step fails.
- The register step re-reads `gate.json` and refuses anything but `"pass"`. Deleting the
  YAML condition still couldn't register a rejected model.
- Grader check, against the passing run's metrics:

```
$ python scripts/evaluation_gate.py --metrics …/20261016105130/metrics.json --incumbent 0.90
candidate test_roc_auc = 0.8637
incumbent 0.9000  delta -0.0363  required +0.0040
GATE FAIL  delta -0.0363 is below the +0.004 margin (worse than the incumbent); not registering
exit 1
```

---

## 3. Evidence: the runs

Console: `https://console.cloud.google.com/vertex-ai/locations/asia-southeast1/pipelines/runs/<run>?project=52907215794`.
Each run's record, saved by `wait_gcp`, is in `reports/lab5/pipeline/runs/<run>.json`. The
per-run files copied from the bucket are in `reports/lab5/pipeline/artifacts/<run>/`.

| Run | What | Params | Result | UTC | Identity |
|---|---|---|---|---|---|
| `itcs355-training-pipeline-20261016101857` | first attempt (debugging) | defaults | **FAILED at train**. A bug, see below | 03:19–03:48 | compute default SA |
| `itcs355-training-pipeline-20261016105130` | **pass** | defaults (`weeks=3`, 100 trees, depth 4) | **SUCCEEDED**: registered v3, deployed staging | 03:51–04:18 | compute default SA |
| `itcs355-training-pipeline-20261016105148` | **validate abort** | `corrupt=pressure_bar` | **FAILED at validate**; train → deploy NOT_TRIGGERED | 03:51–03:58 | compute default SA |
| `itcs355-training-pipeline-20261016111927` | **gate blocks** | `max_depth=2, n_estimators=10` | SUCCEEDED; branch NOT_TRIGGERED | 04:19–04:29 | compute default SA |
| `itcs355-training-pipeline-20261015212502108` | **scheduled** | defaults | SUCCEEDED; branch NOT_TRIGGERED | 04:25–04:32 | compute default SA |

The scheduled run's name carries a US-Pacific timestamp (21:25 on the 15th). That's how
Vertex names scheduled runs. It started at 04:25:02 UTC on the 16th.

### 3.1 Passing run: registers and deploys

```
ingest          SUCCEEDED   03:51:35–03:55:19   18000 rows (3 weeks), no corruption
validate        SUCCEEDED   03:55:19–03:59:14   7 contract tests, 0 failed, 0 skipped
train           SUCCEEDED   03:59:14–04:04:43   n1-standard-4 SPOT
evaluate        SUCCEEDED   04:04:43–04:09:04   gate_decision = pass
condition-1     SUCCEEDED
  register      SUCCEEDED   04:09:05–04:14:01
  deploy-staging SUCCEEDED  04:14:01–04:18:06
```

`gate.json`: candidate **0.8637**, incumbent `itcs355-6688143@1` re-scored on this split
**0.8584**, delta **+0.0053**, the required margin +0.004 → **pass**.

Registry afterwards (`gcloud ai models list-version 8639412615249920000`):

```
VERSION_ID  VERSION_ALIASES  LAB  VERSION_CREATE_TIME
1                            2    2026-09-20T08:48:46Z
2                            2    2026-09-26T05:12:49Z
3           ['default']      5    2026-10-16T04:12:55Z      <- pipeline_run=itcs355-training-pipeline-20261016105130
```

Staging: revision `itcs355-staging-v3-1cpu-041639`, 100% traffic, min instances 0, labels
`course=itcs355, lab=5`. The deploy step's own smoke test got status 200 with
`model_version: "3"`, 235 ms. The same check from my laptop
(`reports/lab5/pipeline/staging-smoke.txt`):

```
PASS  single        200  version=3  283 ms
PASS  batch         200  version=3  146 ms
PASS  out-of-range  422  version=3  133 ms
```

### 3.2 Gate fails: register and deploy are skipped

Same data, deliberately weaker model: 10 trees of depth 2.

```
ingest / validate / train / evaluate   SUCCEEDED
condition-1                            NOT_TRIGGERED      (register, deploy-staging never created)
```

`gate.json`: candidate **0.8575**, incumbent `itcs355-6688143@3` re-scored **0.8637**, delta
**−0.0062** → **fail**. Three things show nothing was registered or deployed:
- the run directory has no `registered.json` or `deployed.json`;
- the registry still lists v3 as `default` and has no v4;
- `itcs355-staging` still has the single revision `itcs355-staging-v3-1cpu-041639`.

(The reason text in this run's `gate.json` says "not distinguishable from noise" even for a
negative delta. I changed the message afterwards to say "worse than the incumbent" when the
delta is below zero. The decision logic is unchanged.)

What I didn't expect: the degraded forest is only 0.006 behind. The failure probability is
close to a logistic function of the features, so even a tiny forest gets most of the way.
The margin is what stops it, not the floor.

### 3.3 Validate aborts on a contract-failing dataset

`corrupt=pressure_bar` divides pressure_kpa by 100, as if the producer had switched to bar.
From the validate job's log (`reports/lab5/pipeline/validate-abort-log.txt`):

```
FAILED tests/test_data.py::test_feature_medians_in_expected_units - Assertion...
E   AssertionError: pressure_kpa median 3.181 outside [250.0, 400.0] — did the producer change units?
1 failed, 6 passed, 6 deselected
VALIDATE FAIL  {'tests': 7, 'failures': 1, 'errors': 0, 'skipped': 0}: aborting before train.
```

Pipeline: ingest SUCCEEDED, validate FAILED, and train, evaluate and condition-1 all
NOT_TRIGGERED. The run's directory holds only `ingest.json`, `validate.json` and the data:
no model, no metrics.

Only **one** of the seven contracts caught this. The range check passed, because 3.18 is
inside the plausible pressure range. The Lab 4 median test is the only thing standing
between a units change and a trained model. (`corrupt=temp_nulls` was also checked locally:
the null test catches it.)

The step also turns a **skipped** contract test into a failure. `tests/test_data.py` skips
when the data file is missing, and pytest exits 0 on a skip. Without that check, "no data"
would have passed validate.

### 3.4 The first run failed, and why that matters

Run `…101857` failed at train. The run-id placeholder `{{$.pipeline_job_name}}` is
substituted inside plain container steps but **not** inside Google's custom-job wrapper,
the only way to get spot. Train received the literal text, looked for its data in a
directory with that name, and found none. The fix: ingest now emits the resolved run id as
an output, and every later step takes it from there. This is a real seam leak. A "neutral"
placeholder behaves differently depending on which provider component a step is compiled
into. Nothing in the YAML can see that.

The same run also showed the spot cost: the train job waited **12 min 15 s** for spot
capacity before starting (created 03:28:29, started 03:40:44). The wait is now capped at
1 h (`max_wait_duration`; the default is 24 h, during which the run looks alive).

---

## 4. Task 3 — the schedule

**The cron is `TZ=Asia/Bangkok 0 2 * * 1`: Mondays 02:00 Bangkok, Sunday 19:00 UTC.** The
scaffold had `0 2 * * 0` (Sunday 02:00 UTC). I changed it, with the reasons in
`pipeline/pipeline.yaml`:

- **Labels mature weekly.** The label is `failed_within_7d`, so a reading is only labelled
  seven days later. Weekly is the shortest interval that gives a whole new week of labels.
- **Someone is there to look.** 02:00 Monday runs after the weekend's readings are in and
  before the 07:00 maintenance shift. A failed run then has a working day of attention.
  Sunday 02:00 UTC is 09:00 Sunday in Bangkok, when nobody is looking.

Vertex accepts an IANA timezone in the cron, but rejected `TZ=UTC …` as an
"Invalid cron schedule".

**Schedules created** (`python -m cloudlayer.pipelines schedules`):

| Schedule | Cron | State | Purpose |
|---|---|---|---|
| `itcs355-lab5-retrain` (`schedules/3067518694239240192`) | `TZ=Asia/Bangkok 0 2 * * 1` | **ACTIVE**, next run 2026-10-18T19:00Z | the real schedule |
| `itcs355-lab5-fire-once` (`schedules/6067478996021411840`) | `25 4 * * *`, max_run_count 1 | **COMPLETED** after 1 run | to watch one scheduled execution today rather than wait for Monday |

**It fired.** The schedule reports `startedRunCount: 1`, `scheduledRunTime
2026-10-16T04:25:00Z, runResponse OK`. The run it created,
`itcs355-training-pipeline-20261015212502108`, links back to the schedule through
`scheduleName` and SUCCEEDED in 7 min 46 s.

**Identity.** The schedule was created with no service account, so the run executed as
**`52907215794-compute@developer.gserviceaccount.com`, the Compute Engine default service
account**. That is recorded in the run's own `serviceAccount` field, not inferred. My
interactive runs used the same identity, because I submitted those without one too. So the
handout's "fails only when scheduled" trap didn't apply here, but only because both paths
use an over-broad identity: that account has `roles/editor`. With the Task 2 scoped training
SA (`itcs355-lab5-train`, from the IAM session), the schedule would run as that SA, and
**deploy_staging would fail**, since that SA has no Cloud Run role. Which identity
deploys staging is the decision Task 2 has to make. Until then, a scheduled run that passes
the gate will break at deploy, not at train.

**What the scheduled run did.** It retrained on the same three weeks with the same config.
The forest is deterministic given the seed, so the candidate equalled v3 exactly:
`delta +0.0000` → **fail**, nothing registered. That's the right outcome. With no new data,
a weekly run costs a few baht and changes nothing.

### 4.1 What should trigger a retrain, for this system

The system: 240 turbines, six sensor readings each, and a forest that predicts
`failed_within_7d`. Three facts about it decide the answer:

- **Labels arrive 7 days late.** Whatever triggers a retrain, the newest week of data has
  no labels yet.
- **The fleet is fixed and slow-changing.** The same 240 machines, ageing by hours since
  service. Real change shows up as wear, over weeks, not hours.
- **Its upstream has already broken once.** Lab 4's fault was a sensor gateway adding 6 °C
  to every temp_c at the same second. Schema, nulls and ranges all passed; only the drift
  check (PSI 0.0655 > 0.05) saw it.

| Strategy | When it is right here | How it fails here |
|---|---|---|
| **Fixed schedule** (weekly, Mon 02:00 Bangkok) | The normal case. Wear accumulates slowly, and one week is exactly one new batch of matured labels. A run costs ~2–3 THB and the gate rejects a retrain that isn't better. The scheduled run above is the proof: unchanged data, delta 0, nothing promoted. | Too slow for a real step change. After a fleet-wide maintenance programme or a new turbine model, the model is stale for up to 7 days, plus the 7-day label lag. And it is blind: at the scheduled hour it retrains on whatever arrived, good or bad. |
| **Data volume threshold** (retrain at ≥ 6,000 newly *labelled* readings, one fleet-week) | When the reading rate is uneven, e.g. machines offline for maintenance or a growing fleet. Volume then tracks "enough new evidence to clear the margin": two more weeks moved test AUC by +0.0053. | Volume says nothing about content. A gateway that duplicates or replays readings hits the threshold early with no new information. At this fleet's steady rate it degenerates into a fixed schedule with extra machinery. Counting *unlabelled* readings would retrain on rows that have no target. |
| **Drift-triggered** (Lab 4's PSI > 0.05 on any feature) | When the inputs really have moved and the model is the stale part, e.g. a genuine rise in operating temperature that an independent thermometer confirms. | It can't tell "the machines changed" from "the sensor broke", and for this system the second is more likely. It also fires on inputs whose labels haven't arrived: a drift alert today can only retrain on data at least 7 days old, which is mostly pre-drift. So it retrains on the wrong data, or later on the corrupted data. |

**What I chose:** the fixed weekly schedule, behind the gate. Drift **alerts a person** and
does not retrain. A drift alert means "check upstream first" (the Lab 4 post-mortem). Once
someone has confirmed the shift is real, they run the same pipeline by hand
(`python -m cloudlayer.pipelines run`), through the same gate.

### 4.2 The worst case

**Drift-triggered retraining on corrupted upstream data destroys the working model faster
than any schedule would.** Played out on this system with Lab 4's fault:

1. **05:10 Monday.** The gateway starts adding 6 °C to every temp_c.
2. **~05:28.** PSI crosses 0.05. Under drift-triggering, the alert starts a retrain.
3. **The first run is harmless, but the trigger loops.** The newest *labelled* week is
   still pre-fault, so the run gains nothing and the gate rejects it. But drift stays above
   threshold for as long as the fault lasts, so with Lab 4's 5-minute drift check the
   trigger fires every 5 minutes.
   - At ~2.5 THB a run that is ~720 THB a day, the whole 800 THB term budget in about a day.
   - A trigger on a *condition* rather than an *event* needs a cooldown, or it becomes a
     loop.
4. **Seven days in, the gate passes the wrong model.** The corrupted readings now carry
   labels, so the candidate is trained *and tested* on +6 °C data. The gate re-scores the
   incumbent on that same corrupted split.
   - The incumbent learned real temperatures and looks worse on it. The candidate learned
     the fault and looks better.
   - **The gate passes.** It defends against noise, not against a test set both models
     share: they are judged on the same lie.
5. **The damage arrives when the sensor is fixed.** The corrupted model is registered as
   `default` and deployed. Once the gateway is repaired, it under-scores every machine by
   6 °C: missed failures, the expensive mistake here. The Lab 4 post-mortem put a needless
   inspection at 1,500 THB; a missed turbine failure costs far more.

The weekly schedule fails the same way at step 4, but once a week instead of every 5
minutes, and with a week in which the drift alert has already reached a person. The
schedule isn't safe on its own; what it buys is time.

**What would actually stop step 4:**

- **A relationship test in validate.** Validate is the only step that can say "this data is
  wrong", not just "new". It caught a units change and a dropped feed in this lab, but a
  +6 °C offset passes every current contract (85 °C is a plausible temperature). The missing
  test is on the *relationship* between features (temp_c against hours_since_service and
  vibration): a sensor offset breaks it, real heating does not.
- **Quarantine by time window.** When a drift alert is confirmed as an upstream fault, the
  affected window is marked bad and ingest excludes it. That's the post-mortem's "mark the
  readings from 05:10:59 as bad", and it has to be a field in the data, not a note.
- **A frozen holdout.** If the gate also scored both models on a fixed, verified test set
  from before any incident, the corrupted candidate in step 4 would lose there. My gate
  scores only the current split; this is the next change I'd make to it.

---

## 5. Resources created, and how long they ran

All labelled `course=itcs355, student=itcs355-6688143, lab=5`, unless noted. Times are UTC
on 2026-10-16.

**Pipeline compute** (21 Vertex custom jobs, `labels.lab=5`, one per step attempt, from
`customJobs.list`):

| Machine | Jobs | Running (start→end) | Created→end (incl. provisioning and spot queue) | THB ceiling |
|---|---|---|---|---|
| e2-highmem-2, on demand: every step except train (Vertex picked it for 2 vCPU / 8 GB) | 15 | 0.109 h | 0.717 h | 4.55 *(at the e2-standard-4 rate of 6.34 THB/h; e2-highmem-2 isn't in `src/costs.py` and is cheaper)* |
| n1-standard-4, **spot**: train | 6 (3 of them run 1's retries) | 0.059 h | 0.549 h | 1.68 *(3.06 THB/h)* |
| Pipeline run fee | 5 runs | — | — | 5.00 *($0.03 each)* |

- Steps actually ran for seconds: a fit takes about 1 s, and the job's run time includes
  container start.
- What dominated the wall clock was provisioning, 3–5 min per step on the first runs and
  under 1 min once warm. Billing will show which of the two Vertex charges for. I've left
  the ceiling at created→end.
- Wall clock per run: 26.5 min (first pass), 10 min (gate-fail), 7.8 min (scheduled).

**Still alive** (left for the coordinator's teardown):

| Resource | Created | Notes |
|---|---|---|
| Vertex schedule `itcs355-lab5-retrain` (`projects/52907215794/locations/asia-southeast1/schedules/3067518694239240192`) | 04:21 | **ACTIVE. Next fire 2026-10-18 19:00 UTC**, about 2.5 THB a run. **`make teardown` won't find it**: the adapter's teardown deletes Cloud Scheduler jobs, and Vertex pipeline schedules are a different service. Delete with `python -m cloudlayer.pipelines teardown`, which removes every `itcs355-lab5-*` schedule. Vertex schedules carry no labels; the name prefix is how teardown finds them. |
| Vertex schedule `itcs355-lab5-fire-once` (`…/schedules/6067478996021411840`) | 04:20 | COMPLETED after its one run. It will never fire again, but `teardown` above removes it too. |
| Cloud Run service `itcs355-staging`, revision `itcs355-staging-v3-1cpu-041639`, 1 vCPU / 2 GiB, min 0 / max 1, `lab=5` | 04:14 | Scales to zero. Billed only while an instance was up: deploy, the in-pipeline smoke (~04:17) and my laptop smoke (~04:27), each followed by Cloud Run's idle tail. Roughly 0.5 h of instance time ≈ 1.6 THB, to confirm from billing. `make teardown LAB=5` deletes it (label `lab=5`). |
| Vertex model version `itcs355-6688143@3`, alias `default`, `lab=5` | 04:12 | Metadata, free. **Now the production version**, which v1 was before. Deleting it would hand the gate's incumbent back to v1. Teardown is the coordinator's call. |
| Artifact Registry `itcs355/itcs355-pipeline`: 2 digests, `a8eebe0a…` (unused after run 1) and `e6bcdd12…` | 03:18, 03:50 | Storage only. The image is ~390 MB locally, likely less compressed. |
| GCS `gs://itcs355-6688143/itcs355/lab5-pipeline/` | from 03:19 | Five run directories plus `_root/` (KFP's own outputs). Each run's data is 18,000 rows, ~1.5 MB. |
| 5 PipelineJob records, 21 custom-job records | — | Metadata, free. |

**Nothing else.** No endpoint, no always-on instance, no Cloud Scheduler job. I didn't touch
`scamshield`, `scamshield-deployer` or `scamshield-oidc`.

**Against the estimate.** The pipeline lines of `reports/lab5-cost.md` §1 assumed 9
debugging-and-demo runs plus 2 scheduled ones, at ~1.9 THB each (17 + 4 THB). There were
5 runs in total. The ceiling above is about 11 THB of compute and fees, plus ~1.6 THB for
staging. The estimate's per-step billing assumption (~1.5 min) was too low for the first
runs, where provisioning took 3–5 min per step. There were also far fewer runs, so the
total lands under the estimate. Billing is the number that counts; this table is what to
check it against.
