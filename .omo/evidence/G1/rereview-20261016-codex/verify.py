"""Re-review Makefile URL transport on owned real pgvector, without legacy DB."""

import json
import os
import subprocess

import psycopg


def make(target: str, url: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["rtk", "proxy", "make", target, "RAG_DATABASE_URL=" + url],
        env={key: value for key, value in os.environ.items()
             if key != "EVENTLENS_RAG_DATABASE_URL"},
        capture_output=True, text=True, check=False,
    )
    print(json.dumps({"target": target, "url": url, "exit": result.returncode,
                      "stdout": result.stdout, "stderr": result.stderr}), flush=True)
    return result


base_url = os.environ["EVENTLENS_RAG_DATABASE_URL"]
query_url = base_url + "?sslmode=disable&connect_timeout=2&application_name=g1_rereview"
assert make("rag-migrate", query_url).returncode == 0
assert make("rag-migrate", query_url).returncode == 0
loaded = make("rag-load-fixtures", query_url)
assert loaded.returncode == 0
assert "persisted 30 fixture articles" in loaded.stdout
with psycopg.connect(query_url) as connection:
    assert connection.execute("SELECT count(*) FROM rag_current_lexical").fetchone() == (30,)
    assert connection.execute("SELECT count(*) FROM rag_current_vectors").fetchone() == (0,)
first = make("rag-rebuild", query_url)
second = make("rag-rebuild", query_url)
assert first.returncode == second.returncode == 0
assert "30 current articles (0 reused generations)" in first.stdout
assert "30 current articles (30 reused generations)" in second.stdout
with psycopg.connect(query_url) as connection:
    assert connection.execute("SELECT count(*) FROM rag_current_vectors").fetchone() == (30,)
    assert connection.execute("SELECT count(*) FROM rag_index_generations").fetchone() == (30,)
print("PASS: all three Make targets accept ampersand URL on real pgvector; reuse 30/30", flush=True)

# A literal apostrophe is accepted by libpq in this valid query value. Verify the
# newly added shell quoting also preserves it; no shell payload is executed.
apostrophe_url = base_url + "?sslmode=disable&application_name=g1_review'quoted"
direct = subprocess.run(
    ["rtk", "proxy", "uv", "run", "--package", "eventlens-rag", "eventlens-rag",
     "migrate", "--database-url", apostrophe_url],
    capture_output=True, text=True, check=False,
)
print(json.dumps({"probe": "direct CLI with literal apostrophe in URI query value",
                  "exit": direct.returncode, "stdout": direct.stdout, "stderr": direct.stderr}), flush=True)
assert direct.returncode == 0
apostrophe_make = make("rag-migrate", apostrophe_url)
print("APOSTROPHE_MAKE_EXIT=" + str(apostrophe_make.returncode), flush=True)

closed_url = "postgresql://rag_test:rag-test-only@127.0.0.1:1/rag_test?sslmode=disable&connect_timeout=2"
closed = make("rag-migrate", closed_url)
assert closed.returncode == 2
assert "rag_storage_unavailable" in closed.stderr + closed.stdout
assert "Missing option" not in closed.stderr + closed.stdout
print("PASS: unreachable database retains typed unavailable error through Make", flush=True)
