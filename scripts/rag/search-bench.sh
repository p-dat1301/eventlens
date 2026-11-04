#!/bin/sh
# Controlled search-selection benchmark (real models, never CI).
#   up    start loopback eval PostgreSQL/pgvector and Elasticsearch containers
#   down  remove them (and their data)
#   bench import the handoff batch into rag_search_eval and run A-E
# Inputs (env): SEARCH_ARTICLES, SEARCH_MANIFEST (read-only handoff files),
# SEARCH_QUESTIONS (search-questions.v1), SEARCH_OUTPUT (private report dir),
# SEARCH_DEVICE (cpu|mps|cuda), SEARCH_REPEATS. Models must already be in the
# local Hugging Face cache; nothing is downloaded here.
set -eu
PG=eventlens-search-eval-pg
ES=eventlens-search-eval-es
PG_IMAGE=pgvector/pgvector:0.8.0-pg17
ES_IMAGE=docker.elastic.co/elasticsearch/elasticsearch:9.2.0
URL=postgresql://rag_eval:rag-eval-only@127.0.0.1:55432/rag_search_eval
case "${1:-}" in
up)
    docker run -d --name "$PG" -e POSTGRES_USER=rag_eval \
        -e POSTGRES_PASSWORD=rag-eval-only -e POSTGRES_DB=rag_search_eval \
        -p 127.0.0.1:55432:5432 "$PG_IMAGE" >/dev/null
    docker run -d --name "$ES" -e discovery.type=single-node \
        -e xpack.security.enabled=false -e ES_JAVA_OPTS="-Xms1g -Xmx1g" \
        -p 127.0.0.1:59200:9200 "$ES_IMAGE" >/dev/null
    for _ in $(seq 60); do
        if curl -fsS http://127.0.0.1:59200/_cluster/health >/dev/null 2>&1 \
            && docker exec "$PG" pg_isready -U rag_eval >/dev/null 2>&1; then
            echo "search-eval: up ($URL, http://127.0.0.1:59200)"
            exit 0
        fi
        sleep 2
    done
    echo "search-eval: containers did not become ready" >&2
    exit 1
    ;;
down)
    docker rm -f "$PG" "$ES" >/dev/null 2>&1 || true
    echo "search-eval: removed"
    ;;
bench)
    : "${SEARCH_ARTICLES:?} ${SEARCH_MANIFEST:?} ${SEARCH_QUESTIONS:?}"
    out=${SEARCH_OUTPUT:-.private/rag-search-bench/reports/run-$(date +%Y%m%dT%H%M%S)}
    export EVENTLENS_RAG_EVAL_DATABASE_URL=$URL TOKENIZERS_PARALLELISM=false
    uv run --group bench eventlens-rag handoff-validate \
        --articles "$SEARCH_ARTICLES" --manifest "$SEARCH_MANIFEST" \
        --output "$out.handoff.json"
    uv run --group bench eventlens-rag handoff-import \
        --articles "$SEARCH_ARTICLES" --manifest "$SEARCH_MANIFEST"
    uv run --group bench eventlens-rag search-bench \
        --questions "$SEARCH_QUESTIONS" --output-dir "$out" \
        --es-url http://127.0.0.1:59200 --device "${SEARCH_DEVICE:-cpu}" \
        --repeats "${SEARCH_REPEATS:-3}"
    ;;
*)
    echo "usage: $0 up|down|bench" >&2
    exit 2
    ;;
esac
