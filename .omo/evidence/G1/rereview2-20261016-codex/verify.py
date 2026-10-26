"""Verify target-specific Make environment and real pgvector CLI flow."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import psycopg


def make(target: str, url: str, extra_env: dict[str, str] | None = None):
    env = {key: value for key, value in os.environ.items()
           if key != "EVENTLENS_RAG_DATABASE_URL"}
    env.update(extra_env or {})
    result = subprocess.run(
        ["rtk", "proxy", "make", target, "RAG_DATABASE_URL=" + url],
        env=env, capture_output=True, text=True, check=False,
    )
    print(json.dumps({"target": target, "exit": result.returncode,
                      "stdout": result.stdout, "stderr": result.stderr}), flush=True)
    return result


base_url = os.environ["EVENTLENS_RAG_DATABASE_URL"]
query_url = base_url + "?sslmode=disable&connect_timeout=2&application_name=g1_review'quoted"
targets = ("rag-migrate", "rag-load-fixtures", "rag-rebuild")

with tempfile.TemporaryDirectory(prefix="g1-make-env-review-") as directory:
    shim = Path(directory) / "uv"
    shim.write_text(
        "#!" + sys.executable + "\n"
        "import json, os\n"
        "print('RECEIVED_URL=' + json.dumps(os.environ['EVENTLENS_RAG_DATABASE_URL']))\n"
    )
    shim.chmod(0o700)
    for target in targets:
        result = make(target, query_url, {"PATH": directory + os.pathsep + os.environ["PATH"]})
        assert result.returncode == 0
        received = next(line.removeprefix("RECEIVED_URL=")
                        for line in result.stdout.splitlines() if line.startswith("RECEIVED_URL="))
        assert json.loads(received).encode() == query_url.encode()
print("PASS: all three targets deliver the URL containing & and apostrophe byte-for-byte", flush=True)

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
    assert connection.execute("SELECT count(*) FROM rag_schema_migrations").fetchone() == (5,)
print("PASS: real pgvector migrate x2, load 30, lexical-before-vector, rebuild x2, reuse 30/30", flush=True)

closed_url = "postgresql://rag_test:rag-test-only@127.0.0.1:1/rag_test?sslmode=disable&connect_timeout=2&application_name=g1_review'quoted"
for target in targets:
    closed = make(target, closed_url)
    assert closed.returncode == 2  # Make wraps the CLI's exit 3.
    assert "rag_storage_unavailable" in closed.stderr + closed.stdout
    assert "Error 3" in closed.stderr + closed.stdout
    assert "Missing option" not in closed.stderr + closed.stdout
    assert "syntax error" not in closed.stderr + closed.stdout
print("PASS: all three Make targets preserve typed CLI unavailable exit 3", flush=True)
