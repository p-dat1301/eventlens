"""Independent G1 CLI/SQL checks; run only through with-test-db.sh."""

import json
import os
import subprocess

import psycopg


def cli(*arguments: str, expected: int = 0) -> str:
    command = [
        "rtk", "proxy", "uv", "run", "--package", "eventlens-rag",
        "eventlens-rag", *arguments,
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    print(json.dumps({"command": command, "exit": result.returncode,
                      "stdout": result.stdout, "stderr": result.stderr}), flush=True)
    assert result.returncode == expected
    return result.stdout


url = os.environ["EVENTLENS_RAG_DATABASE_URL"]
cli("migrate")
cli("migrate")
assert "persisted 30 fixture articles" in cli("load-fixtures")
with psycopg.connect(url) as connection:
    assert connection.execute("SELECT count(*) FROM rag_current_lexical").fetchone() == (30,)
    assert connection.execute("SELECT count(*) FROM rag_current_vectors").fetchone() == (0,)
    matches = connection.execute(
        "SELECT article_id::text FROM rag_current_lexical "
        "WHERE search_vector @@ plainto_tsquery('simple', 'harbor') ORDER BY article_id"
    ).fetchall()
    assert [row[0][-3:] for row in matches] == ["001", "002", "015"]
    document = connection.execute(
        "SELECT document_json FROM rag_current_lexical ORDER BY article_id LIMIT 1"
    ).fetchone()[0]
    for span in document["evidence_spans"]:
        assert document["normalized_content"][span["start_offset"]:span["end_offset"]] == span["excerpt"]
    print("PASS: lexical 30, vectors 0 before rebuild, harbor sources 001/002/015, exact source span", flush=True)

assert "30 current articles (0 reused generations)" in cli("rebuild")
assert "30 current articles (30 reused generations)" in cli("rebuild")
with psycopg.connect(url) as connection:
    assert connection.execute("SELECT count(*) FROM rag_current_vectors").fetchone() == (30,)
    assert connection.execute("SELECT count(*) FROM rag_index_generations").fetchone() == (30,)
    assert connection.execute("SELECT count(*) FROM rag_schema_migrations").fetchone() == (5,)
    distance = connection.execute(
        "SELECT a.embedding <=> b.embedding FROM rag_current_vectors a "
        "JOIN rag_current_vectors b USING (chunk_id) LIMIT 1"
    ).fetchone()
    assert distance == (0.0,)
    print("PASS: vectors 30, generations 30, migration ledger 5, self cosine distance 0", flush=True)

query_url = url + "?connect_timeout=5&application_name=codex_g1_review"
cli("migrate", "--database-url", query_url)
make = subprocess.run(
    ["rtk", "proxy", "make", "rag-migrate", "RAG_DATABASE_URL=" + query_url],
    capture_output=True, text=True, check=False,
    env={key: value for key, value in os.environ.items() if key != "EVENTLENS_RAG_DATABASE_URL"},
)
print(json.dumps({"command": "make rag-migrate with valid multi-parameter URL",
                  "exit": make.returncode, "stdout": make.stdout, "stderr": make.stderr}), flush=True)
assert make.returncode != 0
assert "Missing option" in make.stderr + make.stdout
print("REPRODUCED P2: valid URL succeeds in direct CLI, fails through Makefile", flush=True)

for action in ("migrate", "load-fixtures", "rebuild"):
    cli(action, "--database-url", "postgresql://rag_test:rag-test-only@127.0.0.1:1/rag_test", expected=3)
print("PASS: all three CLI actions return typed unavailable exit 3", flush=True)
