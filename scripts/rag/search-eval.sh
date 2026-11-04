#!/bin/sh
# Search-selection benchmark in an owned, throwaway Compose project.
#
# Dependencies: Elasticsearch (and PostgreSQL when postgres-lexical is
# requested) start under project eventlens-search-eval-<run-id> with random
# loopback ports, and are removed (and verified) at the end, also on
# failure, INT or TERM. External endpoints are used as-is and never torn down:
#   EVENTLENS_RAG_EVAL_ELASTICSEARCH_URL  existing Elasticsearch
#   EVENTLENS_RAG_EVAL_DATABASE_URL       existing *empty* RAG database
# Knobs: SEARCH_EVAL_CONFIGS, SEARCH_EVAL_REPEATS (>=3), SEARCH_EVAL_EMBEDDINGS
# (fake-bge-m3|bge-m3), SEARCH_EVAL_RERANK (none|fake|cross-encoder),
# EVENTLENS_RAG_BGE_M3_REVISION, EVENTLENS_RAG_RERANK_REVISION,
# SEARCH_EVAL_DEVICE, SEARCH_EVAL_LABEL, SEARCH_ARTIFACT_DIR.
# External corpus (all optional; the fixture is used when none is set):
#   SEARCH_EVAL_ARTICLES, SEARCH_EVAL_MANIFEST, SEARCH_EVAL_QUESTIONS paths,
#   SEARCH_EVAL_PRESCREEN, SEARCH_EVAL_EXPECT_EXPORT_ID,
#   SEARCH_EVAL_EXPECT_ARTICLES (refuse any other corpus), SEARCH_EVAL_CHUNKER,
#   SEARCH_EVAL_COMPARE (baseline:candidate,...), SEARCH_EVAL_REVIEW_DIR
#   (private sheet), SEARCH_EVAL_RESUME=1 (reuse $ART/checkpoints),
#   SEARCH_EVAL_FIXTURE_DIR (another synthetic fixture, e.g. freshness-updates-v1),
#   SEARCH_EVAL_TRACE_DIR (per-layer text-free traces of pipeline configs).
# Paths are passed as single arguments, never interpolated into a command.
# Exit: benchmark code; 3 startup failed; 4 dependency not healthy;
# 5 cleanup not verified; 130/143 interrupted.
set -u
. "$(dirname "$0")/search-lib.sh"

RUN_ID="${SEARCH_RUN_ID:-$(new_run_id)}"
ART="${SEARCH_ARTIFACT_DIR:-.omo/evidence/search-platform/final-benchmark/$RUN_ID}"
CONFIGS="${SEARCH_EVAL_CONFIGS:-postgres-lexical,es-bm25,es-semantic,es-hybrid,es-hybrid-rerank}"
REPEATS="${SEARCH_EVAL_REPEATS:-3}"
EMBEDDINGS="${SEARCH_EVAL_EMBEDDINGS:-fake-bge-m3}"
RERANK="${SEARCH_EVAL_RERANK:-fake}"
LABEL="${SEARCH_EVAL_LABEL:-search-eval}"
DEVICE="${SEARCH_EVAL_DEVICE:-cpu}"
SEARCH_PROJECT="eventlens-search-eval-$RUN_ID"
OWNED=0
mkdir -p "$ART"
: >"$ART/steps.tsv"
install_traps

ES_URL="${EVENTLENS_RAG_EVAL_ELASTICSEARCH_URL:-}"
DB_URL="${EVENTLENS_RAG_EVAL_DATABASE_URL:-}"
needs_db=0
case ",$CONFIGS," in *,postgres-lexical,* | *,postgres-lexical@*) needs_db=1 ;; esac
# An external batch is imported into PostgreSQL (source of truth) first.
[ -n "${SEARCH_EVAL_ARTICLES:-}" ] && needs_db=1
services=""
[ -z "$ES_URL" ] && services="elasticsearch"
if [ "$needs_db" = 1 ] && [ -z "$DB_URL" ]; then
    services="$services postgres"
fi
{
    printf 'run_id=%s\nconfigs=%s\nrepeats=%s\nembeddings=%s\nrerank=%s\n' \
        "$RUN_ID" "$CONFIGS" "$REPEATS" "$EMBEDDINGS" "$RERANK"
    printf 'elasticsearch=%s\n' "$([ -n "$ES_URL" ] && echo external || echo owned)"
    printf 'database=%s\n' "$([ "$needs_db" = 0 ] && echo none || { [ -n "$DB_URL" ] && echo external || echo owned; })"
} >"$ART/run.env"

if [ -n "$services" ]; then
    OWNED=1
    # shellcheck disable=SC2086 # service names are fixed words
    if ! step compose-up compose --profile postgres up --detach --wait \
        --wait-timeout "$SEARCH_ES_WAIT_SECONDS" $services; then
        diagnostics
        exit 3
    fi
fi
if [ -z "$ES_URL" ]; then
    es_port=$(host_port elasticsearch 9200)
    ES_URL="http://127.0.0.1:$es_port"
fi
if ! step wait-elasticsearch wait_http "$ES_URL/_cluster/health?wait_for_status=yellow&timeout=1s"; then
    [ "$OWNED" = 1 ] && diagnostics
    exit 4
fi
if [ "$needs_db" = 1 ] && [ -z "$DB_URL" ]; then
    pg_port=$(host_port postgres 5432)
    DB_URL="postgresql://rag_eval:rag-eval-only@127.0.0.1:$pg_port/rag_eval"
    if ! step migrate env EVENTLENS_RAG_DATABASE_URL="$DB_URL" \
        "$UV_BIN" run eventlens-rag migrate; then
        diagnostics
        exit 4
    fi
fi

set -- --output "$ART/reports" --label "$LABEL" --configs "$CONFIGS" \
    --repeats "$REPEATS" --embeddings "$EMBEDDINGS" --rerank "$RERANK" \
    --device "$DEVICE" --checkpoint-dir "$ART/checkpoints"
[ -n "${SEARCH_EVAL_ARTICLES:-}" ] && set -- "$@" --articles "$SEARCH_EVAL_ARTICLES"
[ -n "${SEARCH_EVAL_MANIFEST:-}" ] && set -- "$@" --manifest "$SEARCH_EVAL_MANIFEST"
[ -n "${SEARCH_EVAL_QUESTIONS:-}" ] && set -- "$@" --questions "$SEARCH_EVAL_QUESTIONS"
[ -n "${SEARCH_EVAL_PRESCREEN:-}" ] && set -- "$@" --prescreen "$SEARCH_EVAL_PRESCREEN"
[ -n "${SEARCH_EVAL_EXPECT_EXPORT_ID:-}" ] &&
    set -- "$@" --expect-export-id "$SEARCH_EVAL_EXPECT_EXPORT_ID"
[ -n "${SEARCH_EVAL_EXPECT_ARTICLES:-}" ] &&
    set -- "$@" --expect-articles "$SEARCH_EVAL_EXPECT_ARTICLES"
[ -n "${SEARCH_EVAL_CHUNKER:-}" ] && set -- "$@" --chunker "$SEARCH_EVAL_CHUNKER"
[ -n "${SEARCH_EVAL_COMPARE:-}" ] && set -- "$@" --compare "$SEARCH_EVAL_COMPARE"
[ -n "${SEARCH_EVAL_REVIEW_DIR:-}" ] && set -- "$@" --review-dir "$SEARCH_EVAL_REVIEW_DIR"
[ "${SEARCH_EVAL_RESUME:-0}" = 1 ] && set -- "$@" --resume
[ -n "${SEARCH_EVAL_FIXTURE_DIR:-}" ] && set -- "$@" --fixture-dir "$SEARCH_EVAL_FIXTURE_DIR"
[ -n "${SEARCH_EVAL_TRACE_DIR:-}" ] && set -- "$@" --trace-dir "$SEARCH_EVAL_TRACE_DIR"
printf 'external_corpus=%s\nexpect_export_id=%s\n' \
    "$([ -n "${SEARCH_EVAL_ARTICLES:-}" ] && echo yes || echo no)" \
    "${SEARCH_EVAL_EXPECT_EXPORT_ID:-}" >>"$ART/run.env"

step benchmark env EVENTLENS_RAG_EVAL_ELASTICSEARCH_URL="$ES_URL" \
    EVENTLENS_RAG_EVAL_DATABASE_URL="$DB_URL" \
    "$UV_BIN" run eventlens-rag search-eval "$@"
code=$?
cat "$ART/benchmark.log"
exit "$code"
