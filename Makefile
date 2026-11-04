.PHONY: sync test lint typecheck build check up infra down web crud-api rag-api rag-worker validate-fixture rag-db-create rag-migrate rag-load-fixtures rag-rebuild rag-db-test rag-adopt-smoke rag-preflight rag-adopt gate-g1 enrichment-eval rag-eval web-browser-test gate-g3 search-index-test search-eval-up search-eval-down search-bench

# Local RAG index lives in its own database on the Compose server. The legacy
# `eventlens` database holds untracked pre-ledger RAG tables and is never targeted.
COMPOSE ?= docker compose
RAG_DATABASE_NAME ?= eventlens_rag
RAG_DATABASE_URL ?= postgresql://eventlens:eventlens-local@127.0.0.1:5432/$(RAG_DATABASE_NAME)
WEB_DIR := src/frontend/web
PY_PATHS := src tests typings
RAG_WORKER_ARGS ?= enrich --max-jobs 5
# T9 eval reports (deterministic JSON + measured JSON); fixture/fake only.
EVAL_OUT ?= .omo/evidence/T9
RAG_EVAL_REPEATS ?= 5

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

# Real-browser gate (T8 script, extended in T9) against the built app.
web-browser-test:
	cd $(WEB_DIR) && bun run test:e2e

gate-g3:
	sh scripts/rag/gate-g3.sh

# Search selection (BM25/semantic/hybrid/rerank). The Elasticsearch adapter suite
# needs EVENTLENS_RAG_TEST_ELASTICSEARCH_URL; any skip fails (CI job search-index).
search-index-test:
	sh scripts/rag/search-index-test.sh

# Controlled benchmark with real models (never in CI): isolated *_eval PostgreSQL
# and Elasticsearch on loopback, handoff import, then A-E. See docs/search-selection.md.
search-eval-up search-eval-down search-bench:
	sh scripts/rag/search-bench.sh $(subst search-,,$@)
