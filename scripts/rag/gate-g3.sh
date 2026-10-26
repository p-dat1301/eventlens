#!/bin/sh
# G3 aggregate gate (D40, T9). Noninteractive: frozen installs, check, both
# evals (+ reproducibility), isolated pgvector DB steps with a live rag-api
# smoke, the DB suite, web gates and the browser gate. Every step's log and
# exit code lands in the evidence directory. Owned processes/containers are
# removed by the trap here and in with-test-db.sh / gate-g3-db.sh.
# Exit 1: a technical check failed. Exit 2: technical checks PASS but G3
# acceptance remains OPEN (never decided by this script).
set -u
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
cd "$root" || exit 1
web=src/frontend/web
evidence_root=.omo/evidence/G3
mkdir -p "$evidence_root" || exit 1
G3_EVIDENCE_DIR=$(mktemp -d "$evidence_root/run-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX") || exit 1
G3_EVIDENCE_DIR=$(cd "$G3_EVIDENCE_DIR" && pwd)
# db-suite.sh writes its pytest log into G1_EVIDENCE_DIR when set.
G1_EVIDENCE_DIR=$G3_EVIDENCE_DIR
export G3_EVIDENCE_DIR G1_EVIDENCE_DIR
printf 'G3 evidence: %s\n' "$G3_EVIDENCE_DIR"
git rev-parse HEAD >"$G3_EVIDENCE_DIR/commit" 2>/dev/null || true
git status --porcelain >"$G3_EVIDENCE_DIR/worktree-status" 2>/dev/null || true
status=0
step() {
    label=$1
    shift
    printf '\n== %s: %s\n' "$label" "$*"
    "$@" >"$G3_EVIDENCE_DIR/$label.log" 2>&1
    code=$?
    tail -n 15 "$G3_EVIDENCE_DIR/$label.log"
    printf '%s\t%s\t%s\n' "$label" "$code" "$*" >>"$G3_EVIDENCE_DIR/exit-codes.tsv"
    printf '== exit %s: %s\n' "$code" "$label"
    [ "$code" -eq 0 ] || status=1
}
expect_exit() {
    expected=$1
    label=$2
    shift 2
    printf '\n== %s (expect exit %s): %s\n' "$label" "$expected" "$*"
    "$@" >"$G3_EVIDENCE_DIR/$label.log" 2>&1
    code=$?
    tail -n 5 "$G3_EVIDENCE_DIR/$label.log"
    printf '%s\t%s\t%s\n' "$label" "$code" "$*" >>"$G3_EVIDENCE_DIR/exit-codes.tsv"
    [ "$code" -eq "$expected" ] || status=1
}
same_report() {
    cmp "$G3_EVIDENCE_DIR/eval/$1.json" "$G3_EVIDENCE_DIR/eval-rerun/$1.json"
}
eval_dir=$G3_EVIDENCE_DIR/eval
rerun_dir=$G3_EVIDENCE_DIR/eval-rerun

step sync uv sync --frozen --group dev
step web-install sh -c "cd $web && bun install --frozen-lockfile"
step check make check
step enrichment-eval uv run eventlens-rag enrichment-eval --output "$eval_dir"
step rag-eval uv run eventlens-rag rag-eval --output "$eval_dir"
step enrichment-eval-rerun uv run eventlens-rag enrichment-eval --output "$rerun_dir"
step rag-eval-rerun uv run eventlens-rag rag-eval --output "$rerun_dir"
step enrichment-eval-reproducible same_report enrichment-eval
step rag-eval-reproducible same_report rag-eval
step database sh "$here/with-test-db.sh" sh "$here/gate-g3-db.sh"
expect_exit 3 db-unavailable env \
    EVENTLENS_RAG_DATABASE_URL=postgresql://rag_test:rag-test-only@127.0.0.1:1/rag_test \
    uv run eventlens-rag migrate
step web-test sh -c "cd $web && bun test"
step web-check sh -c "cd $web && bun run check"
step web-typecheck sh -c "cd $web && bun run typecheck"
step web-build sh -c "cd $web && bun run build"
step browser sh -c "cd $web && bun run test:e2e"

if [ "$status" -ne 0 ]; then
    printf 'FAIL\n' >"$G3_EVIDENCE_DIR/technical-status"
    printf 'FAIL: technical checks\n' >"$G3_EVIDENCE_DIR/gate-status"
    printf '\nTechnical checks: FAIL\n'
    exit 1
fi
printf 'PASS\n' >"$G3_EVIDENCE_DIR/technical-status"
printf '\nTechnical checks: PASS\n'
# Technical PASS never implies G3 acceptance: F1-F4 independent review verdicts
# and explicit user acceptance are not machine-checkable.
printf 'OPEN: technical PASS only; G3 acceptance not established by this script\n' \
    >"$G3_EVIDENCE_DIR/gate-status"
echo "G3 acceptance OPEN: needs F1-F4 verdicts and user acceptance."
exit 2
