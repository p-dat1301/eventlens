#!/bin/sh
# T6 phase 3 final verification; each step logs separately; exits recorded.
cd "$(dirname "$0")/../../../.." || exit 9
RUN="$1"
printf 'step\tcommand\texit\n' > "$RUN/exit-codes.tsv"
{ git rev-parse HEAD; git status --short --untracked-files=all; uname -a; docker --version; uv --version; } > "$RUN/00-env.txt" 2>&1
step() { n="$1"; shift; d="$1"; shift; "$@" > "$RUN/$n.log" 2>&1; ec=$?; printf '%s\t%s\t%s\n' "$n" "$d" "$ec" >> "$RUN/exit-codes.tsv"; }
step 01-make-check "make check (no DSN)" rtk proxy env -u VIRTUAL_ENV -u EVENTLENS_RAG_DATABASE_URL make check
step 02-make-rag-db-test "make rag-db-test" rtk proxy env -u VIRTUAL_ENV -u EVENTLENS_RAG_DATABASE_URL make rag-db-test
T6="apps/rag/tests/test_synthesis.py apps/rag/tests/test_citations.py apps/rag/tests/test_redaction.py apps/rag/tests/test_openai_adapter.py apps/rag/tests/test_synthesis_budget.py apps/rag/tests/test_synthesis_ledger.py apps/rag/tests/test_synthesis_concurrency.py apps/rag/tests/test_activation.py apps/rag/tests/test_activation_endpoint.py apps/rag/tests/test_synthesis_contracts.py apps/rag/tests/test_synthesis_integration.py apps/rag/tests/test_synthesis_db.py apps/rag/tests/test_synthesis_e2e.py apps/rag/tests/test_architecture.py"
step 03-pytest-t6-files-db "pytest all T6 test files (+architecture) on isolated DB, -rs" rtk proxy env -u VIRTUAL_ENV -u EVENTLENS_RAG_DATABASE_URL sh apps/rag/with-test-db.sh uv run --package eventlens-rag pytest $T6 -q -rs
step 04-git-diff-check "git diff --check" rtk git diff --check
step 05-synthesis-smoke "eventlens-rag synthesis-smoke (isolated empty DB)" rtk proxy env -u VIRTUAL_ENV -u EVENTLENS_RAG_DATABASE_URL sh apps/rag/with-test-db.sh uv run --package eventlens-rag eventlens-rag synthesis-smoke --output "$PWD/$RUN/synthesis-smoke.json" --evidence "$PWD/$RUN/synthesis-smoke-evidence.json"
step 06-retrieval-smoke "eventlens-rag retrieval-smoke (T5 compat, isolated empty DB)" rtk proxy env -u VIRTUAL_ENV -u EVENTLENS_RAG_DATABASE_URL sh apps/rag/with-test-db.sh uv run --package eventlens-rag eventlens-rag retrieval-smoke --output "$PWD/$RUN/retrieval-smoke.json"
step 07-protected-bytes "protected files byte-identical vs HEAD" git diff --quiet HEAD -- apps/rag/src/eventlens_rag/migrations apps/rag/migrations apps/rag/src/eventlens_rag/policies/retrieval-policy.v1.json apps/rag/src/eventlens_rag/policies/retrieval-policy.v2.json apps/rag/src/eventlens_rag/policies/retrieval-policy.v3.json apps/rag/src/eventlens_rag/policies/enrichment-retry.v1.json apps/rag/db-suite.sh apps/rag/with-test-db.sh apps/rag/db-test.sh apps/rag/tests/conftest.py Makefile
echo done > "$RUN/DONE"
