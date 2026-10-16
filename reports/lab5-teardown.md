# Lab 5 — Teardown (Task 6)

Run on 2026-10-16 (UTC). The raw output is in [`reports/lab5/teardown.txt`](lab5/teardown.txt).

## What was deleted, in order

1. **The Vertex pipeline schedules go first.** A schedule left behind keeps starting pipeline
   runs. That means spot training, a staging deploy and the pipeline fee, about 2.5 THB a run,
   every Monday.
   - `itcs355-lab5-retrain` was ACTIVE, with its next fire due on 2026-10-18 19:00 UTC.
   - `itcs355-lab5-fire-once` had already COMPLETED.

   `make teardown` cannot see either of these. The adapter deletes *Cloud Scheduler* jobs, and
   Vertex pipeline schedules are a different service that carries no labels. I deleted both
   through the Vertex REST API (`DELETE …/schedules/{id}`). `python -m cloudlayer.pipelines
   teardown` does the same thing when the Vertex SDK is installed.
2. **`make teardown LAB=5`.** This deleted the Cloud Run service `itcs355-staging` and the 4
   FAILED custom-job records.
3. **`bash infra/gcp/iam-lab5.sh down`.** This removed the 12 bindings and deleted the three
   service accounts: `itcs355-lab5-train`, `-serve` and `-ci`. They were deleted last, because
   steps 1 and 2 still ran as these identities.

Not touched: the capstone's Cloud Run service `scamshield` (label `lab=capstone`, still
returning 200), `scamshield-deployer`, `gh-deployer` and the default compute service account.

## Checked by hand afterwards

| Resource | Left |
|---|---|
| Vertex pipeline schedules | 0 |
| Running pipeline jobs | 0 |
| Running, pending or queued custom jobs | 0 |
| Vertex endpoints | 0 |
| Cloud Run services | only `scamshield` (capstone) |
| Cloud Run jobs / Cloud Scheduler jobs | 0 / 0 |
| Compute instances | 0 |
| `itcs355-lab5-*` service accounts | 0 |
| SUCCEEDED custom-job records with `lab=5` | **17, kept on purpose (see below)** |

## Why `make cost-report LAB=5` still reports 17 items

`make cost-report` prints `17 resource(s) deleted or still deleting` and exits 1. None of
the 17 is running, and none of them costs anything.

- **Where the count comes from.** `scripts/teardown_verify.py` calls the adapter's
  `teardown()` and counts *every* line it returns. That includes the lines that say
  `kept … (SUCCEEDED — lineage)`. All 17 lines are of that kind: the records of custom jobs
  that finished successfully. They are the pipeline's steps (ingest, validate, train,
  evaluate, register, deploy) across the five runs.
- **Why they cost nothing.** A finished Vertex custom job is a metadata record. Its compute
  was released when the job ended, and it was billed only for the minutes it ran
  ([lab5-pipeline.md §5](lab5-pipeline.md)). Vertex charges nothing to keep the record.
- **Why I kept them.** They are one end of the lineage. The registered model version
  `itcs355-6688143@3` (the production version, `gate=pass`) carries the label `pipeline_run`,
  and the job records are where that reference resolves. If the job records were deleted, the question "which job trained
  this model, on what data, from which image" would point at nothing. The adapter keeps
  SUCCEEDED jobs by default for exactly this reason, and only `PURGE=True` removes them.
- **The trade-off.** I accept a red `cost-report` and explain it here, rather than buying a
  green one by destroying the audit trail. The check is meant to catch things that *bill*.
  These do not, and the table above shows that nothing that bills is left.

To remove them anyway (this breaks the model's lineage): `make teardown LAB=5 PURGE=True`.

## Still to do

- [ ] Re-run `make cost-report LAB=5` after 24 hours, since deletion is asynchronous. Expect
      the same 17 kept records and nothing else.
- [ ] Check the console by hand and take the screenshot of the empty resource list: Vertex
      pipelines, schedules and endpoints, Cloud Run, Cloud Scheduler, and Compute Engine.
- [ ] Read the actual Lab 5 cost from the billing page, filtered by label `lab=5`.
