# ITCS355 Lab 1
# `make reproduce` is the one command a grader runs. Keep it working.

SHELL := /bin/bash
IMAGE ?= itcs355-lab1
TAG   ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo dev)
PLATFORM ?= linux/amd64
SEED ?= 20260101

# Lab 2 defaults. MACHINE is priced in src/costs.py — change both together.
# cloud.env is the source of truth for these, and Make does not read it.
MODEL_REGISTRY_NAME ?= $(shell sed -n "s/^MODEL_REGISTRY_NAME=//p" cloud.env)
# MLflow 3 refuses a file-store backend unless asked; the study's runs are a file store.
FILESTORE = MLFLOW_ALLOW_FILE_STORE=true

MACHINE ?= n1-standard-4
TRIALS  ?= 16
BUDGET  ?= 150
SWEEP   ?= 5
STUDY       ?= lab2-final
STUDY_LOCAL ?= reports/remote-mlruns-final

.PHONY: help setup cloud-check data test portability-audit train image image-push reproduce study verify clean teardown \
        tune train-remote tune-remote pull-runs compare register reload-check serve serve-image loadtest drift \
        inject-drift pipeline cost swap-check llm-eval llm-gate

help:
	@grep -E "^[a-zA-Z_-]+:.*?## .*$$" $(MAKEFILE_LIST) | awk -F":.*?## " "{printf \"  %-20s %s\\n\", \$$1, \$$2}"

setup: ## Install dependencies and print environment status
	python -m pip install --upgrade pip
	pip install -r requirements.txt
	@echo "environment ok"

cloud-check: ## Resolve the eight capability slots
	python scripts/cloud_check.py

data: ## Generate the default dataset (deterministic)
	python scripts/make_dataset.py --seed $(SEED)

test: ## Run data contract and split property tests
	pytest -q tests/

portability-audit: ## Fail if provider strings leak into src/
	python scripts/portability_audit.py

train: ## Train locally, outside the container
	python -m src.train --seed $(SEED) --metrics-out reports/metrics.json

image: ## Build the training image for linux/amd64
	docker buildx build --platform $(PLATFORM) \
	  --build-arg GIT_COMMIT="$$(git rev-parse HEAD 2>/dev/null || echo unknown)" \
	  -t $(IMAGE):$(TAG) --load .

image-push: image ## Push to CONTAINER_REGISTRY via your adapter
	python -c "from src import config; from cloudlayer.factory import get_adapter; \
	print(get_adapter(config.load()).push_image(\"$(IMAGE):$(TAG)\"))"

# Run as the invoking user, not the image's uid 10001: on Linux a bind mount keeps host
# ownership, so uid 10001 cannot write into the grader's reports/ or data/. The image
# still defaults to non-root. That uid has no passwd entry, so give it HOME and USER.
DOCKER_RUN = docker run --rm --platform $(PLATFORM) --user "$$(id -u):$$(id -g)" -e HOME=/tmp -e USER=reproducer

reproduce: image ## THE ONE COMMAND. Grader runs this. Needs Docker only.
	@mkdir -p data reports
	# 1. Data, generated inside the pinned image so the host's Python and NumPy play no part.
	$(DOCKER_RUN) -v "$$PWD/data:/app/data" --entrypoint python \
	  $(IMAGE):$(TAG) scripts/make_dataset.py --seed $(SEED) --out /app/data/raw/sensors.csv
	# 2. Train. Working dir is reports/ so MLflow's ./mlruns artifact store lands on the mount.
	$(DOCKER_RUN) -w /app/reports \
	  -v "$$PWD/data:/app/data:ro" \
	  -v "$$PWD/reports:/app/reports" \
	  -e MLFLOW_TRACKING_URI=sqlite:////app/reports/mlflow.db \
	  -e GIT_COMMIT="$$(git rev-parse HEAD 2>/dev/null || echo unknown)" \
	  $(IMAGE):$(TAG) --seed $(SEED) --metrics-out /app/reports/metrics.json

study: ## Lab 1 Task 5: max_depth sweep at the fixed seed, then the seed spread of the default config
	@for d in 3 5 8 12 16; do \
	  python -m src.train --seed $(SEED) --max-depth $$d --run-name depth-$$d || exit 1; \
	done
	@for s in 1 2 3 4 5; do \
	  python -m src.train --seed $$s --run-name seed-$$s || exit 1; \
	done

verify: ## Check the produced metric against the README claim
	python scripts/verify_metric.py

teardown: ## Delete every resource tagged course=itcs355 for this lab
	python -c "from src import config; from cloudlayer.factory import get_adapter; \
	cfg=config.load(); print(get_adapter(cfg).teardown(cfg.tags(1)))"

clean: ## Remove local artifacts
	rm -rf mlruns mlartifacts mlflow.db reports/metrics.json reports/mlflow.db reports/mlruns .pytest_cache

# --- Lab 2 -------------------------------------------------------------------
tune: ## Budgeted hyperparameter study (>=12 trials), locally
	python -m src.tune --trials $(TRIALS) --budget-thb $(BUDGET)

train-remote: ## Task 1: one training run as a managed job, not on this laptop
	python scripts/train_remote.py --machine-type $(MACHINE) $(REMOTE_ARGS)

tune-remote: ## Task 2: the budgeted study as a managed SPOT job, checkpointed to the bucket
	python scripts/train_remote.py --module src.tune --machine-type $(MACHINE) $(REMOTE_ARGS) \
	  --extra --trials $(TRIALS) --budget-thb $(BUDGET) --seed-sweep $(SWEEP)

pull-runs: ## Copy the runs the job tracked in the bucket down for comparison
	$(FILESTORE) python scripts/pull_runs.py --key $(STUDY)/mlruns --out $(STUDY_LOCAL)

compare: ## Rank runs by metric and by cost per point
	$(FILESTORE) MLFLOW_TRACKING_URI="file://$$PWD/$(STUDY_LOCAL)" \
	  python scripts/compare_runs.py --experiment itcs355-lab2

register: ## Task 4: register RUN with lineage, then promote it
	$(FILESTORE) python scripts/register.py --run $(RUN) --study-mlruns $(STUDY_LOCAL)

reload-check: ## Load the registered model by version and score rows
	$(FILESTORE) python scripts/reload_check.py --name $(MODEL_REGISTRY_NAME) --version $(VERSION)

# --- Lab 3 -------------------------------------------------------------------
serve: ## Run the inference service locally on :8080
	python scripts/export_model.py --out reports/model.joblib
	MODEL_PATH=reports/model.joblib MODEL_VERSION=local uvicorn service.app:app --port 8080

serve-image: ## Build the serving image
	docker buildx build --platform $(PLATFORM) -f service/Dockerfile.serve -t itcs355-serve:$(TAG) --load .

loadtest: ## Load test at three concurrency levels
	@for vus in 1 10 50; do \
	  echo "=== $$vus VUs ==="; \
	  k6 run -e TARGET=$(TARGET) -e VUS=$$vus loadtest/k6.js || true; \
	done

# --- Lab 4 -------------------------------------------------------------------
inject-drift: ## Shift a feature's distribution on purpose
	python scripts/inject_drift.py --feature temp_c --mode shift --magnitude 6

drift: ## Score drift against the reference window
	python -m monitoring.drift --current data/current.csv

# --- Lab 5 -------------------------------------------------------------------
pipeline: ## Compile pipeline/pipeline.yaml for your provider
	python -c "from cloudlayer.pipelines import compile_for; from src import config; \
	compile_for(config.load().provider)"

llm-eval: ## Run the LLM golden set against recorded responses (offline, free)
	python scripts/llm_eval.py --out reports/llm_eval-baseline.json

llm-gate: ## Prove the gate fails on a degraded set — expected to exit non-zero
	python scripts/llm_eval.py --out reports/llm_eval-baseline.json >/dev/null
	python scripts/llm_eval.py --responses evals/fixtures/triage-regressed.jsonl \
	  --out reports/llm_eval.json --baseline reports/llm_eval-baseline.json

cost: ## Build the cost report
	python scripts/cost_report.py --estimate $(EST) --actual $(ACT) --rps $(RPS) --instance $(INSTANCE)

swap-check: ## Prove the portability seam against a second provider
	python scripts/portability_swap_check.py --second-provider $(SECOND)
