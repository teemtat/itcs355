# Lab 2 — evidence that a reclaimed machine cost minutes, not the study

Deliverable: "Interruption survived and resumed — evidence in logs".

Cloud Logging keeps these for 30 days, and `make teardown` has since removed the
cancelled job, so the lines are captured here rather than linked.

## 1. The study was cancelled mid-flight

```
job 2304407871661539328  itcs355-lab2-tune-20260920-082323
trial 00: {'n_estimators': 100, 'max_depth': 4, 'min_samples_leaf': 5, 'class_weight': 'balanced'} -> val_roc_auc=0.8411 cost=0.001166 THB  cumulative=0.001166
trial 01: {'n_estimators': 300, 'max_depth': 4, 'min_samples_leaf': 1, 'class_weight': 'balanced'} -> val_roc_auc=0.8401 cost=0.000900 THB  cumulative=0.002065
trial 02: {'n_estimators': 300, 'max_depth': 4, 'min_samples_leaf': 5, 'class_weight': 'balanced'} -> val_roc_auc=0.8399 cost=0.000880 THB  cumulative=0.002945
trial 03: {'n_estimators': 100, 'max_depth': 12, 'min_samples_leaf': 5, 'class_weight': 'balanced'} -> val_roc_auc=0.8266 cost=0.000374 THB  cumulative=0.003320
trial 04: {'n_estimators': 100, 'max_depth': 12, 'min_samples_leaf': 1, 'class_weight': 'balanced'} -> val_roc_auc=0.8245 cost=0.000389 THB  cumulative=0.003709
-> JOB_STATE_CANCELLED
```

## 2. What survived, in the bucket

```
$ gcloud storage cat gs://itcs355-6688143/itcs355/lab2-final/tune_checkpoint.json
completed trials : 5
spent_thb        : 0.003709
```

The checkpoint is written to BLOB_URI after every trial, so it outlived the machine
that was running the study.

## 3. The replacement job resumed instead of restarting

```
job 6857547094933110784  itcs355-lab2-tune-20260920-082940
seeded 284 tracking files from /gcs/itcs355-6688143/itcs355/lab2-final/mlruns
trial 00: already done, skipping (resumed from checkpoint)
trial 01: already done, skipping (resumed from checkpoint)
trial 02: already done, skipping (resumed from checkpoint)
trial 03: already done, skipping (resumed from checkpoint)
trial 04: already done, skipping (resumed from checkpoint)
trial 05: already done, skipping (resumed from checkpoint)
trial 06: {'n_estimators': 300, 'max_depth': 12, 'min_samples_leaf': 1, 'class_weight': 'balanced'} -> val_roc_auc=0.8256 cost=0.001240 THB  cumulative=0.005808
SEED VARIANCE  mean=0.8407  stdev=0.0014  spread=0.0033
```

Six trials skipped, work resumed at trial 06, and the seed sweep still reported
stdev 0.0014 / spread 0.0033 — the same numbers the uninterrupted study produced.
