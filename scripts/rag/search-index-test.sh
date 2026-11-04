#!/bin/sh
# Elasticsearch adapter suite; needs EVENTLENS_RAG_TEST_ELASTICSEARCH_URL.
# Any skip fails the run (a skip is not Elasticsearch evidence).
set -u
if [ -z "${EVENTLENS_RAG_TEST_ELASTICSEARCH_URL:-}" ]; then
    echo "search-index-test: EVENTLENS_RAG_TEST_ELASTICSEARCH_URL is required" >&2
    exit 1
fi
log=$(mktemp)
code=0
uv run pytest tests/search -q -rfEs >"$log" 2>&1 || code=$?
cat "$log"
if grep -q "skipped" "$log"; then
    echo "search-index-test: skipped tests are not Elasticsearch evidence" >&2
    exit 1
fi
exit "$code"
