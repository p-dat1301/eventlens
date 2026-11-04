"""scripts/rag/search-*.sh with fake docker/curl/uv executables.

No container is started: the fakes record their argv and return scripted exit
codes, so the tests prove how Compose is called, which steps run, how failures
and signals propagate, and that cleanup only targets this run's project.
"""

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
FAKE_DOCKER = """#!/bin/sh
printf '%s\\n' "$*" >>"$FAKE_LOG/docker.argv"
case "$*" in
  *" up "*) exit "${FAKE_UP_RC:-0}" ;;
  *" port elasticsearch 9200"*) echo "127.0.0.1:59999"; exit 0 ;;
  *" port postgres 5432"*) echo "127.0.0.1:59998"; exit 0 ;;
  *" down "*) exit "${FAKE_DOWN_RC:-0}" ;;
  *" ps --all --quiet"*) printf '%s' "${FAKE_LEFT:-}"; exit 0 ;;
esac
exit 0
"""
FAKE_CURL = """#!/bin/sh
printf '%s\\n' "$*" >>"$FAKE_LOG/curl.argv"
exit "${FAKE_CURL_RC:-0}"
"""
FAKE_UV = """#!/bin/sh
printf '%s\\n' "$*" >>"$FAKE_LOG/uv.argv"
case "$*" in
  *pytest*)
    for arg in "$@"; do
      if [ "$prev" = "--junitxml" ]; then
        printf '<testsuite tests="3" skipped="%s"/>' "${FAKE_SKIPPED:-0}" >"$arg"
      fi
      prev=$arg
    done ;;
esac
if [ -n "${FAKE_UV_SLEEP:-}" ]; then sleep "$FAKE_UV_SLEEP"; fi
exit "${FAKE_UV_RC:-0}"
"""


def _fakes(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    for name, body in {"docker": FAKE_DOCKER, "curl": FAKE_CURL, "uv": FAKE_UV}.items():
        path = bin_dir / name
        _ = path.write_text(body)
        path.chmod(0o755)
    log = tmp_path / "log"
    log.mkdir()
    return {
        **{k: v for k, v in os.environ.items() if not k.startswith("EVENTLENS_")},
        "DOCKER_BIN": str(bin_dir / "docker"),
        "CURL_BIN": str(bin_dir / "curl"),
        "UV_BIN": str(bin_dir / "uv"),
        "FAKE_LOG": str(log),
        "SEARCH_ES_WAIT_SECONDS": "2",
        "SEARCH_RUN_ID": "test-run",
        "SEARCH_ARTIFACT_DIR": str(tmp_path / "artifacts"),
    }


def _run(
    script: str, env: dict[str, str], **extra: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["/bin/sh", f"scripts/rag/{script}"],
        cwd=ROOT,
        env={**env, **extra},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def _lines(env: dict[str, str], name: str) -> list[str]:
    path = Path(env["FAKE_LOG"]) / name
    return path.read_text().splitlines() if path.exists() else []


def _steps(env: dict[str, str]) -> list[tuple[str, str]]:
    text = (Path(env["SEARCH_ARTIFACT_DIR"]) / "steps.tsv").read_text()
    pairs: list[tuple[str, str]] = []
    for line in text.splitlines():
        name, code = line.split("\t")
        pairs.append((name, code))
    return pairs


def _artifact(env: dict[str, str], name: str) -> str:
    return (Path(env["SEARCH_ARTIFACT_DIR"]) / name).read_text().strip()


FILE = "--file docker/search-eval.compose.yaml"
PROJECT = f"--project-name eventlens-search-eval-test-run {FILE}"


def test_success_calls_compose_with_explicit_executable_and_arguments(
    tmp_path: Path,
) -> None:
    env = _fakes(tmp_path)
    result = _run("search-eval.sh", env)
    assert result.returncode == 0, result.stderr
    docker = _lines(env, "docker.argv")
    assert docker[0] == (
        f"compose {PROJECT} --profile postgres up --detach --wait "
        "--wait-timeout 2 elasticsearch postgres"
    )
    assert all(line.startswith(f"compose {PROJECT}") for line in docker)
    assert _steps(env) == [
        ("compose-up", "0"),
        ("wait-elasticsearch", "0"),
        ("migrate", "0"),
        ("benchmark", "0"),
        ("compose-down", "0"),
    ]
    assert _artifact(env, "exit-code") == "0"
    assert _artifact(env, "cleanup") == "verified"
    benchmark = _lines(env, "uv.argv")[-1]
    assert "--rerank fake" in benchmark
    assert "--repeats 3" in benchmark


def test_cleanup_never_targets_the_shared_eventlens_project(tmp_path: Path) -> None:
    env = _fakes(tmp_path)
    _ = _run("search-eval.sh", env, FAKE_UV_RC="1")
    for line in _lines(env, "docker.argv"):
        assert "--project-name eventlens-search-eval-test-run" in line
        assert "--project-name eventlens " not in line + " "
    downs = [line for line in _lines(env, "docker.argv") if " down " in line]
    down = "--profile postgres down --volumes --remove-orphans --timeout 30"
    assert downs == [f"compose {PROJECT} {down}"]


def test_startup_failure_stops_before_benchmark(tmp_path: Path) -> None:
    env = _fakes(tmp_path)
    result = _run("search-eval.sh", env, FAKE_UP_RC="1")
    assert result.returncode == 3
    assert _lines(env, "uv.argv") == []
    assert [s for s, _ in _steps(env)] == ["compose-up", "compose-down"]
    assert (Path(env["SEARCH_ARTIFACT_DIR"]) / "diagnostics-ps.log").exists()
    assert _artifact(env, "cleanup") == "verified"


def test_health_timeout_exits_non_zero_without_running_anything(tmp_path: Path) -> None:
    env = _fakes(tmp_path)
    started = time.monotonic()
    result = _run("search-eval.sh", env, FAKE_CURL_RC="7")
    assert result.returncode == 4
    assert time.monotonic() - started < 30
    assert _lines(env, "uv.argv") == []
    assert ("wait-elasticsearch", "1") in _steps(env)


def test_benchmark_failure_code_is_preserved(tmp_path: Path) -> None:
    env = _fakes(tmp_path)
    result = _run("search-eval.sh", env, FAKE_UV_RC="2")
    # migrate fails first with the same fake code: dependency step -> 4.
    assert result.returncode == 4
    env2 = _fakes(tmp_path / "second")
    result = _run(
        "search-eval.sh",
        env2,
        FAKE_UV_RC="9",
        SEARCH_EVAL_CONFIGS="es-bm25",
    )
    assert result.returncode == 9
    assert _artifact(env2, "exit-code") == "9"
    assert ("benchmark", "9") in _steps(env2)


def test_cleanup_failure_is_reported_not_hidden(tmp_path: Path) -> None:
    env = _fakes(tmp_path)
    result = _run(
        "search-eval.sh", env, SEARCH_EVAL_CONFIGS="es-bm25", FAKE_LEFT="abc123"
    )
    assert result.returncode == 5
    assert _artifact(env, "cleanup") == "failed"


def test_external_endpoints_are_never_started_or_torn_down(tmp_path: Path) -> None:
    env = _fakes(tmp_path)
    result = _run(
        "search-eval.sh",
        env,
        EVENTLENS_RAG_EVAL_ELASTICSEARCH_URL="http://127.0.0.1:9200",
        EVENTLENS_RAG_EVAL_DATABASE_URL="postgresql://u:p@127.0.0.1:1/db",
    )
    assert result.returncode == 0, result.stderr
    assert _lines(env, "docker.argv") == []
    assert _artifact(env, "cleanup") == "not-owned"
    assert (
        "postgresql://"
        not in (Path(env["SEARCH_ARTIFACT_DIR"]) / "run.env").read_text()
    )


def test_two_runs_use_distinct_projects(tmp_path: Path) -> None:
    first = _fakes(tmp_path / "a")
    second = _fakes(tmp_path / "b")
    _ = _run(
        "search-eval.sh", first, SEARCH_RUN_ID="run-a", SEARCH_EVAL_CONFIGS="es-bm25"
    )
    _ = _run(
        "search-eval.sh", second, SEARCH_RUN_ID="run-b", SEARCH_EVAL_CONFIGS="es-bm25"
    )
    assert all(
        "eventlens-search-eval-run-a" in line for line in _lines(first, "docker.argv")
    )
    assert all(
        "eventlens-search-eval-run-b" in line for line in _lines(second, "docker.argv")
    )


@pytest.mark.parametrize(("sig", "code"), [(signal.SIGTERM, 143), (signal.SIGINT, 130)])
def test_signals_are_never_reported_as_success(
    tmp_path: Path, sig: signal.Signals, code: int
) -> None:
    env = _fakes(tmp_path)
    process = subprocess.Popen(
        ["/bin/sh", "scripts/rag/search-eval.sh"],
        cwd=ROOT,
        env={**env, "FAKE_UV_SLEEP": "3", "SEARCH_EVAL_CONFIGS": "es-bm25"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 20
    while not _lines(env, "uv.argv") and time.monotonic() < deadline:
        time.sleep(0.1)
    process.send_signal(sig)
    assert process.wait(timeout=30) == code
    assert _artifact(env, "exit-code") == str(code)
    assert _artifact(env, "cleanup") == "verified"


def test_index_suite_fails_on_skips_and_passes_clean(tmp_path: Path) -> None:
    env = _fakes(tmp_path)
    skipped = _run("search-index-test.sh", env, FAKE_SKIPPED="2")
    assert skipped.returncode == 2
    clean_env = _fakes(tmp_path / "clean")
    clean = _run("search-index-test.sh", clean_env)
    assert clean.returncode == 0, clean.stderr
    assert [s for s, _ in _steps(clean_env)] == [
        "compose-up",
        "wait-elasticsearch",
        "pytest",
        "compose-down",
    ]
    assert "--junitxml" in _lines(clean_env, "uv.argv")[0]


def test_index_suite_preserves_test_failures(tmp_path: Path) -> None:
    env = _fakes(tmp_path)
    assert _run("search-index-test.sh", env, FAKE_UV_RC="1").returncode == 1


def test_dev_down_only_touches_the_dev_project(tmp_path: Path) -> None:
    env = _fakes(tmp_path)
    result = _run("search-eval-down.sh", env)
    assert result.returncode == 0, result.stderr
    for line in _lines(env, "docker.argv"):
        assert line.startswith(f"compose --project-name eventlens-search-dev {FILE}")


def test_dev_up_refuses_a_port_owned_by_someone_else(tmp_path: Path) -> None:
    env = _fakes(tmp_path)
    # curl answers (port busy) and the dev project has no running container.
    result = _run("search-eval-up.sh", env)
    assert result.returncode == 3
    assert not any(" up " in line for line in _lines(env, "docker.argv"))


def test_make_passes_variables_to_the_script(tmp_path: Path) -> None:
    env = _fakes(tmp_path)
    result = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "make",
            "--no-print-directory",
            "search-eval",
            f"DOCKER={env['DOCKER_BIN']}",
            "SEARCH_EVAL_REPEATS=4",
            "SEARCH_EVAL_CONFIGS=es-hybrid",
            "SEARCH_EVAL_EMBEDDINGS=bge-m3",
            "SEARCH_EVAL_RERANK=none",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    benchmark = _lines(env, "uv.argv")[-1]
    for expected in (
        "--repeats 4",
        "--configs es-hybrid",
        "--embeddings bge-m3",
        "--rerank none",
    ):
        assert expected in benchmark


def test_external_corpus_paths_pass_as_single_arguments_and_need_a_database(
    tmp_path: Path,
) -> None:
    env = _fakes(tmp_path)
    spaced = tmp_path / "dir with space; $(touch pwned)"
    spaced.mkdir()
    result = _run(
        "search-eval.sh",
        env,
        SEARCH_EVAL_CONFIGS="es-bm25@codepoint-1200-200.v1",
        SEARCH_EVAL_ARTICLES=str(spaced / "articles.jsonl"),
        SEARCH_EVAL_MANIFEST=str(spaced / "manifest.json"),
        SEARCH_EVAL_QUESTIONS=str(spaced / "questions.json"),
        SEARCH_EVAL_EXPECT_EXPORT_ID="search-test-200-v1",
        SEARCH_EVAL_EXPECT_ARTICLES="200",
        SEARCH_EVAL_RESUME="1",
    )

    assert result.returncode == 0, result.stderr
    assert not (ROOT / "pwned").exists()
    docker = _lines(env, "docker.argv")
    assert docker[0].endswith("elasticsearch postgres")  # import needs PostgreSQL
    assert ("migrate", "0") in _steps(env)
    benchmark = _lines(env, "uv.argv")[-1]
    assert f"--articles {spaced}/articles.jsonl" in benchmark
    assert "--expect-export-id search-test-200-v1 --expect-articles 200" in benchmark
    assert "--resume" in benchmark
    assert "--checkpoint-dir" in benchmark
    assert "external_corpus=yes" in _artifact(env, "run.env")
