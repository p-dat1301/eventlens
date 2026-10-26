#!/bin/sh
# G1 aggregate gate (D40). Exit 1: a technical check failed. Exit 2: technical
# checks PASS but G1 acceptance remains OPEN (never decided by this script).
set -u
here=$(dirname "$0")
evidence_root=.omo/evidence/G1
mkdir -p "$evidence_root" || exit 1
G1_EVIDENCE_DIR=$(mktemp -d "$evidence_root/run-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX") || exit 1
export G1_EVIDENCE_DIR
printf 'G1 evidence: %s\n' "$G1_EVIDENCE_DIR"
status=0
step() {
    label=$1
    shift
    printf '\n== %s: %s\n' "$label" "$*"
    "$@" >"$G1_EVIDENCE_DIR/$label.log" 2>&1
    code=$?
    tail -n 20 "$G1_EVIDENCE_DIR/$label.log"
    printf '%s\t%s\t%s\n' "$label" "$code" "$*" >>"$G1_EVIDENCE_DIR/exit-codes.tsv"
    printf '== exit %s: %s\n' "$code" "$*"
    [ "$code" -eq 0 ] || status=1
}
expect_exit() {
    expected=$1
    label=$2
    shift 2
    printf '\n== %s (expect exit %s): %s\n' "$label" "$expected" "$*"
    "$@" >"$G1_EVIDENCE_DIR/$label.log" 2>&1
    code=$?
    tail -n 5 "$G1_EVIDENCE_DIR/$label.log"
    printf '%s\t%s\t%s\n' "$label" "$code" "$*" >>"$G1_EVIDENCE_DIR/exit-codes.tsv"
    [ "$code" -eq "$expected" ] || status=1
}
step sync uv sync --group dev
step stale-worker-path test ! -e apps/ai-worker
step graph-tests uv run pytest tests/backend/shared/contracts tests/backend/event_pipeline \
    tests/backend/rag/application/test_persist_article.py tests/architecture -q
step worker-validate uv run eventlens-events validate fixtures/article_bundle.v1.json
step worker-process uv run eventlens-events process fixtures/article_bundle.v1.json
step database sh "$here/with-test-db.sh" sh "$here/gate-g1-db.sh"
expect_exit 3 db-unavailable env \
    EVENTLENS_RAG_DATABASE_URL=postgresql://rag_test:rag-test-only@127.0.0.1:1/rag_test \
    uv run eventlens-rag migrate
if [ "$status" -ne 0 ]; then
    printf 'FAIL\n' >"$G1_EVIDENCE_DIR/technical-status"
    printf 'FAIL: technical checks\n' >"$G1_EVIDENCE_DIR/gate-status"
    printf '\nTechnical checks: FAIL\n'
    exit 1
fi
printf 'PASS\n' >"$G1_EVIDENCE_DIR/technical-status"
printf '\nTechnical checks: PASS\n'
# Technical PASS never implies G1 acceptance. Acceptance needs F1-F4 review
# verdicts, verified D41 approval evidence and explicit user acceptance; none of
# these is machine-checkable, so this script never writes an acceptance PASS.
printf 'OPEN: technical PASS only; G1 acceptance not established by this script\n' \
    >"$G1_EVIDENCE_DIR/gate-status"
echo "G1 acceptance OPEN: needs F1-F4 verdicts, D41 approval evidence, user acceptance."
exit 2
