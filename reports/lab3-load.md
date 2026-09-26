# Lab 3 — Serving, load testing, and rollback

I put the Lab 2 model behind an API, tested how much traffic it can take, and practised
rolling back a bad version.

- **Where it runs:** Cloud Run, Singapore (asia-southeast1). Service name `itcs355-serve`.
- **Model:** `itcs355-6688143@1` from the Vertex model registry (the Lab 2 model).
- **Image:** `itcs355-serve@sha256:97e7928a…` (full name in `reports/lab3/serve-image.txt`).
- **Load tests:** k6, run from my laptop in Thailand. Raw results in `reports/lab3/k6/`.
- **Log of every deploy and traffic change** (with time): `reports/lab3/events.jsonl`.

A quick guide to the numbers: **p50** is the typical request. **p95** means 95% of
requests were faster than this. **p99** is the slow tail. "Concurrency 10" means 10 users
sending requests at the same time, non-stop.

---

## My latency target (set before measuring)

> **p95 under 200 ms for a one-row `/predict`, with 10 users at once, and under 1% errors.**
> Measured from my laptop, so the trip to Singapore and back counts.

It is in `loadtest/k6.js`. I committed it at 11:49 (commit `5242792`), and the first load
test ran at 11:52.

- **Why 200 ms:** the users are dashboards that show a machine's risk when a new reading
  comes in. Above ~200 ms the update stops feeling instant.
- **Why 10 users:** that is about how many dashboards would be open at the busiest time.

---

## How it's deployed

| | |
|---|---|
| Platform | Cloud Run. Each model version / machine size is its own "revision" |
| Machine size | first 1 vCPU + 2 GB, then 2 vCPU + 4 GB. Always exactly 1 instance running |
| Health checks | Cloud Run waits for `/ready` before sending any traffic, and uses `/health` to check it's still alive |
| Loading the model | once, when the container starts. `deploy()` looks up `name@version` in the registry and gives the container the `gs://` folder. The container downloads it through the adapter |
| Startup log | `{"event":"model_loaded","model_version":"1","model_uri":"gs://…/lab2-registry/v1","load_ms":7435.1}` |
| Smoke test | `make smoke`: one row → 200, batch of 2 → 200, bad input → 422. All answers include `model_version` |

I fixed it at 1 instance on purpose. The numbers below show what **one machine** can
handle. If Cloud Run could add machines, it would hide the limit.

---

## Results at 1, 10 and 50 users

60 seconds per level, one row per request. "Server time" is how long the app itself took,
without the network.

### 1 vCPU / 2 GB

| Users | Requests/s | p50 | p95 | p99 | Errors | Server time (p50) |
|---:|---:|---:|---:|---:|---:|---:|
| 1  | 15.2 | 56 ms  | 131 ms | 140 ms | 0% | 10 ms |
| 10 | 48.5 | 200 ms | **305 ms** ✗ | 369 ms | 0% | 90 ms |
| 50 | 63.7 | 785 ms | 873 ms | 920 ms | 0% | 10 ms |

### 2 vCPU / 4 GB (one size up)

| Users | Requests/s | p50 | p95 | p99 | Errors | Server time (p50) |
|---:|---:|---:|---:|---:|---:|---:|
| 1  | 16.9  | 52 ms  | 112 ms | 131 ms | 0% | 10 ms |
| 10 | 92.7  | 101 ms | **179 ms** ✓ | 236 ms | 0% | 27 ms |
| 50 | 115.1 | 435 ms | 557 ms | 684 ms | 0% | 8 ms |

A second run at 10 users on 2 vCPU gave p95 152 ms at 109 requests/s.

**Setup that meets the target: 2 vCPU / 4 GB, one instance always on.** p95 was 179 ms at
10 users.

### Breaking point

To find where it breaks, I ran more levels, 40 seconds each:

| Users | 1 vCPU p95 | 1 vCPU req/s | 2 vCPU p95 | 2 vCPU req/s |
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

- **1 vCPU breaks at 9 users** (p95 205 ms).
- **2 vCPU breaks at 18 users** (p95 229 ms). 16 users still passes, just barely (198 ms).
- **No errors at any level.** When it's overloaded it gets slower. It doesn't fail.

**What I learned from this:**
- **There's a hard ceiling.** 1 vCPU tops out around 64 requests/s, 2 vCPU around 129.
  Each request costs about 15 ms of CPU.
- **The limit is the server, not my laptop.** Doubling the CPU doubled the ceiling, using
  the same laptop running the same k6. If k6 were the problem, the ceiling wouldn't move.
- **The server's own timer hides the problem.** When overloaded, the app says each request
  took ~8–10 ms, but the client waited 400–800 ms. The requests were waiting in line before
  they reached the app, so the app never saw that time. You only see it from the client.
- **Why p95 is ~120 ms even with 1 user:** a few trips over the network are slow. The app
  itself stayed at 12–13 ms, so it's the network, not the service.
- **The first 1 vCPU run at 10 users was slower** (305 ms) than the repeat (227 ms). It was
  the first heavy load on a fresh machine. Both fail the target, so the conclusion is the
  same.

### Cold start (measured separately)

In the tests above, one instance is always on, so none of those numbers include a cold
start. To measure it, I deployed a copy that can scale to zero, left it idle for 17 minutes
so it shut down, then sent one request. Script: `scripts/cold_start.py`, results in
`reports/lab3/cold-start.json`.

| When (UTC) | First request | Next 5 requests | Was it really a cold start? |
|---|---:|---:|---|
| 05:47:27 | **14.6 s** | 121–211 ms | yes, the log shows the model loading (10.3 s) |
| 06:06:15 | **8.4 s**  | 125–175 ms | yes, the log shows the model loading (3.8 s) |

- **A cold start takes 8–15 seconds**, about 100 times slower than a normal request.
- Most of that is the app starting up: loading Python, downloading the model, and opening
  it.
- If the service could scale to zero, the first user after every quiet period would wait
  that long.
- That's why I keep one instance always on. The downside is paying for it 24 hours a day
  (see Cost).

---

## Batch vs single requests

2 vCPU. 100 predictions either way.

| | Time per call (avg) | Predictions/s |
|---|---:|---:|
| 100 separate `/predict` calls, one after another | 58.5 ms × 100 = **5.85 s** | 16.9 |
| 1 `/predict/batch` call with 100 rows | **67 ms** | 1,472 |
| `/predict`, 10 users | 107 ms | 93 |
| `/predict/batch` (100 rows), 10 users | 102 ms | **9,728** |

- **One batch of 100 is about 87× faster** than 100 separate calls. With 10 users it gives
  about 100× more predictions per second.
- **Why:** almost all the cost is per call, not per row. The network trip (~45 ms) and the
  setup for each call are paid once, whether there's 1 row or 100. Scoring 100 rows took
  7.6 ms, compared with 6.4 ms for 1 row.
- Batch was faster at every level I tested. Batch calls at 10 users were still under
  200 ms.

---

## Payload size: when does converting the data cost more than the model?

**On the deployed service, 2 vCPU, 1 user:**

| Rows | Request size | Client p50 | Server total (p50) | Model only (p50) |
|---:|---:|---:|---:|---:|
| 1   | 243 B   | 51 ms | 8.0 ms | 6.4 ms |
| 10  | 1.4 KB  | 55 ms | 8.2 ms | 6.5 ms |
| 25  | 3.4 KB  | 55 ms | 8.2 ms | 6.5 ms |
| 50  | 6.9 KB  | 53 ms | 8.8 ms | 6.9 ms |
| 100 | 14.1 KB | 58 ms | 9.9 ms | 7.6 ms |

Up to 100 rows (the most the API allows), the model is still ~75% of the server's time,
and the network is most of what the client waits for.

To find where it flips, I had to go past 100 rows, which the API rejects. So I timed the
same code on my laptop, step by step (`scripts/payload_bench.py`, raw table in
`reports/lab3/payload-bench.md`). My laptop is faster than the cloud machine, so compare
the steps against each other, not the exact milliseconds.

| Rows | Size | Read + check the JSON | Make a table (DataFrame) | Model | Write the answer |
|---:|---:|---:|---:|---:|---:|
| 100     | 15 KB   | 0.13 ms | 0.29 ms | 1.77 ms | 0.01 ms |
| 1,000   | 156 KB  | 1.47 ms | 1.26 ms | 2.63 ms | 0.06 ms |
| 5,000   | 781 KB  | 7.36 ms | 4.19 ms | 4.36 ms | 0.19 ms |
| 20,000  | 3.1 MB  | 29.0 ms | 18.5 ms | 11.7 ms | 0.74 ms |
| 100,000 | 15.6 MB | 164 ms  | 153 ms  | 56 ms   | 3.8 ms  |

- The model barely slows down with more rows. Checking the JSON and building the table grow
  with every row.
- **They overtake the model at about 1,000 rows (~150 KB).** At 5,000 rows they take ~2.7×
  as long as the model, and at 100,000 rows ~5.6×.
- The 100-row limit is well below that point, so it isn't what slows things down.

---

## One machine size up

| | 1 vCPU / 2 GB | 2 vCPU / 4 GB | Change |
|---|---:|---:|---:|
| p95 at 10 users | 305 ms (227 ms rerun) | 179 ms (152 ms rerun) | −41% (−33%) |
| p95 at 1 user | 131 ms | 112 ms | −15% |
| Max requests/s | ~64 | ~129 | ×2 |
| Breaks at | 9 users | 18 users | ×2 |
| Price | **3.17 THB/h** ($0.09504) | **6.34 THB/h** ($0.19008) | ×2 |
| THB per 1,000 predictions at full load | 0.0138 | 0.0137 | same |

- **Twice the price, twice the capacity.** At full load, each prediction costs the same.
- **What the extra money buys is room to spare:** p95 now stays under the target at 10
  users.
- With just 1 user, it's only 15% faster. That request is mostly network time, and more CPU
  doesn't make the network faster.

Where the prices come from: Google's Cloud Billing Catalog API (Cloud Run, read on
2026-09-26, Singapore):
- CPU: $0.0000216 per vCPU per second
- Memory: $0.0000024 per GB per second
- Exchange rate 33.36 THB/USD, same as `src/costs.py`
- No per-request charge with this billing mode. I ignored the free tier.

---

## Canary and rollback

### The bad version (v2)

I trained a slightly worse model and registered it as `itcs355-6688143@2`
(`scripts/make_canary_model.py`). Scores on the test set (`reports/lab3/canary-model.json`):

| | v1 (current) | v2 (canary) |
|---|---:|---:|
| ROC AUC | 0.8533 | 0.8485 (−0.005) |
| log loss | 0.2706 | 0.3145 |
| Brier | 0.0803 | 0.0904 |
| average predicted probability | 0.120 | 0.197 |
| % of rows with probability > 0.3 | 11.1% | 22.3% |

- **The idea:** someone tries to catch more failures by giving failures 2× weight, and
  makes the trees shallower.
- **AUC barely changes**, so a check that only looks at AUC would let it through.
- **But every probability goes up.** A dashboard that alerts above a fixed threshold would
  fire about twice as often.

### How the test works

Both versions run on 2 vCPU with one instance each, so v2 can't be spotted by being
slower. `scripts/canary_watch.py` does everything:

1. Sends real test rows (with known answers) from 6 threads, about 70 requests per second.
2. **Baseline:** 3 minutes with 100% of traffic on v1, to learn what "normal" looks like.
3. Moves traffic to **90% v1 / 10% v2**.
4. Every 10 seconds, checks whether the whole endpoint looks different from normal.
5. If it raises an alert, it moves traffic back to 100% v1 immediately.

**The detector doesn't know which version answered each request.**
- It only sees the predicted probabilities from the endpoint as a whole.
- **The check:** is the average probability over the last 2 minutes clearly higher than
  normal? It's measured as a z-score, and it alerts when z is above 4 twice in a row.
- During the normal period, z never went above 0.86 (in the 90/10 run) or 0.90 (in the
  50/50 run). So an alert at 4 is not going to go off by chance.
- Every response also says which version answered (`x-model-version`). I save that **only as
  evidence**. The detection code never reads it.

### What happened at 90/10

| Time (UTC) | What happened | Where it's recorded |
|---|---|---|
| 05:14:48.4 | 100% on v1, baseline starts | `events.jsonl` |
| 05:18:12.5 | first answer from v2 | request log |
| 05:18:17.2 | traffic change confirmed: v1 = 90%, v2 = 10% | `events.jsonl` |
| 05:21:28.3 | **alert** (z = 4.04, then 4.63) | `run90-windows.csv` |
| 05:21:31.4 | last answer from v2 | request log |
| 05:21:33.6 | traffic back to v1 = 100%, confirmed by Cloud Run | `events.jsonl` |

- **It took 196 seconds to notice**, counting from v2's first answer (190 s from when the
  traffic change was confirmed).
- **Rolling back took 5.9 seconds**, from the alert until Cloud Run confirmed 100% on v1.
- 0 errors out of 42,011 requests.

### Proof that traffic really moved

I have two separate sources:

1. **From my side:** each answer says which version sent it
   (`run90-versions-per-10s.csv`). v2's share was 0% before, 8–12% during the canary, 0.9%
   in the 10 seconds when the rollback happened, then 0% after.
2. **From Google's side:** Cloud Monitoring counts requests per revision per minute
   (`run90-platform-counts.csv`, from `scripts/traffic_evidence.py`):

| Minute ending (UTC) | v1 | v2 |
|---|---:|---:|
| 05:17:33 | 4,827 | 0 |
| 05:18:33 | 4,795 | 0 |
| 05:19:33 | 4,516 | 379 |
| 05:20:33 | 4,487 | 449 |
| 05:21:33 | 4,398 | 505 |
| 05:22:33 | 4,569 | 266 |
| 05:23:33 | 4,832 | **0** |

Google's numbers are per minute, so they show v2 about a minute later than mine. Both
agree: after the rollback, v2 got nothing.

Chart of both runs: `reports/lab3/canary/canary.png`.

**Checking afterwards** (matching each answer to its version, which the detector couldn't
do): v1's average was 0.121 and v2's was 0.199. With v2 getting 10% of traffic, the overall
average goes up by ~0.008. That small rise is what the detector caught.

### Same test at 50/50

- Noticed **34 seconds** after v2's first answer (30 s after the traffic change).
- Rolled back 4.7 seconds after that.
- v2 answered 1,571 requests. That's almost the same as the 1,599 it answered in the
  3-minute 90/10 run.

### Write-up (five lines)

1. **What showed the problem:** the **average predicted probability** of the whole
   endpoint. It needs no correct answers. Log loss (which needs correct answers) never
   moved outside its normal noise, and even went down. At 10% traffic the damage is watered
   down 10 times, and log loss would have needed around 100,000 requests to show it.
2. **How long it took:** **196 seconds** from v2's first answer (about 1,600 requests to v2,
   ~14,000 in total). Then 5.9 seconds to roll back.
3. **What would make it faster:**
   - Compare v2 directly with v1, instead of the whole endpoint mixed together. That's
     about 10× more signal at 90/10.
   - Use a test that adds up evidence as it goes (like CUSUM), instead of waiting for a
     2-minute window to fill.
   - Send more traffic.
   - Agree on a probability check (average, alert rate) that every new release must pass.
4. **At 50/50:** it took **34 seconds instead of 196**. Each request carries 5× more of the
   change, so it needs about 25× fewer requests. The 10-second check interval and the
   "twice in a row" rule keep it from being even faster.
5. **The trade-off:** v2 answered about the same number of requests either way (~1,570–1,600).
   50/50 finds the problem faster, but half of all users get the bad model while it does.
   90/10 limits how many users are affected each minute, but takes longer to notice.

---

## Cost per 1,000 predictions

**Setup:** 2 vCPU / 4 GB (the one that meets the target), one instance always on:
**6.34 THB/hour**.

**Speed:** 92.7 requests/s, measured at 10 users while meeting the target. This is the
slower of my two runs at 10 users. The absolute max is ~129.

**Utilisation assumption: 25%.** The machine is sized for the busiest time (10
dashboards). On average I assume traffic is a quarter of that (busy shifts versus nights).
But the machine is paid for 24 hours either way.

```
predictions per hour at 25% = 92.7 × 0.25 × 3600 = 83,430
cost per 1,000             = 6.34 THB / 83.43   = 0.076 THB
```

The utilisation guess matters a lot (computed with `src/costs.cost_per_1k_predictions`):

| Utilisation | THB per 1,000 predictions |
|---:|---:|
| 5%  | 0.380 |
| **25%** | **0.076** |
| 80% | 0.024 |
| 100% | 0.019 |

At 5% it costs 20× more per prediction than at 100%. The price per hour is the same. The
difference is how much of the paid time is wasted.

**When is batch cheaper than keeping the endpoint on?**
- Keeping the endpoint on costs **152 THB a day**, even if nobody uses it.
- A batch job on the same machine does 1,472 predictions/s (measured). That's about
  0.0012 THB per 1,000 predictions, plus ~0.05 THB to start each run.
- The two only cost the same at **~127 million predictions a day** (~1,470 per second).
  That's 11× more than this one instance can even handle (~11 million a day at max).
- **So at any volume this endpoint can handle, a daily batch job is cheaper. The endpoint
  is only worth paying for when you need the answer right away.**

---

## Teardown

At ~06:08 UTC, `make teardown` (now defaults to Lab 3) deleted the Cloud Run service
`itcs355-serve` and all its revisions. After that:
- `gcloud run services list` shows 0 services.
- I never created a Vertex endpoint.
- `make cost-report` finds nothing tagged `lab=3`.

Record: `reports/lab3/teardown.txt`.

**What I kept on purpose:** the image in Artifact Registry, `itcs355-6688143@2` in the
model registry, and the model file in the bucket. These are only a few MB of storage, with
no hourly charge.

**I still need to check the billing page by hand.**
