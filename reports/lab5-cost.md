# Lab 5 — Cost report

## 1. Estimate before running

Written on 2026-10-16, **before** anything in Lab 5 was built or run. The commit that adds
this section comes before the first Lab 5 pipeline commit, so the history shows this number
was not adjusted afterwards to match the bill.

**Prediction for the whole of Lab 5: about 45 THB, with an upper bound of 70 THB.**
Including Labs 1–4 (฿38.88 of usage in the 90-day billing view, `reports/lab4/billing-90days.webp`),
the term total would be about 85 THB, and 110 THB at the upper bound. The cap is 800 THB.

### What it is based on

- **Rates.** From `src/costs.py`, checked on 2026-09-20 for asia-southeast1, at 33.36 THB/USD:
  - n1-standard-4 spot (Vertex, including the management fee): 3.06 THB/h
  - e2-standard-4 on demand: 6.34 THB/h
  - Cloud Run 1 vCPU / 2 GiB, instance-based: 3.17 THB/h
- **Vertex AI Pipelines.** A fee of $0.03 per run (1.00 THB). Each step also runs as its own
  Vertex training job at training rates. If a step doesn't name a machine, it gets
  e2-standard-4.
- **Lab 4's gap.** The bill came to about 2.4× my Lab 4 estimate. That estimate counted only
  the time spent serving. It left out instance start-up and the idle time Cloud Run keeps an
  instance alive after the last request. So below I count idle tails on purpose, and I keep a
  bigger contingency than Lab 4 had.

### One pipeline run

| Step | Machine | Billed time (assumed) | THB |
|---|---|---|---|
| Run fee | — | — | 1.00 |
| ingest, validate, evaluate, register, deploy | e2-standard-4 on demand | 5 × ~1.5 min = 7.5 min | 0.79 |
| train | n1-standard-4 **spot** | ~2.5 min | 0.13 |
| **Per run (a full pass)** | | | **≈ 1.9** |

A run that stops at validate or at the gate costs less, about 1.4 THB. Still, I price every run
as a full one: provisioning time per step is the number I'm least sure of. Vertex brings up a
fresh machine for each step, and how much of that I'm billed for is what this estimate has
to find out.

### Lab 5 total

| Component | Working | THB |
|---|---|---|
| Pipeline runs: 1 pass, 1 gate-blocked, 1 validate-abort, ~6 debugging runs | 9 × 1.9 | 17 |
| Scheduled runs: 1 to confirm the schedule fires, plus 1 weekly fire if it happens before teardown | 2 × 1.9 | 4 |
| Staging deploys on Cloud Run (scale to zero, 1 vCPU / 2 GiB) | ~3 deploys × (startup + ~15 min idle tail) ≈ 0.85 h × 3.17 | 3 |
| Staging smoke tests | a few requests, inside the idle tail above | 0 |
| Artifact Registry: one new pipeline-step image (~1 GB, with gcloud in it) | $0.10/GB-month, part of a month | 2 |
| Cloud Storage: pipeline root, per-run data, model artifacts | well under 1 GB | 0.1 |
| Monitoring and logging: custom metrics, pipeline and job logs | inside the free tiers at this volume | 1 |
| Vertex Gemini calls for Part B (golden set, baseline, changed run, guardrail demo) | ~300 calls × ~1.5k input + ~400 output tokens = 0.45 M in, 0.12 M out. Gemini Flash list price $0.30 / $2.50 per M → 0.45 × 10.0 + 0.12 × 83.4 | 15 |
| Short second-provider test (upload / download / invoke), deleted the same day | storage and one invocation target for about an hour; probably inside free tier | 3 |
| **Total** | | **≈ 45** |
| **Upper bound** (+50% for provisioning overhead and a warm instance left up by mistake) | | **≈ 70** |

### What would make this wrong

- **Provisioning billed per step.** If Vertex bills each step for most of its ~1–2 min machine
  start-up, and debugging takes more runs than six, the pipeline line could double. That alone
  would still keep Lab 5 under 70 THB.
- **A warm instance left running.** A staging revision deployed with `min-instances=1` and left
  on overnight would cost 3.17 × 24 = 76 THB a day. That would cost more than the rest of this
  table, which is why the pipeline deploys staging with `min-instances=0`.
- **Token counts.** The Gemini line is the one built on numbers I haven't measured. The real
  counts must come from the provider's usage fields (Part B, Task 9), not from this estimate.
  The token price above is the list price as I know it. `src/llmcost.py` still marks its
  rates unverified.

---

## 2. Actual, from billing filtered by tag

Read on 2026-10-18 from Cloud Billing → Reports for billing account `01BC39-DAC66E-ADC6E0`,
project `itcs355-6688143`, 1–18 Oct 2026. Every figure is **usage cost before credits**. The
free-trial credits covered all of it, so the amount actually paid is 0 THB.

View used for the `lab=5` lines, grouped by SKU and paged through (15 SKUs):
`console.cloud.google.com/billing/01BC39-DAC66E-ADC6E0/reports;timeRange=CUSTOM_RANGE;from=2026-10-01;to=2026-10-18;grouping=GROUP_BY_SKU;labels=lab:5?project=itcs355-6688143`

| Slice | Billing filter | THB |
|---|---|---|
| Everything carrying the Lab 5 label | `labels=lab:5` | **8.30** |
| Gemini calls for Part B (Vertex prediction calls carry no labels) | the six `Gemini 2.5 Flash / Flash Lite … Predictions` SKUs, unfiltered | **1.04** |
| Pipeline bucket prefix and pipeline image (shared bucket and repo, so no label) | `Standard Storage Singapore`, `Artifact Registry Storage` | < 0.10 *(the line is 0.08 for all of October, every lab)* |
| **Lab 5 on GCP** | | **≈ 9.34** |
| Azure second-provider run (Task 4), Azure for Students credit | not on GCP billing. `az consumption usage list` returns no prices on a student subscription | ≈ 6 *(an estimate, not billing: one day of ACR Basic, everything else inside free grants)* |

**Reported actual: 9.34 THB on GCP, from billing.** Azure is shown on its own line and
marked as an estimate, because no bill for it could be read.

Raw view of the whole month by service (every lab plus the capstone, 30.81 THB of usage,
paid by credits): `reports/lab5/billing-oct-1-17-by-service.webp`. The **term total is far
under the 800 THB cap**: 38.88 THB for Labs 1–4 (90-day view) plus 9.34 for Lab 5.

One oddity to record: the report's *charge-period* range of 16–18 Oct showed only storage,
even though every Lab 5 resource was created on 16 Oct. The `lab=5` lines only appear over
the month-to-date range. So the label filter, not the date filter, is what isolates Lab 5
here.

## 3. The gap

**Estimate ≈ 45 THB (upper bound 70) against an actual 9.34 THB on GCP: −36 THB, −79%.**
The Azure estimate doesn't change the picture.

Lab 4 went over its estimate by 2.4× because it left out idle tails, so for Lab 5 I padded
every line. I padded too much. The gap breaks down like this:

| Line | Estimate | Actual | Why |
|---|---|---|---|
| Gemini (Part B) | 15 | 1.04 | I assumed about 300 calls at 1.5k input and 400 output tokens. The real run was 85 calls with shorter prompts (490 input and 47 output tokens on the shipped configuration, `reports/lab5-llm.md`). Recorded fixtures replace live calls, so the gate re-runs for free |
| Pipeline runs | 21 | 7.04 | Two separate errors. **Count:** I planned 11 runs (9 manual and 2 scheduled) and only 5 happened. Debugging took fewer runs than feared, and the weekly fire was deleted before it came round. **Per-run compute:** estimated 0.92 THB, actual 0.40. Vertex billed only about 0.22 h of E2 core across 15 steps, so provisioning time was mostly free, the opposite of the risk I flagged in section 1. The **fee per run was exact**: $0.03, 5 × = 5.04 THB billed |
| Staging | 3 | 1.26 | Scale to zero worked. 1,420 vCPU-seconds is about 24 minutes of instance time across every deploy, smoke test and idle tail |
| Artifact Registry, storage, monitoring | 3.1 | < 0.10 | The image is smaller than the 1 GB I assumed, and everything else stayed inside free tiers |
| Azure test | 3 | ≈ 6 (estimate, student credit) | Not on GCP billing. One day of ACR Basic is the likely charge |

The surprise: **the pipeline run fee (5.04 THB) is the largest single item in the lab.** Each
step finishes in seconds, so the orchestrator's flat fee costs more than all the compute it
schedules, about 2.5× the 2.00 THB of training and step compute together.

**Check from my own logs (`reports/lab5-pipeline.md` §5):** the resource-hour ceilings add up
to 13.9 THB with Gemini included. Billing (9.34) is lower because I priced e2-highmem-2 at the
e2-standard-4 rate and counted provisioning time. Line by line, the spot training figure
(created→end 0.549 h × 3.06 = 1.68 against 1.41 billed) and the fee (5.00 against 5.04) both
come within 20%.

## 4. Breakdown by component

All figures are GCP usage before credits, 1–18 Oct, label `lab=5` unless noted.

| Component | THB | What is in it |
|---|---|---|
| Training | **1.41** | Spot n1 for the train step: 2.99 core-h 0.89, 11.2 GiB-h RAM 0.45, Vertex management fee 0.07 |
| Pipeline | **5.63** | Run fee 5.04 (5 runs × $0.03), E2 compute for the ingest/validate/evaluate/register/deploy steps 0.47, persistent disk 0.12 |
| Serving (staging) | **1.26** | Cloud Run instance CPU 1.03, memory 0.23, request-tier CPU 0.01, 3 requests |
| LLM step (Part B) | **1.04** | Gemini 2.5 Flash input 0.28, output 0.26, thinking output 0.47; Flash-Lite 0.03 (unlabelled, read by SKU) |
| Storage | **< 0.10** | Bucket prefix `lab5-pipeline/` (~8 MB) and two pipeline image digests. Shared resources, so this is an upper bound |
| Monitoring | **0.00** | Logs and metrics inside the free tiers. No alert policy was created in Lab 5 |
| **Total (GCP)** | **≈ 9.34** | |
| Second provider (Azure, Task 4) | ≈ 6 *(estimate)* | On the student credit. See section 2 |

Take the fee out of *Pipeline* and 0.59 THB is left. The fee is about 90% of what the pipeline costs.
Training + Pipeline + Serving = 1.41 + 5.63 + 1.26 = 8.30, which is the `lab=5` total.

## 5. Cost per 1,000 predictions

From `src/costs.cost_per_1k_predictions`, using throughput measured in the Lab 3 load test
(`reports/lab3-load.md`): the ceiling of one instance is about 64 req/s on 1 vCPU and about
129 req/s on 2 vCPU. These are always-on prices (instance-based billing).

| Utilisation | 1 vCPU / 2 GiB (3.17 THB/h, 64 req/s) | 2 vCPU / 4 GiB (6.34 THB/h, 129 req/s) |
|---|---|---|
| 5% | 0.2752 THB | 0.2730 THB |
| 25% | 0.0550 THB | 0.0546 THB |
| 80% | 0.0172 THB | 0.0171 THB |

- **Utilisation, not machine size, drives the price.** The two sizes cost almost exactly the
  same per prediction, because doubling the vCPU doubled both the price and the throughput.
  Going from 5% to 80% utilisation is a 16× difference.
- **The 5% row is the one I believe for this system.** It is a maintenance classifier for one
  fleet, called by people and by a nightly job, nowhere near tens of requests a second.
- **For comparison, the LLM step costs 8.86 THB per 1,000 requests** (`reports/lab5-llm.md`,
  Task 9). That is about 30× the classifier even at 5% utilisation. Per request, the token
  bill dominates any serving bill in this system.
- `batch_breakeven_rps` puts the point where a warm 1 vCPU endpoint stops beating a daily
  batch job at **~0.0009 req/s**, about 75 requests a day. Real traffic sits above that
  during the day and below it at night. That is the argument for scale-to-zero, not for
  batch only.

## 6. One optimisation actually applied: right-size, then scale to zero

Labs 3 and 4 served staging on **2 vCPU / 4 GiB with one instance always on**. That was the
size that met the p95 target at 16 users. For Lab 5, the pipeline's deploy step puts staging
on **1 vCPU / 2 GiB with min-instances 0** (revision `itcs355-staging-v3-1cpu-041639`).

| | Before (Labs 3–4) | After (Lab 5) |
|---|---|---|
| Configuration | 2 vCPU / 4 GiB, min 1, max 1 | 1 vCPU / 2 GiB, min 0, max 1 |
| Rate while an instance is up | 6.34 THB/h | 3.17 THB/h |
| Cost of keeping staging available for a day | **152 THB** (24 h × 6.34) | **1.26 THB** for the whole lab, from billing (≈ 0.39 instance-hours) |
| THB per 1,000 at 5% utilisation | 0.2730 | 0.2752 while warm. Zero while nobody calls |
| Throughput ceiling (one instance) | ~129 req/s | ~64 req/s |
| Latency, warm, 1 user (client side) | p50 ~51 ms (Lab 3 payload test, 2 vCPU) | 283 ms on the first smoke request, then 133–146 ms (`reports/lab5/pipeline/staging-smoke.txt`; the client here is the pipeline step, not the laptop) |
| **First request after idle** | none (always warm) | **8–15 s cold start** (Lab 3 measurement, `reports/lab3/cold-start.json`) |

**What it bought:** a staging service that costs nothing while nobody uses it. Lab 5's
staging existed for about 2 hours (created 04:14 UTC, deleted at teardown around 06:09 UTC).
The old configuration would have billed about 12.7 THB for those 2 hours, and the lab spent
1.26 THB. Left up for a week, which is how a staging service usually lives, that is about
1,065 THB avoided. Under the old setup, staging alone would have broken the 800 THB term cap.

**What it cost:** half the throughput ceiling, and an 8–15 s first request after every quiet
period. That is a trade, not a free win. It is the right trade for *staging*, which only CI
and a person running smoke tests ever call. It would be the wrong trade for production at
the Lab 3 target (p95 < 200 ms for every request), where the cold start would break the SLO
for the first user of every quiet spell. Production would keep min-instances 1 or use a
startup-CPU-boost configuration.
