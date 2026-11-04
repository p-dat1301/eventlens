#!/bin/sh
# Search suite (tests/search: Elasticsearch adapter + PG -> outbox -> ES end to
# end) against owned, throwaway Elasticsearch and PostgreSQL (project
# eventlens-search-eval-<run-id>, random loopback ports), or against
# EVENTLENS_RAG_TEST_ELASTICSEARCH_URL / EVENTLENS_RAG_TEST_DATABASE_URL when
# given (never started or torn down by this script).
# Writes JUnit XML; any skipped test fails the run (exit 2): a skip is not
# integration evidence. Exit codes as in search-eval.sh.
set -u
. "$(dirname "$0")/search-lib.sh"

RUN_ID="${SEARCH_RUN_ID:-$(new_run_id)}"
ART="${SEARCH_ARTIFACT_DIR:-.omo/evidence/search-platform/final-gates/search-index-test/$RUN_ID}"
SEARCH_PROJECT="eventlens-search-eval-$RUN_ID"
OWNED=0
mkdir -p "$ART"
: >"$ART/steps.tsv"
install_traps

ES_URL="${EVENTLENS_RAG_TEST_ELASTICSEARCH_URL:-}"
DB_URL="${EVENTLENS_RAG_TEST_DATABASE_URL:-}"
services=""
[ -z "$ES_URL" ] && services="elasticsearch"
[ -z "$DB_URL" ] && services="$services postgres"
printf 'run_id=%s\nelasticsearch=%s\ndatabase=%s\n' "$RUN_ID" \
    "$([ -z "$ES_URL" ] && echo owned || echo external)" \
    "$([ -z "$DB_URL" ] && echo owned || echo external)" >"$ART/run.env"
if [ -n "$services" ]; then
    OWNED=1
    # shellcheck disable=SC2086 # service names are fixed words
    if ! step compose-up compose --profile postgres up --detach --wait \
        --wait-timeout "$SEARCH_ES_WAIT_SECONDS" $services; then
        diagnostics
        exit 3
    fi
fi
[ -z "$ES_URL" ] && ES_URL="http://127.0.0.1:$(host_port elasticsearch 9200)"
[ -z "$DB_URL" ] && DB_URL="postgresql://rag_eval:rag-eval-only@127.0.0.1:$(host_port postgres 5432)/rag_eval"
if ! step wait-elasticsearch wait_http "$ES_URL/_cluster/health?wait_for_status=yellow&timeout=1s"; then
    [ "$OWNED" = 1 ] && diagnostics
    exit 4
fi

step pytest env EVENTLENS_RAG_TEST_ELASTICSEARCH_URL="$ES_URL" \
    EVENTLENS_RAG_DATABASE_URL="$DB_URL" EVENTLENS_RAG_TEST_SERVER_OWNED=1 \
    "$UV_BIN" run pytest tests/search -p no:cacheprovider -rs \
    --junitxml "$ART/junit.xml"
code=$?
cat "$ART/pytest.log"
if [ "$code" -eq 0 ]; then
    skipped=$(sed -n 's/.*skipped="\([0-9]*\)".*/\1/p' "$ART/junit.xml" 2>/dev/null | head -n 1)
    if [ -z "$skipped" ] || [ "$skipped" != 0 ]; then
        printf 'search-index-test invalid: skipped=%s (skips are not evidence)\n' \
            "${skipped:-unknown}" >&2
        exit 2
    fi
fi
exit "$code"
