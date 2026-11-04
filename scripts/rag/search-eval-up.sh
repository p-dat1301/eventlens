#!/bin/sh
# Developer Elasticsearch (project eventlens-search-dev, never "eventlens").
# Port EVENTLENS_SEARCH_ES_PORT (default 9200) on 127.0.0.1; refuses to start
# when another process already owns that port. Idempotent.
set -u
. "$(dirname "$0")/search-lib.sh"
SEARCH_PROJECT=eventlens-search-dev
EVENTLENS_SEARCH_ES_PORT="${EVENTLENS_SEARCH_ES_PORT:-9200}"
export EVENTLENS_SEARCH_ES_PORT
ART="${SEARCH_ARTIFACT_DIR:-.omo/evidence/search-platform/dev-env}"
mkdir -p "$ART"
: >"$ART/steps.tsv"

running=$(compose ps --quiet --status running elasticsearch 2>/dev/null)
if [ -z "$running" ] && "$CURL_BIN" --silent --max-time 2 \
    "http://127.0.0.1:$EVENTLENS_SEARCH_ES_PORT" >/dev/null 2>&1; then
    printf 'port %s is already served by something outside %s; set EVENTLENS_SEARCH_ES_PORT\n' \
        "$EVENTLENS_SEARCH_ES_PORT" "$SEARCH_PROJECT" >&2
    exit 3
fi
if ! step compose-up compose up --detach --wait \
    --wait-timeout "$SEARCH_ES_WAIT_SECONDS" elasticsearch; then
    diagnostics
    cat "$ART/compose-up.log" >&2
    exit 3
fi
printf 'elasticsearch: http://127.0.0.1:%s (project %s)\n' \
    "$EVENTLENS_SEARCH_ES_PORT" "$SEARCH_PROJECT"
