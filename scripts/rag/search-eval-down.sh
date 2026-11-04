#!/bin/sh
# Stop the developer Elasticsearch (project eventlens-search-dev only).
# Idempotent; verifies that no container of the project is left.
set -u
. "$(dirname "$0")/search-lib.sh"
SEARCH_PROJECT=eventlens-search-dev
ART="${SEARCH_ARTIFACT_DIR:-.omo/evidence/search-platform/dev-env}"
mkdir -p "$ART"
: >"$ART/steps.tsv"
OWNED=1
if cleanup_owned; then
    printf 'project %s removed (verified)\n' "$SEARCH_PROJECT"
    exit 0
fi
exit 5
