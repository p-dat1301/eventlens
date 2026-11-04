#!/bin/sh
# Import a real handoff batch into an owned, throwaway PostgreSQL +
# Elasticsearch (project eventlens-search-eval-<run-id>) and prove:
#   1. first import writes every revision, provenance row and outbox event;
#   2. a repeat import writes nothing (revisions, provenance, outbox, jobs);
#   3. an upstream version re-sent with other content is refused and rolls
#      back (tampered single-record copy built in $TMPDIR, deleted after);
#   4. outbox -> rebuild/drain -> Elasticsearch holds exactly the PostgreSQL
#      revisions (reconcile --deep), a repeat import queues no event;
#   5. briefing queries over Elasticsearch cite exact NFC slices that map
#      back to the upstream article_version_id (scripts/rag/handoff_probe.py).
# Inputs: HANDOFF_ARTICLES, HANDOFF_MANIFEST, HANDOFF_QUESTIONS (paths, read
# only), SEARCH_ARTIFACT_DIR. Outputs are text-free (IDs, counts, codes).
# Exit: first failing check; 3 startup; 4 dependency; 5 cleanup not verified.
set -u
. "$(dirname "$0")/search-lib.sh"

RUN_ID="${SEARCH_RUN_ID:-$(new_run_id)}"
ART="${SEARCH_ARTIFACT_DIR:-.omo/evidence/search-platform/handoff-import/$RUN_ID}"
SEARCH_PROJECT="eventlens-search-eval-$RUN_ID"
OWNED=1
: "${HANDOFF_ARTICLES:?HANDOFF_ARTICLES required}"
: "${HANDOFF_MANIFEST:?HANDOFF_MANIFEST required}"
: "${HANDOFF_QUESTIONS:?HANDOFF_QUESTIONS required}"
mkdir -p "$ART"
: >"$ART/steps.tsv"
install_traps

if ! step compose-up compose --profile postgres up --detach --wait \
    --wait-timeout "$SEARCH_ES_WAIT_SECONDS" elasticsearch postgres; then
    diagnostics
    exit 3
fi
ES_URL="http://127.0.0.1:$(host_port elasticsearch 9200)"
DB_URL="postgresql://rag_eval:rag-eval-only@127.0.0.1:$(host_port postgres 5432)/rag_eval"
step wait-elasticsearch wait_http "$ES_URL/_cluster/health?wait_for_status=yellow&timeout=1s" ||
    exit 4
step migrate env EVENTLENS_RAG_DATABASE_URL="$DB_URL" "$UV_BIN" run eventlens-rag migrate ||
    exit 4

imp() {
    env EVENTLENS_RAG_EVAL_DATABASE_URL="$DB_URL" "$UV_BIN" run eventlens-rag \
        handoff-import --articles "$1" --manifest "$2"
}
search() {
    env EVENTLENS_RAG_DATABASE_URL="$DB_URL" EVENTLENS_RAG_ELASTICSEARCH_URL="$ES_URL" \
        EVENTLENS_RAG_SEARCH_BACKEND=elasticsearch \
        EVENTLENS_RAG_SEARCH_EMBEDDINGS=fake-bge-m3 \
        "$UV_BIN" run eventlens-rag index "$@"
}

step import-1 imp "$HANDOFF_ARTICLES" "$HANDOFF_MANIFEST" || exit 1
step import-2 imp "$HANDOFF_ARTICLES" "$HANDOFF_MANIFEST" || exit 1

tampered=$(mktemp -d "${TMPDIR:-/tmp}/handoff-tampered.XXXXXX")
step build-tampered "$UV_BIN" run python - "$HANDOFF_ARTICLES" "$HANDOFF_MANIFEST" \
    "$tampered" <<'EOF'
import hashlib, json, sys
from pathlib import Path
articles, manifest, out = sys.argv[1:]
first = json.loads(Path(articles).read_text(encoding="utf-8").splitlines()[0])
first["content"] += " (bản sửa thử nghiệm)"
first["word_count"] = len(first["content"].split())
raw = (json.dumps(first, ensure_ascii=False) + "\n").encode()
m = json.loads(Path(manifest).read_text(encoding="utf-8"))
m.update(articles_sha256=hashlib.sha256(raw).hexdigest(), articles_bytes=len(raw),
         record_count=1, language_counts={first["language"]: 1},
         source_counts={first["source_domain"]: 1},
         parse_version_counts={first["parse_version"]: 1},
         quality_status_counts={first["quality_status"]: 1},
         missing_published_at_count=int(first["published_at"] is None),
         published_at_min=first["published_at"], published_at_max=first["published_at"],
         fetched_at_min=first["fetched_at"], fetched_at_max=first["fetched_at"])
Path(out, "articles.jsonl").write_bytes(raw)
Path(out, "manifest.json").write_text(json.dumps(m), encoding="utf-8")
print(json.dumps({"article_version_id": first["article_version_id"]}))
EOF
step import-conflict imp "$tampered/articles.jsonl" "$tampered/manifest.json"
conflict=$?
rm -rf "$tampered"
printf 'import-conflict-expected-exit\t2\nimport-conflict-actual-exit\t%s\n' \
    "$conflict" >>"$ART/checks.tsv"
[ "$conflict" = 2 ] || exit 1

step index-rebuild search rebuild || exit 1
step drain-1 search drain-outbox || exit 1
step reconcile-1 search reconcile --deep || exit 1
step import-3 imp "$HANDOFF_ARTICLES" "$HANDOFF_MANIFEST" || exit 1
step drain-2 search drain-outbox || exit 1
step health search health || exit 1
step probe env "$UV_BIN" run python scripts/rag/handoff_probe.py "$DB_URL" \
    "$ES_URL" "$HANDOFF_QUESTIONS" 8 || exit 1
exit 0
