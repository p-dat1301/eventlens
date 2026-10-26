#!/bin/sh
# G1 database steps inside the isolated harness: migrate, persist, rebuild, suite.
set -eu
step() {
    label=$1
    shift
    code=0
    "$@" >"$G1_EVIDENCE_DIR/$label.log" 2>&1 || code=$?
    tail -n 20 "$G1_EVIDENCE_DIR/$label.log"
    printf '%s\t%s\t%s\n' "$label" "$code" "$*" >>"$G1_EVIDENCE_DIR/exit-codes.tsv"
    [ "$code" -eq 0 ] || exit "$code"
}
step migrate uv run eventlens-rag migrate
step load-fixtures uv run eventlens-rag load-fixtures
step rebuild uv run eventlens-rag rebuild
step rebuild-idempotent uv run eventlens-rag rebuild
# Second rebuild must reuse every complete generation (D36): zero new embeddings.
all_reused() {
    counts=$(sed -n 's/.*published \([0-9]*\) current articles (\([0-9]*\) reused generations).*/\1 \2/p' "$1")
    echo "published/reused: $counts"
    set -- $counts
    [ "${1:-0}" -gt 0 ] && [ "$1" = "${2:-}" ]
}
step generation-reuse all_reused "$G1_EVIDENCE_DIR/rebuild-idempotent.log"
step db-suite sh "$(dirname "$0")/db-suite.sh"
