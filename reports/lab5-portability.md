# Lab 5 Task 4 — The portability test

Primary: GCP (Cloud Storage, Cloud Run, Vertex). Second provider: **Azure**, with only
`upload`, `download` and `invoke` in [cloudlayer/azure.py](../cloudlayer/azure.py).

## Write-up

**Hardest method: `upload`.** `invoke` took twenty minutes because Container Apps, like
Cloud Run, forwards the request to the container unchanged, so the GCP version ported
almost line for line. `upload` was the hard one because writes need auth, and the serving
image must not ship an SDK. That meant the Blob REST API with three credential sources:
Shared Key for the emulator, the Container Apps identity endpoint inside the app, and
`az account get-access-token` on the laptop. My first signed PUT came back
`403 AuthorizationFailure`. urllib had quietly added a form-encoded `Content-Type` after
I signed the request. On GCP, `gcloud storage cp` had hidden all of this from me.

**The leak I could not hide: who is allowed to call.** Cloud Run is private by default,
and `GcpAdapter.invoke` attaches an identity token. Container Apps' external ingress
accepts anyone. `invoke(endpoint, payload)` has no parameter that means "the caller's
identity", and making the app private (Easy Auth, or internal ingress plus a VNet) is a
deployment decision, not an adapter one. So the Azure `invoke` sends no credential at all,
and the two adapters have different security properties behind the same method. The same
leak shows up on the data plane. As GCP project owner I could read my bucket. As Azure
subscription owner I can read nothing until I give myself `Storage Blob Data Contributor`.
Two smaller leaks: model URIs in the GCP registry are `gs://` URIs, so a model registered
on GCP has to be re-uploaded before Azure can serve it. And the original swap check reused
the primary's `BLOB_URI`, which handed `gs://…` to the Azure adapter. That is the "hidden
in config" failure mode, and the import scan could never have caught it.

**Full migration: about 25 engineering days.** These three methods took about a day, and
they were the easy three. Still to do: training jobs and registry on Azure ML (4.5 days);
`deploy`/`set_traffic` on Container Apps revisions (2); `emit_metric`/`recent_inputs` on
Azure Monitor and KQL (3); `generate` (1.5); `teardown`, `push_image` and
`download_prefix`/`mount_path` (2.5); the pipeline (3); CI federation and the three scoped
identities (2.5); dashboards and alerts, now in `infra/gcp/` (2); re-verified price
tables (1); and a load test, canary and drift rehearsal on the new platform (3).

**Verdict: half yes, half no.** The Layer 1 discipline was worth it. It was cheap, it
keeps `src/` testable offline, and the extended audit found only a region name in a
docstring. As insurance against moving off GCP, the eleven-method seam was **not** worth
it for this system. The seam covers the cheap 20% of a migration. The expensive 80%
(identity, IaC, observability, pipeline semantics) lives in `infra/` and in provider
behaviour that no method signature can see. For one student on one cloud with credits,
the better engineering choice is to accept lock-in on purpose and keep a clean Layer 1.

## Evidence

**Part A: audit.** The stock audit passed at `0747212`. I extended
[scripts/portability_audit.py](../scripts/portability_audit.py) to cover more SDK and
hostname patterns, CLI invocations, metadata endpoints and the `/gcs/` mount. It also
checks that every cloud.env value arrives *through* cloud.env: there must be no literal
project, bucket, region, registry or identity in code, every slot must be set, and the
`BLOB_URI` scheme must match `CLOUD_PROVIDER`. Before: two hits, `asia-southeast1`
hard-coded in `src/costs.py`. That marked a real hidden assumption: the price table is
valid for one region only. The fix rewords the docstring to tie the table to `REGION` and
says plainly that a different region needs its own rows. No behaviour changed, and
`make test` gives 50 passed with 4 skipped (the skips need a live `SERVICE_URL`). After:
`PASSED … no literal copy of the 7 cloud.env values; all 7 slots resolve through
cloud.env (gcp)`. Full output is in
[audit-before.txt](lab5/portability/audit-before.txt) and
[audit-after.txt](lab5/portability/audit-after.txt).

The price tables in `src/costs.py` and `src/llmcost.py` are still keyed by provider
*name*. That is data selected by `cfg.provider`, not a provider call. I left it there.

**Part B: swap.** Why Azure and not AWS: Azure for Students needs only a university email
and no card, and it comes with USD 100 of credit that simply stops when used up, so it
cannot overrun the 800 THB budget. Azurite is Microsoft's own blob emulator and really
checks Shared Key signatures (it rejected my bad one). Container Apps runs the Cloud Run
image unchanged. A SageMaker endpoint would need `/ping` and `/invocations` instead.
[swap-check-before.txt](lab5/portability/swap-check-before.txt) shows the stub output,
then the config leak. Now `make swap-check SECOND=azure` reads `AZURE_*` slots from
cloud.env, and it refuses to run if `BLOB_URI` or `PROJECT_ID` would be inherited from
GCP.

Emulator run (`bash infra/azure/emulator-demo.sh`, no account, 0 THB): the serving image
loaded its model from Azurite *through `AzureAdapter.download`*, then:

```
  [PASS            ] upload    http://127.0.0.1:10000/devstoreaccount1/itcs355/itcs355/portability/probe.txt
  [PASS            ] download  26 bytes, identical
  [PASS            ] invoke    p=0.039 model_version=1 110 ms
```

Full log: [swap-check-emulator.txt](lab5/portability/swap-check-emulator.txt). The
emulator cannot show managed-identity auth, Container Apps ingress or real latency.

**Real run: pending.** It needs an Azure for Students account, which I have to create.
After that, `bash infra/azure/lab5-swap.sh demo` creates everything, runs
`make swap-check SECOND=azure` into `reports/lab5/portability/swap-check-azure.txt`, and
deletes the resource group, even if a step fails. Expect about 30 minutes and about
6 THB (one day of registry Basic) on the student credit. Cleanup if interrupted:
`bash infra/azure/lab5-swap.sh down`. The script has not yet been run against Azure.
