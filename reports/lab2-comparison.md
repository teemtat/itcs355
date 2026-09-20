# Lab 2 — Run comparison

Experiment `itcs355-lab2` · 16 grid trials · 5 seed-sweep runs

`thb_per_point` is cost per percentage point of val_roc_auc above the worst trial. Cheap improvements rank low; expensive improvements rank high, however good the headline number is.

| run_id   | n_estimators | max_depth | min_samples_leaf | class_weight | val_roc_auc | val_pr_auc | test_roc_auc | fit_s | fit_thb  | thb_per_point |
|----------|--------------|-----------|------------------|--------------|-------------|------------|--------------|-------|----------|---------------|
| 3895bd37 | 100          | 4         | 5                | None         | 0.8426      | 0.3932     | 0.8533       | 0.35  | 0.000301 | 0.00017       |
| d4f0cada | 100          | 4         | 1                | None         | 0.8424      | 0.3938     | 0.8518       | 0.34  | 0.00029  | 0.00016       |
| f82d86e6 | 300          | 4         | 5                | None         | 0.8411      | 0.3887     | 0.8545       | 1.03  | 0.000874 | 0.00053       |
| 80b26009 | 100          | 4         | 5                | balanced     | 0.8411      | 0.3919     | 0.851        | 1.37  | 0.001166 | 0.0007        |
| 98828bc0 | 300          | 4         | 1                | None         | 0.8404      | 0.3901     | 0.8537       | 1.01  | 0.000859 | 0.00054       |
| 716e3f0c | 100          | 4         | 1                | balanced     | 0.8403      | 0.3889     | 0.8478       | 0.35  | 0.000299 | 0.00019       |
| 37643239 | 300          | 4         | 1                | balanced     | 0.8401      | 0.3892     | 0.8507       | 1.06  | 0.0009   | 0.00058       |
| 92e12e82 | 300          | 4         | 5                | balanced     | 0.8399      | 0.3896     | 0.8519       | 1.03  | 0.00088  | 0.00057       |
| 8f105421 | 300          | 12        | 5                | None         | 0.8354      | 0.3791     | 0.8431       | 1.33  | 0.001132 | 0.00103       |
| 115a0b3b | 100          | 12        | 5                | None         | 0.8322      | 0.3794     | 0.8417       | 0.44  | 0.000373 | 0.00048       |
| fa9fe887 | 300          | 12        | 5                | balanced     | 0.8286      | 0.3892     | 0.8443       | 1.31  | 0.001111 | 0.00268       |
| f96cfb51 | 100          | 12        | 1                | None         | 0.8268      | 0.3898     | 0.8415       | 0.53  | 0.000451 | 0.00192       |
| 35d8ff0e | 100          | 12        | 5                | balanced     | 0.8266      | 0.3927     | 0.8452       | 0.44  | 0.000374 | 0.00174       |
| 6fdbb7ec | 300          | 12        | 1                | None         | 0.8265      | 0.3857     | 0.8374       | 1.41  | 0.0012   | 0.00587       |
| cdbe8b59 | 300          | 12        | 1                | balanced     | 0.8256      | 0.396      | 0.8399       | 1.46  | 0.00124  | 0.01083       |
| 78622c45 | 100          | 12        | 1                | balanced     | 0.8245      | 0.386      | 0.834        | 0.46  | 0.000389 | 0.08615       |

## Seed variance for the best validation configuration

The split is held at the study seed; only the model's own randomness moves, so
this is the configuration's variance and not the split's.

| run_id   | seed | val_roc_auc | test_roc_auc |
|----------|------|-------------|--------------|
| 0fa2525c | 1    | 0.8426      | 0.8537       |
| 419798ba | 2    | 0.8396      | 0.8533       |
| 93234bc9 | 3    | 0.8405      | 0.8526       |
| 00239a6f | 4    | 0.8393      | 0.8532       |
| fa9b4de3 | 5    | 0.8416      | 0.8541       |

**mean 0.8407 · stdev 0.0014 · spread 0.0033**

The gap between the top two grid rows is 0.0002. That gap is smaller than the spread above, so the top rows are not distinguishable on score and something else has to decide.

## Cost: measured two ways, and they disagree

| | THB |
|---|---|
| Sum of per-trial fit time, as the study logged it | 0.0118 |
| Machine time actually billed (53.3 min at 3.06 THB/h) | 2.7166 |

A factor of about 229. The study times `model.fit`; the
provider charges for the machine from the moment it boots — image pull, data
load, artifact writes, and every second the process spends doing something that
is not training. Quote the second number in anything anyone pays for.

## Price provenance

Rates from `src/costs.py`: n1-standard-4 at 3.06 THB/h (spot), verified 2026-09-20 against the Vertex AI
pricing page with the region set to Singapore (asia-southeast1) and against the
Cloud Billing Catalog API. USD/THB 33.36. See the module docstring for the
derivation, including why spot is 34% of on-demand here and not 30%.

## Which model did you register, and why?

**I registered run `3895bd37` — 100 trees, `max_depth=4`, `min_samples_leaf=5`, no class
weighting. It went into the registry as `itcs355-6688143` version 1.**

It's top of the table on validation (0.8426), but it only beats second place by 0.0002.
When I reran that same config at 5 different seeds the score moved by 0.0033 — 16 times
bigger than the gap. So the top of the table is just noise. The six configs at
`max_depth=4` are basically tied and I can't pick between them on score.

What I can pick on is cost. This one trains in 0.35s for 0.0003 THB. The same config with
300 trees costs 2.9x more and scores *lower* on validation. Paying triple for a worse
number is an easy no.

Two configs beat it on test (0.8545 vs 0.8533). I didn't pick on test on purpose. If I
choose using the test set then I've fitted the test set, just slowly, and I've got no
honest number left to report.

Training costs 0.05 THB a run, so retraining weekly is 0.22 THB a month.

**Where I could be wrong:** `max_depth=4` might be too shallow and missing something that
only shows up with more machines. I've only got one split of 240 machines. A different
split could flip the depth results around completely.
