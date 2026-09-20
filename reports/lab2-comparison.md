# Lab 2 — Run comparison

Experiment `itcs355-lab2` · 16 grid trials · 5 seed-sweep runs

`thb_per_point` is cost per percentage point of val_roc_auc above the worst trial. Cheap improvements rank low; expensive improvements rank high, however good the headline number is.

| run_id   | n_estimators | max_depth | min_samples_leaf | class_weight | val_roc_auc | val_pr_auc | test_roc_auc | fit_s | fit_thb  | thb_per_point |
|----------|--------------|-----------|------------------|--------------|-------------|------------|--------------|-------|----------|---------------|
| 3436d36d | 100          | 4         | 5                | None         | 0.8426      | 0.3932     | 0.8533       | 0.35  | 0.000301 | 0.00017       |
| 2883116f | 100          | 4         | 1                | None         | 0.8424      | 0.3938     | 0.8518       | 0.37  | 0.000317 | 0.00018       |
| 6a47d97c | 300          | 4         | 5                | None         | 0.8411      | 0.3887     | 0.8545       | 1.0   | 0.000852 | 0.00051       |
| bb5ea93c | 100          | 4         | 5                | balanced     | 0.8411      | 0.3919     | 0.851        | 1.08  | 0.000918 | 0.00055       |
| cea32f69 | 300          | 4         | 1                | None         | 0.8404      | 0.3901     | 0.8537       | 1.05  | 0.000892 | 0.00056       |
| a0b1df96 | 100          | 4         | 1                | balanced     | 0.8403      | 0.3889     | 0.8478       | 0.36  | 0.000303 | 0.00019       |
| 98cc6623 | 300          | 4         | 1                | balanced     | 0.8401      | 0.3892     | 0.8507       | 1.04  | 0.000881 | 0.00056       |
| 5c875aa8 | 300          | 4         | 5                | balanced     | 0.8399      | 0.3896     | 0.8519       | 1.06  | 0.000905 | 0.00059       |
| 71ed0490 | 300          | 12        | 5                | None         | 0.8354      | 0.3791     | 0.8431       | 1.38  | 0.00117  | 0.00107       |
| 6c0cfa8c | 100          | 12        | 5                | None         | 0.8322      | 0.3794     | 0.8417       | 0.48  | 0.000404 | 0.00052       |
| 305f2508 | 300          | 12        | 5                | balanced     | 0.8286      | 0.3892     | 0.8443       | 1.27  | 0.001082 | 0.00261       |
| 93f3d78d | 100          | 12        | 1                | None         | 0.8268      | 0.3898     | 0.8415       | 0.49  | 0.000417 | 0.00178       |
| 30a3a93f | 100          | 12        | 5                | balanced     | 0.8266      | 0.3927     | 0.8452       | 0.47  | 0.000401 | 0.00187       |
| 6e7b31b8 | 300          | 12        | 1                | None         | 0.8265      | 0.3857     | 0.8374       | 1.48  | 0.001255 | 0.00614       |
| 9242888b | 300          | 12        | 1                | balanced     | 0.8256      | 0.396      | 0.8399       | 1.35  | 0.001146 | 0.01001       |
| 2ec5e052 | 100          | 12        | 1                | balanced     | 0.8245      | 0.386      | 0.834        | 0.48  | 0.000408 | 0.09035       |

## Seed variance for the best validation configuration

The split is held at the study seed; only the model's own randomness moves, so
this is the configuration's variance and not the split's.

| run_id   | seed | val_roc_auc | test_roc_auc |
|----------|------|-------------|--------------|
| a58e7a48 | 1    | 0.8426      | 0.8537       |
| 5cc62498 | 2    | 0.8396      | 0.8533       |
| 8a049bbb | 3    | 0.8405      | 0.8526       |
| 537d057f | 4    | 0.8393      | 0.8532       |
| e0499755 | 5    | 0.8416      | 0.8541       |

**mean 0.8407 · stdev 0.0014 · spread 0.0033**

The gap between the top two grid rows is 0.0002. That gap is smaller than the spread above, so the top rows are not distinguishable on score and something else has to decide.

## Cost: measured two ways, and they disagree

| | THB |
|---|---|
| Sum of per-trial fit time, as the study logged it | 0.0117 |
| Machine time actually billed (44.7 min at 3.06 THB/h) | 2.2806 |

A factor of about 196. The study times `model.fit`; the
provider charges for the machine from the moment it boots — image pull, data
load, artifact writes, and every second the process spends doing something that
is not training. Quote the second number in anything anyone pays for.

## Price provenance

Rates from `src/costs.py`: n1-standard-4 at 3.06 THB/h (spot), verified 2026-09-20 against the Vertex AI
pricing page with the region set to Singapore (asia-southeast1) and against the
Cloud Billing Catalog API. USD/THB 33.36. See the module docstring for the
derivation, including why spot is 34% of on-demand here and not 30%.

## Which model did you register, and why?

TODO(Lab 2): 200 words maximum. Must address all four:

1. Why this model rather than the highest-scoring one, if they differ
2. The variance across seeds for your chosen configuration
3. What it costs to train, and to retrain monthly
4. One way this choice could be wrong

An answer that only says "highest validation score" scores zero on this task.