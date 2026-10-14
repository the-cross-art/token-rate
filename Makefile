.DEFAULT_GOAL := help
SHELL := /bin/bash
VENV := .venv
PY := $(VENV)/bin/python

help: ## show this help
	@grep -hE '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/' | \
		awk -F'\t' '{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

bootstrap: ## one-command setup on a fresh machine (deps + models)
	./scripts/bootstrap.sh

setup: ## create the venv and install dependencies only
	uv venv $(VENV) --python 3.12
	uv pip install --python $(PY) -e .
	@test -f .env || cp .env.example .env
	@echo "setup complete"

models: ## download the GGUFs for the active hardware profile
	./scripts/fetch_models.sh

profile: ## show the active hardware profile
	@$(PY) scripts/profile.py

monitoring: ## start Prometheus + Grafana natively (~420MB, recommended)
	./scripts/monitoring.sh start

monitoring-docker: ## start Prometheus + Grafana under Docker (~2GB)
	PROM_URL=http://prometheus:9090 docker compose -f monitoring/docker-compose.yml up -d
	@echo "grafana  http://127.0.0.1:3001/d/token-rate-main"

monitoring-down: ## stop the monitoring stack (either mode)
	-./scripts/monitoring.sh stop
	-docker compose -f monitoring/docker-compose.yml down 2>/dev/null

proxy: ## run the metering proxy on :8080
	$(PY) -m uvicorn proxy.main:app --host 127.0.0.1 --port 8080

spec: ## serve the draft-model speculative arm on :8081
	./scripts/serve.sh spec
ngram: ## serve the n-gram speculative arm on :8082
	./scripts/serve.sh ngram
plain: ## serve the unspeculated control arm on :8083
	./scripts/serve.sh plain
ollama: ## serve the ollama control arm on :11434
	OLLAMA_KEEP_ALIVE=10m ollama serve
stop: ## stop all llama.cpp arms
	./scripts/serve.sh stop
status: ## show the profile and which ports are up
	./scripts/serve.sh status

load: ## find the batch ceiling (ramps concurrency 1->32)
	$(PY) bench/load.py --sweep

load-at: ## hold one concurrency level: make load-at N=32
	$(PY) bench/load.py --concurrency $(or $(N),8) --duration 60

bench: ## A/B every decoding arm over the standard prompt set
	$(PY) bench/ab_compare.py

stripe-setup: ## create Stripe test-mode meter, price and per-agent customers
	$(PY) scripts/stripe_bootstrap.py

stripe-usage: ## show accrued billable usage per agent (from Stripe)
	$(PY) scripts/stripe_bootstrap.py --invoice

usage: ## print the local ledger summary
	@curl -s http://127.0.0.1:8080/usage | $(PY) -m json.tool

down: ## stop EVERYTHING this project starts
	-./scripts/serve.sh stop
	-./scripts/monitoring.sh stop
	-docker compose -f monitoring/docker-compose.yml down 2>/dev/null
	-pkill -f "uvicorn proxy.main" 2>/dev/null
	-pkill -f "ollama serve" 2>/dev/null
	@echo "all stopped"

.PHONY: help bootstrap setup models profile monitoring monitoring-docker \
        monitoring-down proxy spec ngram plain ollama stop status load load-at bench \
        stripe-setup stripe-usage usage down
