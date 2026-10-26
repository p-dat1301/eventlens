.PHONY: sync test lint typecheck build check up infra down web crud-api rag-api rag-worker validate-fixture rag-db-create rag-migrate rag-load-fixtures rag-rebuild rag-db-test gate-g1

# Local RAG index lives in its own database on the Compose server. The legacy
# `eventlens` database holds untracked pre-ledger RAG tables and is never targeted.
COMPOSE ?= docker compose
RAG_DATABASE_NAME ?= eventlens_rag
RAG_DATABASE_URL ?= postgresql://eventlens:eventlens-local@127.0.0.1:5432/$(RAG_DATABASE_NAME)
WEB_DIR := src/frontend/web
PY_PATHS := src tests typings
RAG_WORKER_ARGS ?= enrich --max-jobs 5

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
rag-migrate rag-load-fixtures rag-rebuild: export EVENTLENS_RAG_DATABASE_URL = $(RAG_DATABASE_URL)

rag-migrate:
	uv run eventlens-rag migrate

rag-load-fixtures:
	uv run eventlens-rag load-fixtures

rag-rebuild:
	uv run eventlens-rag rebuild

rag-db-test:
	sh scripts/rag/db-test.sh

gate-g1:
	sh scripts/rag/gate-g1.sh
