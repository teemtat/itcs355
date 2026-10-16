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
