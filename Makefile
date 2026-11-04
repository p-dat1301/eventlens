.PHONY: search-bench news-openapi news-smoke rag-worker-serve sync test lint typecheck build check up infra down web crud-api rag-api rag-worker validate-fixture rag-db-create rag-migrate rag-load-fixtures rag-rebuild rag-db-test rag-adopt-smoke rag-preflight rag-adopt gate-g1 enrichment-eval rag-eval search-eval-up search-eval search-eval-down search-index-test search-index-rebuild search-index-reconcile search-index-health web-browser-test gate-g3

# Local RAG index lives in its own database on the Compose server. The legacy
# `eventlens` database holds untracked pre-ledger RAG tables and is never targeted.
COMPOSE ?= docker compose
RAG_DATABASE_NAME ?= eventlens_rag
RAG_DATABASE_URL ?= postgresql://eventlens:eventlens-local@127.0.0.1:5432/$(RAG_DATABASE_NAME)
WEB_DIR := src/frontend/web
CRUD_API_URL ?= http://127.0.0.1:8000
RAG_API_URL ?= http://127.0.0.1:8001
RAG_WORKER_SERVE_ARGS ?= --query-workers 2 --summary-workers 1
PY_PATHS := src tests typings
RAG_WORKER_ARGS ?= enrich --max-jobs 5
# T9 eval reports (deterministic JSON + measured JSON); fixture/fake only.
EVAL_OUT ?= .omo/evidence/T9
RAG_EVAL_REPEATS ?= 5
# Search platform (docs/search-evaluation.md). Benchmarks and the ES suite
# run in their own throwaway Compose project (eventlens-search-eval-<run>);
# search-eval-up/down manage the developer ES (project eventlens-search-dev).
DOCKER ?= docker
SEARCH_EVAL_REPEATS ?= 3
SEARCH_EVAL_CONFIGS ?= postgres-lexical,es-bm25,es-semantic,es-hybrid,es-hybrid-rerank
SEARCH_EVAL_EMBEDDINGS ?= fake-bge-m3
SEARCH_EVAL_RERANK ?= fake
SEARCH_EVAL_LABEL ?= search-eval
SEARCH_EVAL_DEVICE ?= cpu
SEARCH_ES_PORT ?= 9200
SEARCH_ELASTICSEARCH_URL ?= http://127.0.0.1:$(SEARCH_ES_PORT)
SEARCH_INDEX_EMBEDDINGS ?= none

sync:
	uv sync --group dev
	cd $(WEB_DIR) && bun install --frozen-lockfile

test:
	uv run pytest
	cd $(WEB_DIR) && bun test

lint:
	uv run ruff format --check $(PY_PATHS)
	uv run ruff check $(PY_PATHS)
	cd $(WEB_DIR) && bun run check

typecheck:
	uv run basedpyright
	cd $(WEB_DIR) && bun run typecheck

build:
	cd $(WEB_DIR) && bun run build

check: lint typecheck test build

up:
	docker compose up --build crud-api rag-api web

infra:
	docker compose --profile infra up -d postgres minio

down:
	docker compose --profile infra down

# Four local runtimes: web (Vite :5173), crud-api (:8000), rag-api (:8001), rag-worker.
web:
	cd $(WEB_DIR) && bun run dev

crud-api:
	uv run eventlens-crud-api

rag-api:
	uv run eventlens-rag-api

rag-worker: export EVENTLENS_RAG_DATABASE_URL = $(RAG_DATABASE_URL)
rag-worker:
	uv run eventlens-rag-worker $(RAG_WORKER_ARGS)

# News backend (docs/news-backend.md): long-running worker with separate
# pools for chat runs and summaries, OpenAPI export, end-to-end HTTP smoke.
rag-worker-serve: export EVENTLENS_RAG_DATABASE_URL = $(RAG_DATABASE_URL)
rag-worker-serve:
	uv run eventlens-rag-worker serve $(RAG_WORKER_SERVE_ARGS)

news-openapi:
	uv run python scripts/news/export_openapi.py --output docs/openapi

NEWS_SMOKE_REPORT ?= .omo/evidence/news-backend/final-gates/api-smoke.json
news-smoke:
	uv run python scripts/news/api_smoke.py --crud $(CRUD_API_URL) --rag $(RAG_API_URL) --report $(NEWS_SMOKE_REPORT) $(NEWS_SMOKE_ARGS)

validate-fixture:
	uv run eventlens-events validate fixtures/article_bundle.v1.json

rag-db-create:
	printf '%s\n' "SELECT format('CREATE DATABASE %I', '$(RAG_DATABASE_NAME)') WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '$(RAG_DATABASE_NAME)') \gexec" | $(COMPOSE) --profile infra exec -T postgres psql -X -q -v ON_ERROR_STOP=1 -U eventlens -d postgres

# Pass the URL through the recipe environment, not the shell command line, so
# characters such as & or ' in the URL are never parsed by the shell.
rag-migrate rag-preflight rag-adopt rag-load-fixtures rag-rebuild: export EVENTLENS_RAG_DATABASE_URL = $(RAG_DATABASE_URL)

# Alembic is the only migration authority. A database built by the historical
# 001-007 runner is refused until it is adopted: back it up, stop writers,
# run rag-preflight (read-only) and then rag-adopt (verified baseline stamp).
rag-migrate:
	uv run eventlens-rag migrate

rag-preflight:
	uv run eventlens-rag preflight

rag-adopt:
	uv run eventlens-rag adopt-legacy

rag-load-fixtures:
	uv run eventlens-rag load-fixtures

rag-rebuild:
	uv run eventlens-rag rebuild

# DB suite twice (fresh Alembic schema, then adopted legacy schema).
rag-db-test:
	sh scripts/rag/db-test.sh

rag-adopt-smoke:
	sh scripts/rag/with-test-db.sh sh scripts/rag/adopt-smoke.sh

gate-g1:
	sh scripts/rag/gate-g1.sh

# T9: deterministic evals on fixtures/rag with fake adapters; no DB, no network.
enrichment-eval:
	uv run eventlens-rag enrichment-eval --output $(EVAL_OUT)

rag-eval:
	uv run eventlens-rag rag-eval --output $(EVAL_OUT) --repeats $(RAG_EVAL_REPEATS)

search-eval-up search-eval-down search-eval search-index-test: export DOCKER_BIN = $(DOCKER)
search-eval-up: export EVENTLENS_SEARCH_ES_PORT = $(SEARCH_ES_PORT)
search-eval: export SEARCH_EVAL_REPEATS := $(SEARCH_EVAL_REPEATS)
search-eval: export SEARCH_EVAL_CONFIGS := $(SEARCH_EVAL_CONFIGS)
search-eval: export SEARCH_EVAL_EMBEDDINGS := $(SEARCH_EVAL_EMBEDDINGS)
search-eval: export SEARCH_EVAL_RERANK := $(SEARCH_EVAL_RERANK)
search-eval: export SEARCH_EVAL_LABEL := $(SEARCH_EVAL_LABEL)
search-eval: export SEARCH_EVAL_DEVICE := $(SEARCH_EVAL_DEVICE)

search-eval-up:
	sh scripts/rag/search-eval-up.sh

search-eval-down:
	sh scripts/rag/search-eval-down.sh

search-eval:
	sh scripts/rag/search-eval.sh

search-index-test:
	sh scripts/rag/search-index-test.sh

# Index maintenance against the developer ES and $(RAG_DATABASE_URL); URLs go
# through the environment, never the command line.
search-index-rebuild search-index-reconcile search-index-health: export EVENTLENS_RAG_DATABASE_URL = $(RAG_DATABASE_URL)
search-index-rebuild search-index-reconcile search-index-health: export EVENTLENS_RAG_ELASTICSEARCH_URL = $(SEARCH_ELASTICSEARCH_URL)
search-index-rebuild search-index-reconcile search-index-health: export EVENTLENS_RAG_SEARCH_BACKEND = elasticsearch
search-index-rebuild search-index-reconcile search-index-health: export EVENTLENS_RAG_SEARCH_EMBEDDINGS = $(SEARCH_INDEX_EMBEDDINGS)

search-index-rebuild:
	uv run eventlens-rag index rebuild

search-index-reconcile:
	uv run eventlens-rag index reconcile --deep

search-index-health:
	uv run eventlens-rag index health

# Real-browser gate (T8 script, extended in T9) against the built app.
web-browser-test:
	cd $(WEB_DIR) && bun run test:e2e

gate-g3:
	sh scripts/rag/gate-g3.sh

# PR #7 controlled benchmark; search-eval-up/down keep the isolated platform scripts.
search-bench:
	sh scripts/rag/search-bench.sh bench
