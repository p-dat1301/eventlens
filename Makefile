.PHONY: sync test lint typecheck build check up infra down validate-fixture

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
