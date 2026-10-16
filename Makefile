.PHONY: sync test lint typecheck build check up infra down validate-fixture rag-db-create rag-migrate rag-load-fixtures rag-rebuild rag-db-test gate-g1

# Local RAG index lives in its own database on the Compose server. The legacy
# `eventlens` database holds untracked pre-ledger RAG tables and is never targeted.
COMPOSE ?= docker compose
RAG_DATABASE_NAME ?= eventlens_rag
RAG_DATABASE_URL ?= postgresql://eventlens:eventlens-local@127.0.0.1:5432/$(RAG_DATABASE_NAME)

sync:
	uv sync --all-packages --group dev
	cd apps/web && bun install --frozen-lockfile

test:
	uv run pytest
	cd apps/web && bun test

lint:
	uv run ruff format --check .
	uv run ruff check .
	cd apps/web && bun run check

typecheck:
	uv run basedpyright
	cd apps/web && bun run typecheck

build:
	cd apps/web && bun run build

check: lint typecheck test build

up:
	docker compose up --build api web

infra:
	docker compose --profile infra up -d postgres minio

down:
	docker compose --profile infra down

validate-fixture:
	uv run --package eventlens-worker eventlens-worker validate fixtures/article_bundle.v1.json

rag-db-create:
	printf '%s\n' "SELECT format('CREATE DATABASE %I', '$(RAG_DATABASE_NAME)') WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '$(RAG_DATABASE_NAME)') \gexec" | $(COMPOSE) --profile infra exec -T postgres psql -X -q -v ON_ERROR_STOP=1 -U eventlens -d postgres

# Pass the URL through the recipe environment, not the shell command line, so
# characters such as & or ' in the URL are never parsed by the shell.
rag-migrate rag-load-fixtures rag-rebuild: export EVENTLENS_RAG_DATABASE_URL = $(RAG_DATABASE_URL)

rag-migrate:
	uv run --package eventlens-rag eventlens-rag migrate

rag-load-fixtures:
	uv run --package eventlens-rag eventlens-rag load-fixtures

rag-rebuild:
	uv run --package eventlens-rag eventlens-rag rebuild

rag-db-test:
	sh apps/rag/db-test.sh

gate-g1:
	sh apps/rag/gate-g1.sh
