#!/bin/sh
set -eu
name="eventlens-rag-test-$(date +%s)-$$"
created=false
cleanup() {
    if [ "$created" = true ]; then
        docker rm -f "$name" >/dev/null
    fi
}
trap cleanup EXIT HUP INT TERM
docker info >/dev/null
docker run --detach --name "$name" --publish 127.0.0.1::5432 \
    --env POSTGRES_DB=rag_test --env POSTGRES_USER=rag_test \
    --env POSTGRES_PASSWORD=rag-test-only \
    --health-cmd='pg_isready -U rag_test -d rag_test' \
    --health-interval=1s --health-timeout=3s --health-retries=30 \
    --tmpfs /var/lib/postgresql/data \
    pgvector/pgvector:0.8.0-pg17 >/dev/null
created=true
attempt=0
until [ "$(docker inspect --format '{{.State.Health.Status}}' "$name")" = healthy ]; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 40 ]; then
        docker logs "$name"
        exit 1
    fi
    sleep 1
done
port=$(docker inspect --format '{{(index (index .NetworkSettings.Ports "5432/tcp") 0).HostPort}}' "$name")
export EVENTLENS_RAG_DATABASE_URL="postgresql://rag_test:rag-test-only@127.0.0.1:$port/rag_test"
uv run --package eventlens-rag pytest apps/rag/tests/test_postgres_storage.py apps/rag/tests/test_postgres_publication.py -q
