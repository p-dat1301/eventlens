#!/bin/sh
# G3 database steps inside the isolated harness: migrate, persist, rebuild, a
# live rag-api smoke against PostgreSQL (fixture model, loopback only), then the
# full DB suite (skips fail). The API process is always stopped on exit.
set -eu
api_pid=
cleanup() {
    if [ -n "$api_pid" ]; then
        kill "$api_pid" 2>/dev/null || true
        wait "$api_pid" 2>/dev/null || true
    fi
}
trap cleanup EXIT HUP INT TERM
# STEP_TAIL: lines of each step log echoed to the console (db-suite echoes its
# whole bounded summary so failing test IDs reach the CI log).
step() {
    label=$1
    shift
    code=0
    "$@" >"$G3_EVIDENCE_DIR/$label.log" 2>&1 || code=$?
    tail -n "${STEP_TAIL:-15}" "$G3_EVIDENCE_DIR/$label.log"
    printf '%s\t%s\t%s\n' "$label" "$code" "$*" >>"$G3_EVIDENCE_DIR/exit-codes.tsv"
    [ "$code" -eq 0 ] || exit "$code"
}
step migrate uv run eventlens-rag migrate
step load-fixtures uv run eventlens-rag load-fixtures
step rebuild uv run eventlens-rag rebuild
port=$(uv run python -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
EVENTLENS_API_RAG_DATABASE_URL=$EVENTLENS_RAG_DATABASE_URL \
    uv run uvicorn backend.rag.api_main:app --host 127.0.0.1 --port "$port" \
    >"$G3_EVIDENCE_DIR/rag-api.log" 2>&1 &
api_pid=$!
step api-health uv run python "$(dirname "$0")/api_smoke.py" health "http://127.0.0.1:$port"
step api-briefing-citation uv run python "$(dirname "$0")/api_smoke.py" briefing "http://127.0.0.1:$port"
cleanup
api_pid=
STEP_TAIL=500 step db-suite sh "$(dirname "$0")/db-suite.sh"
