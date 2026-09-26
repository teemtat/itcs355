# Lab 3 — Serving, load testing, and rollback

Service: `itcs355-serve` on **Cloud Run**, asia-southeast1. Model: Vertex Model Registry
`itcs355-6688143@1` (the Lab 2 model, RandomForest depth 4). Image:
`itcs355-serve@sha256:97e7928a…` (full reference in `reports/lab3/serve-image.txt`).
Client: k6 v1 on a laptop in Thailand. Raw k6 summaries: `reports/lab3/k6/`. Every deploy
and traffic change, with its UTC time: `reports/lab3/events.jsonl`.

## Latency target (stated before measuring)

> **p95 < 200 ms for single-row `POST /predict`, at 10 concurrent clients, error rate < 1%,**
> measured from the client, so it includes the Thailand–Singapore round trip.

It is written into `loadtest/k6.js` as a k6 threshold. Commit `5242792`
(2026-09-26 11:49 +07) added it. The first run against the deployed endpoint started at
11:52 +07. Why 200 ms: an operator dashboard scores a machine when its reading arrives,
and past ~200 ms the update stops feeling immediate. Why 10: about ten dashboards open
at peak.

## Deployment

| | |
|---|---|
| Platform | Cloud Run service, one revision per model version × instance size |
| Instance | `1cpu-2gi` first, then `2cpu-4gi`. `min-instances=1`, `max-instances=1`, instance-based billing |
| Probes | startup → `/ready` (no traffic until the model is loaded), liveness → `/health` |
| Model load | once, at startup. `deploy()` resolves `name@version` in the Vertex registry to its `gs://` artifact and passes it as `MODEL_URI`. The container fetches it through `adapter.download()` |
| Startup log | `{"event":"model_loaded","model_version":"1","model_uri":"gs://…/lab2-registry/v1","load_ms":7435.1}` |
| Smoke | `make smoke`: single → 200, batch(2) → 200, out-of-range → 422, all with `model_version` |

`max-instances=1` is deliberate. The numbers below describe what **one instance** can do,
not what the autoscaler can hide.

## Three concurrency levels

60 s per level, single-row `/predict`. `srv` is the server's own time from the
`Server-Timing` header.

### 1 vCPU / 2 GiB (`run-1cpu-2gi`)

| Concurrency | Throughput (req/s) | p50 | p95 | p99 | Errors | srv p50 |
|---:|---:|---:|---:|---:|---:|---:|
| 1  | 15.2 | 56 ms  | 131 ms | 140 ms | 0.00% | 10 ms |
| 10 | 48.5 | 200 ms | **305 ms** ✗ | 369 ms | 0.00% | 90 ms |
| 50 | 63.7 | 785 ms | 873 ms | 920 ms | 0.00% | 10 ms |

### 2 vCPU / 4 GiB (`run-2cpu-4gi`), one instance size up

| Concurrency | Throughput (req/s) | p50 | p95 | p99 | Errors | srv p50 |
|---:|---:|---:|---:|---:|---:|---:|
| 1  | 16.9  | 52 ms  | 112 ms | 131 ms | 0.00% | 10 ms |
| 10 | 92.7  | 101 ms | **179 ms** ✓ | 236 ms | 0.00% | 27 ms |
| 50 | 115.1 | 435 ms | 557 ms | 684 ms | 0.00% | 8 ms |

A second 40 s run at 10 on 2 vCPU gave p95 152 ms at 109 req/s (`2cpu-sweep-c10`).

**The configuration that meets the target is `2cpu-4gi`**, one warm instance: p95 179 ms at
concurrency 10.

### Breaking point

Sweeps of 40 s each:

| Concurrency | 1 vCPU p95 | 1 vCPU req/s | 2 vCPU p95 | 2 vCPU req/s |
|---:|---:|---:|---:|---:|
| 2  | 129 | 28.0 | | |
| 4  | 132 | 55.9 | | |
| 6  | 155 | 63.3 | | |
| 8  | 184 | 66.7 | | |
| 9  | **205** | 65.2 | | |
| 10 | 227 | 63.6 | 152 | 109.4 |
| 12 | | | 166 | 115.5 |
| 14 | | | 180 | 122.4 |
| 16 | | | 198 | 128.4 |
| 18 | | | **229** | 113.9 |
| 20 | | | 227 | 128.9 |

- **1 vCPU breaks at concurrency 9** (p95 205 ms).
- **2 vCPU breaks at concurrency 18** (p95 229 ms). 16 is the last level inside the
  target, and only just (198 ms).
- There were no errors at any level. The service fails by getting slower, not by
  returning errors.

**Reading the numbers.**
- Throughput levels off at about 64 req/s on 1 vCPU and about 129 req/s on 2 vCPU,
  which is roughly 15 ms of CPU per request.
- Doubling the vCPUs doubles the ceiling. That means the ceiling is the server, not the
  client (common failure mode: "flat ceiling regardless of concurrency"). The same k6
  process on the same laptop pushed twice as many requests once the server had twice the
  CPU.
- Past saturation, the server's own time stays at ~8–10 ms while the client sees
  400–800 ms. The extra time is queueing in front of the app, which the app cannot see.
  So the server's own latency metric looks healthy during an overload, and only
  client-side percentiles show the problem.
- The p95 at concurrency 1 (~110–130 ms against a 51–56 ms median) comes from a minority
  of slow round trips. The server-side p95 is 12–13 ms, so the extra is network, not
  service.
- The first 1-vCPU run at concurrency 10 (p95 305 ms) was slower than the repeat
  (227 ms). Its server-side time was also higher (median 90 ms against 10 ms). It was the
  first sustained load on a fresh instance. Both runs are over the target, so the
  conclusion does not change, but that first run is the worst case.

### Cold start (reported separately)

The load-tested revisions keep one warm instance, so none of the percentiles above
contains a cold start. Cold start was measured on a scale-to-zero revision
(`min-instances=0`, no traffic, left idle long enough to be reclaimed):
`scripts/cold_start.py` → `reports/lab3/cold-start.json`.

| Sample (idle 17 min first) | First request | Next 5 requests | Service log confirms a cold start |
|---|---:|---:|---|
| 05:47:27 UTC | **14.6 s** | 121–211 ms | yes: `model_loaded` at 05:47:42, `load_ms` 10,293 |
| 06:06:15 UTC | **8.4 s**  | 125–175 ms | yes: `model_loaded` at 06:06:22, `load_ms` 3,788 |

- **Cold start is 8–15 s, about 100× the warm p95.**
- Most of it is the service's own startup: Python imports, fetching the model from the
  registry bucket, and unpickling (3.8–10.3 s). Container start and the `/ready` startup
  probe make up the rest.
- On a scale-to-zero configuration, every first request after a quiet period would take
  that long, and it would dominate p99.
- That is why the target-meeting configuration keeps `min-instances=1`. It is also why
  that configuration is paid for 24 h a day (see Cost).

## Batch size

2 vCPU. Same row values, 100 predictions either way.

| | Latency per call (avg) | Predictions/s |
|---|---:|---:|
| 100 × `/predict`, one client, sequential | 58.5 ms × 100 = **5.85 s** | 16.9 |
| 1 × `/predict/batch` with 100 rows, one client | **67 ms** | 1,472 |
| `/predict` at concurrency 10 | 107 ms | 93 |
| `/predict/batch` (100 rows) at concurrency 10 | 102 ms | **9,728** |

- One 100-row batch replaces 100 single calls at about **87× less wall time** for one
  client, and **~100× the predictions per second** at concurrency 10.
- The reason is that almost none of the cost is per row. The round trip (~45 ms) and the
  fixed per-call overhead (HTTP, validation, one `predict_proba` call over 100 trees) are
  paid once per request. Scoring 100 rows took 7.6 ms on the server, against 6.4 ms for
  one row.
- The advantage did not disappear at any concurrency tested. The batch run at
  concurrency 10 was still inside the 200 ms p95.

## Payload size: where serialisation starts to dominate

**Deployed endpoint, 2 vCPU, concurrency 1**:

| Rows | Request bytes | Client p50 | Server total p50 | Server scoring p50 |
|---:|---:|---:|---:|---:|
| 1   | 243    | 51 ms | 8.0 ms | 6.4 ms |
| 10  | 1,379  | 55 ms | 8.2 ms | 6.5 ms |
| 25  | 3,405  | 55 ms | 8.2 ms | 6.5 ms |
| 50  | 6,948  | 53 ms | 8.8 ms | 6.9 ms |
| 100 | 14,140 | 58 ms | 9.9 ms | 7.6 ms |

- Up to the API's own cap of 100 rows (14 KB), serialisation never dominates. Scoring is
  ~75% of the server's time, and the network round trip is most of what the client sees.
- To find the crossover, the payload has to exceed the cap, so `scripts/payload_bench.py`
  times the service's own code path in-process, stage by stage (on the laptop, so read
  the shares rather than the milliseconds). Raw table: `reports/lab3/payload-bench.md`.

| Rows | Request | decode + validate | → DataFrame | score | encode |
|---:|---:|---:|---:|---:|---:|
| 100     | 15 KB   | 0.13 ms | 0.29 ms | 1.77 ms | 0.01 ms |
| 1,000   | 156 KB  | 1.47 ms | 1.26 ms | 2.63 ms | 0.06 ms |
| 5,000   | 781 KB  | 7.36 ms | 4.19 ms | 4.36 ms | 0.19 ms |
| 20,000  | 3.1 MB  | 29.0 ms | 18.5 ms | 11.7 ms | 0.74 ms |
| 100,000 | 15.6 MB | 164 ms  | 153 ms  | 56 ms   | 3.8 ms  |

- Scoring cost barely grows with rows. A forest's cost is mostly per call, not per row.
  Per-row Pydantic validation and building the DataFrame grow linearly.
- **Request handling overtakes scoring at about 1,000 rows (~150 KB).** By 5,000 rows it
  is ~2.7× the scoring time, and at 100,000 rows it is ~5.6×.
- The 100-row cap stays well on the scoring-dominated side, so it is not what limits
  throughput.

## Instance size: one step up

| | 1cpu-2gi | 2cpu-4gi | Change |
|---|---:|---:|---:|
| p95 at concurrency 10 | 305 ms (227 ms rerun) | 179 ms (152 ms rerun) | −41% (−33%) |
| p95 at concurrency 1 | 131 ms | 112 ms | −15% |
| Saturation throughput | ~64 req/s | ~129 req/s | ×2.0 |
| Breaking concurrency | 9 | 18 | ×2 |
| Price (instance-based, Singapore) | $0.09504/h = **3.17 THB/h** | $0.19008/h = **6.34 THB/h** | ×2.0 |
| THB per 1,000 predictions at saturation | 0.0138 | 0.0137 | ≈ same |

- Twice the price buys twice the throughput. At full load the cost per prediction is the
  same.
- What the extra money buys is **headroom**: p95 under the target at the stated
  concurrency.
- Latency at concurrency 1 barely moves (−15%). An unloaded request is mostly network, and
  more CPU does not shorten the round trip.

Prices come from the Cloud Billing Catalog API (Cloud Run service `152E-C115-5142`, read
2026-09-26):
- instance-based CPU: $0.0000216 per vCPU-second (SKU `4D3C-D63E-1DF1`)
- instance-based memory: $0.0000024 per GiB-second (SKU `7550-6D11-4653`)
- USD/THB 33.36, as in `src/costs.py`
- no per-request fee under instance-based billing; free tier ignored

## Canary and rollback

### The worse version

`itcs355-6688143@2` in the Vertex registry, made by `scripts/make_canary_model.py`. Scores
from `reports/lab3/canary-model.json`:

| test split | v1 (stable) | v2 (canary) |
|---|---:|---:|
| ROC AUC | 0.8533 | 0.8485 (−0.005) |
| log loss | 0.2706 | 0.3145 |
| Brier | 0.0803 | 0.0904 |
| mean predicted p | 0.120 | 0.197 |
| share with p > 0.3 | 11.1% | 22.3% |

- The story behind v2 is a plausible "boost recall" change: failures are up-weighted 2×
  and the trees are cut to depth 2.
- Ranking barely changes, so an offline AUC gate would have let it through.
- What it breaks is calibration. Every probability is pushed upwards, so a dashboard that
  alerts at a fixed threshold fires about twice as often.

### Setup

Both revisions run on `2cpu-4gi` with one warm instance each, so the canary cannot be
spotted by latency. `scripts/canary_watch.py` does the whole run:

1. Replays labelled rows from the held-out test split with 6 client threads (~70 req/s).
2. Baseline: 180 s with 100% on v1.
3. `adapter.set_traffic` switches to **90/10**.
4. Every 10 s it checks the endpoint as a whole.
5. On an alert it rolls back automatically: `set_traffic(v1=100)`.

**The detector is blind to the version.**
- It sees only the stream of predicted probabilities for the endpoint as a whole.
- Test: z = (mean p over the last 120 s − baseline mean) / (baseline sd / √n).
- Alert when z > 4 on two consecutive checks.
- The same test run over the baseline never went above |z| = 0.86 (90/10 run) or 0.90
  (50/50 run), so the threshold has a wide false-alarm margin.
- The `x-model-version` header of every response is written to the request log **only as
  evidence**. The detection code never reads it.

### What happened (90/10, run `run90`)

| UTC | Event | Source |
|---|---|---|
| 05:14:48.4 | traffic v1=100 (baseline starts) | `events.jsonl` |
| 05:18:12.5 | first response from v2 | request log |
| 05:18:17.2 | `update-traffic` returned: v1=90, v2=10 | `events.jsonl`, platform-reported split |
| 05:21:28.3 | **alert**: z = 4.04 then 4.63 | `run90-windows.csv` |
| 05:21:31.4 | last response from v2 | request log |
| 05:21:33.6 | `update-traffic` returned: v1=100 (reported by the platform) | `events.jsonl` |

**Detection took 196 s** from the first v2 response (190 s from the moment the split
command returned). **Rollback took 5.9 s** from the alert to a confirmed 100/0 split.
There were no errors in 42,011 requests.

### Evidence that traffic actually moved

Two sources that do not depend on each other:

1. **Client side**, from the response header of every request
   (`run90-versions-per-10s.csv`): v2's share is 0% during the baseline, 8–12% after the
   split, 0.9% in the 10 s bucket that contains the rollback, then 0% until the end.
2. **Platform side**, Cloud Monitoring `run.googleapis.com/request_count` per revision per
   minute (`run90-platform-counts.csv`, from `scripts/traffic_evidence.py`):

| Minute ending (UTC) | v1 revision | v2 revision |
|---|---:|---:|
| 05:17:33 | 4,827 | 0 |
| 05:18:33 | 4,795 | 0 |
| 05:19:33 | 4,516 | 379 |
| 05:20:33 | 4,487 | 449 |
| 05:21:33 | 4,398 | 505 |
| 05:22:33 | 4,569 | 266 |
| 05:23:33 | 4,832 | **0** |

The platform aggregates by minute and shows the v2 traffic about one minute later than
the client does. Both sources agree that v2 served nothing after the rollback.

Figure: `reports/lab3/canary/canary.png` plots v2's share and the detector's z against
time for both runs.

**In hindsight** (version joined back in after the run, not available to the detector):
mean p was 0.121 from v1 and 0.199 from v2. At a 10% share that shifts the endpoint's
mean by ~0.008, which is what the detector picked up.

### Same thing at 50/50 (run `run50`)

- Detected **34 s** after the first v2 response (30 s after the split returned).
- Rolled back 4.7 s later.
- v2 served 1,571 requests in total, about the same as the 1,599 it served during the
  3-minute 90/10 run.

### Write-up (five lines)

1. **What revealed it:** the endpoint-wide **mean predicted probability**, a label-free
   output metric. Rolling log loss against labels never moved outside its noise; it even
   drifted *down*. At a 10% share the damage is diluted tenfold, and the label-based
   metric would have needed on the order of 10⁵ requests.
2. **How long:** **196 s** from the first request that v2 served (about 1,600 canary
   requests, ~14,000 in total), then 5.9 s to a confirmed rollback.
3. **What would make it faster:**
   - Compare the canary to the stable version *per revision*, not the blended endpoint
     mean. That would be ~10× the signal at 90/10.
   - Use a sequential test (CUSUM) instead of a 120 s window that must fill up.
   - Send more traffic.
   - Have a pre-agreed calibration check (mean p, alert rate) on every release.
4. **At 50/50:** detection took **34 s instead of 196 s**. Each request carries 5× the
   shift, so about 25× fewer requests are needed, capped by the 10 s check interval and
   the two-check rule.
5. **The catch:** v2 reached almost exactly as many users either way (~1,570–1,600
   requests). 50/50 finds the problem faster but exposes half of all users while it does.
   90/10 limits how many users are affected in any one minute and pays for it in
   detection time.


## Cost per 1,000 predictions

**Configuration:** `2cpu-4gi`, the one that meets the target, one always-warm instance,
instance-based billing: **6.34 THB/h** ($0.19008/h, derivation above).

**Throughput:** 92.7 req/s, measured at the stated concurrency of 10 inside the p95
target. This is the lower of the two runs at concurrency 10; the capacity at saturation is
~129 req/s.

**Utilisation assumption (stated explicitly): 25%.** The endpoint is sized for the peak
of ten dashboards. Average traffic is assumed to be a quarter of peak (busy shifts versus
nights), and the instance is paid for 24 h regardless.

```
predictions per hour at 25%  = 92.7 × 0.25 × 3600 = 83,430
cost per 1,000               = 6.34 THB / 83.43   = 0.076 THB
```

The utilisation is the fragile part (`src/costs.cost_per_1k_predictions`, same inputs):

| Utilisation | THB per 1,000 predictions |
|---:|---:|
| 5%  | 0.380 |
| **25%** | **0.076** |
| 80% | 0.024 |
| 100% | 0.019 |

The same endpoint costs 20× more per prediction at 5% utilisation than at 100%. The
per-hour price does not change; only how much of it is wasted.

**When batch inference is cheaper:**
- The warm endpoint costs **152 THB/day** whatever it serves.
- A Cloud Run job on the same 2 vCPU scoring `/predict/batch`-style chunks (1,472
  predictions/s measured) costs about 0.0012 THB per 1,000 predictions, plus ~0.05 THB of
  startup per run.
- Those two only meet at **~127 million predictions a day (~1,470 req/s)**. That is 11×
  more than this instance can serve (~11 million/day at saturation).
- **At any volume this endpoint can actually carry, a daily batch is cheaper. The warm
  endpoint is worth paying for only when a prediction is needed within seconds.**


## Teardown

`make teardown` (LAB=3 by default now) deleted the Cloud Run service `itcs355-serve` and
all of its revisions at ~06:08 UTC. After that:
- `gcloud run services list` returns 0 services.
- No Vertex endpoint was ever created.
- `make cost-report` finds nothing tagged `lab=3`.

The record is in `reports/lab3/teardown.txt`.

What is kept on purpose:
- the image in Artifact Registry
- `itcs355-6688143@2` in the model registry
- the model file in the bucket

These are storage, not compute: a few MB with no hourly charge.

**Still check the billing console by hand.**
