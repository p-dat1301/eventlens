"""Ollama bench run protocol: preflight, durable records, resume, offline report.

Regression for codex-review-20261029 P2: a digest timeout after 28 completed
generations destroyed the whole report. Fake clients count every call; no
network, no model.
"""

import json
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import cast, final
from urllib.error import URLError

import pytest

from backend.rag.evaluation import ollama_bench
from backend.rag.evaluation.guards import no_network
from backend.rag.evaluation.metrics import Json
from backend.rag.evaluation.ollama_bench import (
    OllamaBenchPreflightError,
    rebuild_report,
    run_bench,
    shared_view,
)
from backend.rag.evaluation.ollama_checkpoint import (
    CheckpointLocationError,
    CheckpointStore,
)
from backend.rag.evaluation.search_eval.dataset import (
    EvalQuestion,
    SearchDataset,
    load_dataset,
)
from backend.rag.evaluation.search_eval.factory import SystemOptions, build_systems
from backend.rag.evaluation.search_eval.scoring import RankedPassage
from backend.rag.evaluation.search_eval.systems import SearchSystem

MODEL = "qwen3.5:4b"
DIGEST = "d8b0f5e9760cd168" + "0" * 48
OTHER_DIGEST = "e" * 64
TIMEOUT = "timed out"
type Report = dict[str, Json]


class CrashError(RuntimeError):
    """Simulated process death (never caught by the bench)."""


@final
class FakeOllama:
    """Counts calls; failures are injected per call index or per method."""

    def __init__(  # noqa: PLR0913 - injected failure knobs
        self,
        *,
        digests: Sequence[object] = (DIGEST,),
        fail_chat_at: frozenset[int] | None = None,
        crash_chat_at: int | None = None,
        unload_error: Exception | None = None,
        num_thread: int | None = 8,
        prompt_tokens: int = 3000,
    ) -> None:
        self.digests = list(digests)
        self.fail_chat_at = fail_chat_at or frozenset()
        self.crash_chat_at = crash_chat_at
        self.unload_error = unload_error
        self.num_thread = num_thread
        self.prompt_tokens = prompt_tokens
        self.chat_calls = 0
        self.digest_calls = 0
        self.unload_calls = 0
        self.prompts: list[str] = []

    def chat(self, model: str, system: str, user: str) -> tuple[str, dict[str, object]]:
        del model, system
        self.chat_calls += 1
        if self.crash_chat_at == self.chat_calls:
            raise CrashError
        if self.chat_calls in self.fail_chat_at:
            raise URLError(TIMEOUT)
        self.prompts.append(user)
        body = {
            "abstain": False,
            "answer": "x",
            "claims": [{"text": "x", "sources": ["S1"]}],
        }
        return json.dumps(body), {
            "prompt_eval_count": self.prompt_tokens,
            "eval_count": 40,
            "done_reason": "stop",
        }

    def unload(self, model: str) -> None:
        del model
        self.unload_calls += 1
        if self.unload_error is not None:
            raise self.unload_error

    def digest(self, model: str) -> str | None:
        del model
        index = min(self.digest_calls, len(self.digests) - 1)
        self.digest_calls += 1
        value = self.digests[index]
        if isinstance(value, Exception):
            raise value
        return cast("str | None", value)


@pytest.fixture(scope="module")
def dataset() -> SearchDataset:
    full = load_dataset()
    return replace(full, questions=full.questions[:8])


@pytest.fixture(scope="module")
def system(dataset: SearchDataset) -> SearchSystem:
    (built,) = build_systems(SystemOptions(configs=("memory-bm25",)), None)
    _ = built.prepare(dataset.articles)
    return built


@pytest.fixture
def store(tmp_path: Path) -> CheckpointStore:
    return CheckpointStore(tmp_path / ".private" / "qa.checkpoint")


@final
class Clock:
    """Deterministic perf counter: each read advances 1 s."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 1.0
        return self.now


def _run(  # noqa: PLR0913 - test knobs
    dataset: SearchDataset,
    system: SearchSystem,
    client: FakeOllama,
    store: CheckpointStore,
    *,
    evidence: Callable[[EvalQuestion], Sequence[RankedPassage]] | None = None,
    configuration: dict[str, Json] | None = None,
    evidence_k: int = 5,
    allow_truncation: bool = True,
) -> Report:
    with no_network():
        return run_bench(
            dataset,
            evidence or (lambda q: system.search(q).ranked),
            client,
            [MODEL],
            timer=Clock(),
            store=store,
            evidence_k=evidence_k,
            evidence_configuration=configuration or {"config": "memory-bm25"},
            allow_evidence_truncation=allow_truncation,
        )


def _model(report: Report) -> dict[str, Json]:
    return cast("dict[str, dict[str, Json]]", report["models"])[MODEL]


def _outcomes(report: Report) -> dict[str, int]:
    outcomes = cast("dict[str, dict[str, int]]", _model(report)["outcomes"])
    return outcomes["all"]


# --- preflight ----------------------------------------------------------------


@pytest.mark.parametrize(
    "digest", [None, "qwen3.5:4b", "sha256:short", URLError("timed out")]
)
def test_unpinned_model_identity_stops_before_any_generation(
    dataset: SearchDataset,
    system: SearchSystem,
    store: CheckpointStore,
    digest: object,
) -> None:
    client = FakeOllama(digests=(digest,))

    with pytest.raises(OllamaBenchPreflightError):
        _ = _run(dataset, system, client, store)

    assert client.chat_calls == 0
    assert not store.manifest_path.exists()


def test_evidence_k_never_cuts_the_payload_silently(
    dataset: SearchDataset, system: SearchSystem, store: CheckpointStore
) -> None:
    client = FakeOllama()

    with pytest.raises(OllamaBenchPreflightError, match="exceed evidence_k"):
        _ = _run(dataset, system, client, store, evidence_k=2, allow_truncation=False)
    assert client.chat_calls == 0

    report = _run(dataset, system, client, store, evidence_k=2, allow_truncation=True)
    assert cast("int", report["evidence_truncated_questions"]) > 0


def test_checkpoint_must_be_private(tmp_path: Path) -> None:
    with pytest.raises(CheckpointLocationError):
        _ = CheckpointStore(tmp_path / "evidence" / "qa")


# --- final metadata / cleanup failures keep generations -----------------------


def test_final_digest_timeout_keeps_every_completed_response(
    dataset: SearchDataset, system: SearchSystem, store: CheckpointStore
) -> None:
    # Given: preflight digest ok, all chats ok, the final digest lookup times out.
    client = FakeOllama(digests=(DIGEST, URLError("timed out")))

    report = _run(dataset, system, client, store)

    count = len(dataset.questions)
    assert client.chat_calls == count
    assert len(store.records(MODEL)) == count
    model = _model(report)
    assert _outcomes(report)["answered"] == count
    assert model["final_digest_check"] == "unavailable: URLError"
    assert model["auxiliary_errors"] == ["final_digest: URLError"]
    assert model["status"] == "complete"  # generations are complete and pinned


def test_unload_timeout_is_recorded_not_fatal(
    dataset: SearchDataset, system: SearchSystem, store: CheckpointStore
) -> None:
    client = FakeOllama(unload_error=TimeoutError("timed out"))

    report = _run(dataset, system, client, store)

    assert _model(report)["auxiliary_errors"] == ["unload: TimeoutError"]
    assert len(store.records(MODEL)) == len(dataset.questions)


def test_model_swapped_during_run_fails_the_run_but_keeps_records(
    dataset: SearchDataset, system: SearchSystem, store: CheckpointStore
) -> None:
    client = FakeOllama(digests=(DIGEST, OTHER_DIGEST))

    report = _run(dataset, system, client, store)

    assert report["status"] == "failed"
    assert cast("str", _model(report)["final_digest_check"]).startswith("mismatch")
    assert len(store.records(MODEL)) == len(dataset.questions)


# --- execution errors stay in the denominator ---------------------------------


def test_one_failed_call_mid_batch_keeps_the_others_and_the_denominator(
    dataset: SearchDataset, system: SearchSystem, store: CheckpointStore
) -> None:
    client = FakeOllama(fail_chat_at=frozenset({4}))

    report = _run(dataset, system, client, store)

    count = len(dataset.questions)
    outcomes = _outcomes(report)
    assert outcomes["execution_error"] == 1
    assert outcomes["answered"] == count - 1
    assert sum(outcomes.values()) == count
    model = _model(report)
    validity = cast("dict[str, Json]", model["schema_validity"])
    assert validity["denominator"] == count
    assert validity["numerator"] == count - 1
    assert report["status"] == "partial"
    assert model["status"] == "partial"
    assert len(cast("list[Json]", model["errors"])) == 1


def test_resume_retries_only_the_failed_call(
    dataset: SearchDataset, system: SearchSystem, store: CheckpointStore
) -> None:
    _ = _run(dataset, system, FakeOllama(fail_chat_at=frozenset({4})), store)
    again = FakeOllama()

    report = _run(dataset, system, again, store)

    assert again.chat_calls == 1
    assert report["status"] == "complete"
    rejected = cast("list[dict[str, str]]", report["checkpoint_rejected"])
    assert [r["reason"] for r in rejected] == ["retried-error"]


# --- crash and resume ---------------------------------------------------------


def test_crash_after_n_then_resume_calls_only_the_missing_questions(
    dataset: SearchDataset, system: SearchSystem, store: CheckpointStore
) -> None:
    crashed = FakeOllama(crash_chat_at=4)
    with pytest.raises(CrashError):
        _ = _run(dataset, system, crashed, store)
    assert len(store.records(MODEL)) == 3  # persisted one by one, before the crash

    resumed = FakeOllama()
    report = _run(dataset, system, resumed, store)

    count = len(dataset.questions)
    assert resumed.chat_calls == count - 3
    assert report["status"] == "complete"
    calls = cast("dict[str, int]", _model(report)["calls"])
    assert calls == {"network_this_run": count - 3, "replayed_from_checkpoint": 3}
    rows = cast("list[dict[str, Json]]", _model(report)["questions"])
    origins = {str(r["origin_run_id"]) for r in rows if r["source"] == "replayed"}
    assert len(origins) == 1
    assert origins != {str(report["run_id"])}  # provenance of the original run kept


def test_cached_latency_is_never_reported_as_network_timing(
    dataset: SearchDataset, system: SearchSystem, store: CheckpointStore
) -> None:
    first = _run(dataset, system, FakeOllama(), store)
    second = _run(dataset, system, FakeOllama(), store)

    count = len(dataset.questions)
    network = cast("dict[str, Json]", _model(second)["latency_network"])
    replayed = cast("dict[str, Json]", _model(second)["latency_replayed"])
    assert network["samples"] == 0
    assert replayed["samples"] == count
    assert replayed == _model(first)["latency_network"]
    tokens = cast("dict[str, int]", _model(second)["tokens"])
    assert tokens["prompt_this_run"] == 0
    assert tokens["prompt"] == 3000 * count


# --- damaged checkpoint files -------------------------------------------------


def _torn(text: str) -> str:
    return text[: len(text) // 2]


def _extra_key(text: str) -> str:
    return json.dumps({**cast("dict[str, Json]", json.loads(text)), "extra": 1})


def _edited_content(text: str) -> str:
    return json.dumps({**cast("dict[str, Json]", json.loads(text)), "content": "{}"})


@pytest.mark.parametrize(
    ("damage", "reason"),
    [(_torn, "unreadable"), (_extra_key, "schema"), (_edited_content, "digest")],
)
def test_torn_or_invalid_record_is_rejected_and_regenerated(
    dataset: SearchDataset,
    system: SearchSystem,
    store: CheckpointStore,
    damage: Callable[[str], str],
    reason: str,
) -> None:
    _ = _run(dataset, system, FakeOllama(), store)
    target = store.path(MODEL, dataset.questions[2].question_id)
    _ = target.write_text(damage(target.read_text(encoding="utf-8")), encoding="utf-8")
    again = FakeOllama()

    report = _run(dataset, system, again, store)

    assert again.chat_calls == 1
    rejected = cast("list[dict[str, str]]", report["checkpoint_rejected"])
    assert [r["reason"] for r in rejected] == [reason]
    assert any((store.root / "rejected").iterdir())  # moved aside, not deleted


def test_no_temp_files_are_left_behind(
    dataset: SearchDataset, system: SearchSystem, store: CheckpointStore
) -> None:
    _ = _run(dataset, system, FakeOllama(), store)

    assert not list(store.root.rglob("*.tmp"))


# --- identity: anything that changes the request invalidates reuse ------------


def _shift_first_span(
    system: SearchSystem,
) -> Callable[[EvalQuestion], Sequence[RankedPassage]]:
    def evidence(question: EvalQuestion) -> Sequence[RankedPassage]:
        ranked = system.search(question).ranked
        first = ranked[0]
        shorter = replace(first, char_end=first.char_end - 1, text=first.text[:-1])
        return (shorter, *ranked[1:])

    return evidence


@pytest.mark.parametrize(
    "change",
    [
        "model_digest",
        "prompt_version",
        "system_prompt",
        "evidence",
        "evidence_order",
        "options",
        "configuration",
    ],
)
def test_changed_request_never_reuses_old_responses(
    dataset: SearchDataset,
    system: SearchSystem,
    store: CheckpointStore,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    _ = _run(dataset, system, FakeOllama(), store)
    client = FakeOllama()
    evidence: Callable[[EvalQuestion], Sequence[RankedPassage]] | None = None
    configuration: dict[str, Json] | None = None
    if change == "model_digest":
        client = FakeOllama(digests=(OTHER_DIGEST,))
    elif change == "prompt_version":
        monkeypatch.setattr(ollama_bench, "PROMPT_VERSION", "ollama-bench-prompt.v3")
    elif change == "system_prompt":
        monkeypatch.setattr(ollama_bench, "SYSTEM_PROMPT", "Khác.")
    elif change == "evidence":
        evidence = _shift_first_span(system)
    elif change == "evidence_order":
        evidence = lambda q: tuple(reversed(system.search(q).ranked[:5]))  # noqa: E731
    elif change == "options":
        client = FakeOllama(num_thread=4)
    else:
        configuration = {"config": "memory-bm25#fill6"}

    report = _run(
        dataset, system, client, store, evidence=evidence, configuration=configuration
    )

    count = len(dataset.questions)
    assert client.chat_calls == count
    calls = cast("dict[str, int]", _model(report)["calls"])
    assert calls["replayed_from_checkpoint"] == 0
    rejected = cast("list[dict[str, str]]", report["checkpoint_rejected"])
    assert {str(r["reason"]) for r in rejected} == {"stale"}


def test_identical_rerun_reuses_everything(
    dataset: SearchDataset, system: SearchSystem, store: CheckpointStore
) -> None:
    _ = _run(dataset, system, FakeOllama(), store)
    again = FakeOllama()

    _ = _run(dataset, system, again, store)

    assert again.chat_calls == 0


# --- offline report -----------------------------------------------------------


def test_offline_rebuild_matches_the_live_report(
    dataset: SearchDataset, system: SearchSystem, store: CheckpointStore
) -> None:
    live = _run(dataset, system, FakeOllama(fail_chat_at=frozenset({2})), store)

    with no_network():
        rebuilt = rebuild_report(store, dataset)

    assert rebuilt == live
    assert rebuilt["report_digest"] == live["report_digest"]


def test_shared_view_has_no_model_output_or_prompt(
    dataset: SearchDataset, system: SearchSystem, store: CheckpointStore
) -> None:
    client = FakeOllama()
    report = _run(dataset, system, client, store)

    text = json.dumps(shared_view(report), ensure_ascii=False)

    assert '"responses"' not in text
    assert all(prompt[:60] not in text for prompt in client.prompts)
    record = store.records(MODEL)[dataset.questions[0].question_id]
    assert record.prompt == client.prompts[0]  # full prompt only in .private


def test_context_risk_is_flagged_from_model_usage(
    dataset: SearchDataset, system: SearchSystem, store: CheckpointStore
) -> None:
    report = _run(dataset, system, FakeOllama(prompt_tokens=7900), store)

    context = cast("dict[str, Json]", _model(report)["context"])
    assert context["prompt_plus_cap_over_ctx"] == len(dataset.questions)
    assert context["max_prompt_eval_count"] == 7900


# --- CLI ----------------------------------------------------------------------


def test_cli_refuses_a_non_private_output(tmp_path: Path) -> None:
    from typer.testing import CliRunner  # noqa: PLC0415

    from backend.rag.cli_main import app  # noqa: PLC0415

    result = CliRunner().invoke(
        app,
        [
            "ollama-bench",
            "--output",
            str(tmp_path / "evidence"),
            "--base-url",
            "http://127.0.0.1:9",
            "--models",
            MODEL,
        ],
    )

    assert result.exit_code == 2


def test_cli_rebuilds_the_report_offline(
    dataset: SearchDataset, system: SearchSystem, tmp_path: Path
) -> None:
    from typer.testing import CliRunner  # noqa: PLC0415

    from backend.rag.cli_main import app  # noqa: PLC0415

    store = CheckpointStore(tmp_path / ".private" / "cli.checkpoint")
    live = _run(dataset, system, FakeOllama(), store)
    ids = tmp_path / "ids.json"
    _ = ids.write_text(json.dumps([q.question_id for q in dataset.questions]))
    out = tmp_path / ".private" / "out"
    shared = tmp_path / "shared" / "qa.shared.json"

    result = CliRunner().invoke(
        app,
        [
            "ollama-bench-report",
            "--checkpoint",
            str(store.root),
            "--output",
            str(out),
            "--label",
            "qa",
            "--question-ids",
            str(ids),
            "--shared-output",
            str(shared),
        ],
    )

    assert result.exit_code == 0, result.output
    rebuilt = cast("dict[str, Json]", json.loads((out / "qa.json").read_text()))
    assert rebuilt["report_digest"] == live["report_digest"]
    assert '"responses"' not in shared.read_text(encoding="utf-8")
