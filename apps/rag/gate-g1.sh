#!/bin/sh
set -u
technical_status=0
uv sync --all-packages --group dev || technical_status=1
uv run pytest contracts/python/tests apps/ai-worker/tests apps/rag/tests -q || technical_status=1
uv run --package eventlens-worker eventlens-worker validate fixtures/article_bundle.v1.json || technical_status=1
uv run --package eventlens-worker eventlens-worker process fixtures/article_bundle.v1.json || technical_status=1
sh apps/rag/db-test.sh || technical_status=1
printf 'Technical checks exit status: %s\nG1 OPEN; worker rename BLOCKED (owner approval required).\n' "$technical_status"
exit 2
