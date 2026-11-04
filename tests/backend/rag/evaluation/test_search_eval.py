"""Search-selection benchmark: fixture integrity, metrics, reports, review sheet,
and the offline Ollama synthesis bench (fake client; no network, no model)."""

import hashlib
import json
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest

from backend.rag.domain.paragraph_chunking import chunk_paragraphs
from backend.rag.domain.search_index import SearchChunkDocument, search_chunk_id
from backend.rag.evaluation.guards import no_network
from backend.rag.evaluation.metrics import Json
from backend.rag.evaluation.ollama_bench import (
    OllamaBenchConfigError,
    OllamaClient,
    bench_endpoint,
    judge,
    run_bench,
)
from backend.rag.evaluation.ollama_checkpoint import CheckpointStore
from backend.rag.evaluation.search_eval import systems as systems_module
from backend.rag.evaluation.search_eval.dataset import (
    FIXTURE,
    EvalQuestion,
    PassageLabel,
    SearchDataset,
    load_dataset,
)
from backend.rag.evaluation.search_eval.factory import (
    SearchEvalConfigError,
    SystemOptions,
    build_systems,
)
from backend.rag.evaluation.search_eval.review import COLUMNS, build_review_sheet
from backend.rag.evaluation.search_eval.runner import evaluate, failing_configurations
from backend.rag.evaluation.search_eval.scoring import RankedPassage, score_question
from backend.rag.evaluation.search_eval.systems import SearchSystem

ROOT = Path(__file__).parents[4]
type Report = dict[str, Json]


@pytest.fixture(scope="module")
def dataset() -> SearchDataset:
    return load_dataset()


def _memory() -> list[SearchSystem]:
    return build_systems(
        SystemOptions(configs=("memory-bm25", "memory-semantic", "memory-hybrid")),
        None,
    )


@pytest.fixture(scope="module")
def report(dataset: SearchDataset) -> Report:
    with no_network():
        deterministic, _ = evaluate(dataset, _memory(), 3, {})
    return deterministic


def test_fixture_is_reproducible_from_the_authored_corpus() -> None:
    completed = subprocess.run(
        [sys.executable, "scripts/rag/build_search_fixture.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_fixture_labels_point_at_exact_spans_and_chunks(dataset: SearchDataset) -> None:
    texts = {a.article_id: a for a in dataset.articles}
    for question in dataset.questions:
        for passage in question.passages:
            article = texts[passage.article_id]
            assert article.content_hash == passage.content_hash
            assert passage.char_end > passage.char_start
        expected = {
            search_chunk_id(c.article_id, c.content_hash, c.chunker_version, c.index)
            for p in question.passages
            if p.grade == 2 and question.answerable
            for c in chunk_paragraphs(
                p.article_id,
                p.content_hash,
                texts[p.article_id].normalized_content,
            )
            if c.char_start <= p.char_start and p.char_end <= c.char_end
        }
        assert set(question.required_chunk_ids) == expected


def test_fixture_covers_the_required_question_kinds(dataset: SearchDataset) -> None:
    intents = {q.intent for q in dataset.questions}
    assert {
        "factual",
        "numeric",
        "temporal",
        "update",
        "comparison",
        "multi_source",
    } <= intents
    kinds = {q.answerability for q in dataset.questions}
    assert {"answerable", "no_answer", "false_premise"} <= kinds
    assert any(q.temporal_requirement == "two_time_points" for q in dataset.questions)
    manifest = cast(
        "dict[str, object]",
        json.loads((FIXTURE / "manifest.json").read_text(encoding="utf-8")),
    )
    assert manifest["synthetic"] is True
    assert (FIXTURE / "LABELING.md").is_file()


def test_tampered_fixture_is_rejected(tmp_path: Path) -> None:
    for name in ("articles.jsonl", "questions.jsonl", "qrels.jsonl", "manifest.json"):
        _ = (tmp_path / name).write_bytes((FIXTURE / name).read_bytes())
    with (tmp_path / "qrels.jsonl").open("a", encoding="utf-8") as handle:
        _ = handle.write("\n")
    with pytest.raises(ValueError, match="manifest digest"):
        _ = load_dataset(tmp_path)


def _passage(start: int, end: int, article: UUID) -> RankedPassage:
    return RankedPassage(
        chunk_id=uuid4(),
        article_id=article,
        content_hash="h",
        chunk_version="v",
        char_start=start,
        char_end=end,
        text="t",
        fused_rank=1,
        lexical_rank=1,
        semantic_rank=None,
    )


def test_scoring_distinguishes_exact_and_overlapping_support() -> None:
    article, other = uuid4(), uuid4()
    question = EvalQuestion(
        question_id="q",
        query="x",
        intent="factual",
        answerability="answerable",
        required_article_ids=(article,),
        required_chunk_ids=(),
        required_claims=(),
        temporal_requirement=None,
        allowed_scope="fixture-internal",
        article_grades={article: 2},
        passages=(PassageLabel(article, "h", 10, 20, 2, temporal_correct=True),),
    )
    exact = score_question(question, [_passage(0, 30, article)])
    partial = score_question(question, [_passage(15, 40, article)])
    miss = score_question(question, [_passage(0, 30, other), _passage(0, 30, article)])
    assert (exact.exact_support, exact.overlap_support) == (1, 1)
    assert (partial.exact_support, partial.overlap_support) == (0, 1)
    assert miss.reciprocal_rank == 0.5
    assert miss.recall[1] == 0.0
    assert miss.recall[3] == 1.0


def test_report_has_every_required_field_and_is_reproducible(
    report: Report, dataset: SearchDataset
) -> None:
    with no_network():
        again, measured = evaluate(dataset, _memory(), 3, {"git_commit": "x"})
    assert again["report_digest"] == report["report_digest"]
    assert report["repeats"] == 3
    configs = cast("dict[str, dict[str, Json]]", report["configurations"])
    for entry in configs.values():
        metrics = cast("dict[str, Json]", entry["metrics"])
        for key in (
            "recall@1",
            "recall@3",
            "recall@5",
            "recall@10",
            "mrr",
            "ndcg@10",
            "article_recall@10",
            "passage_recall@10",
            "citation_exact_support@5",
            "citation_overlap_support@5",
            "freshness@1",
            "scope_violations",
        ):
            assert key in metrics
        assert metrics["scope_violations"] == 0
        assert entry["metrics_identical_across_repeats"] is True
        assert cast("dict[str, Json]", entry["repeat_consistency"])["value"] == 1.0
        assert entry["errors"] == []
    dataset_info = cast("dict[str, Json]", report["dataset"])
    assert dataset_info["fixture_digest"] == dataset.fixture_digest
    latency = cast("dict[str, Json]", measured["configurations"])
    memory = cast("dict[str, Json]", latency["memory-hybrid"])
    stages = cast("dict[str, Json]", memory["latency"])
    total = cast("dict[str, Json]", stages["total"])
    assert "p95_ms" in total
    assert failing_configurations(report) == []


def test_trap_documents_would_be_reported_as_scope_violations(
    dataset: SearchDataset, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a broken trap set that is visible (same scope, current, live).
    def visible(documents: Sequence[SearchChunkDocument]) -> list[SearchChunkDocument]:
        return [replace(d, chunk_id=uuid4()) for d in documents[:6]]

    monkeypatch.setattr(systems_module, "_traps", visible)
    (system,) = build_systems(SystemOptions(configs=("memory-bm25",)), None)

    # When
    with no_network():
        deterministic, _ = evaluate(dataset, [system], 3, {})

    # Then: hits on trap copies are counted and fail the configuration.
    assert failing_configurations(deterministic) == ["memory-bm25"]


def test_unknown_or_incomplete_configurations_fail_fast() -> None:
    with pytest.raises(SearchEvalConfigError):
        _ = build_systems(SystemOptions(configs=("es-hybrid",)), None)
    with pytest.raises(SearchEvalConfigError):
        _ = build_systems(SystemOptions(configs=("memory-magic",)), None)
    with pytest.raises(SearchEvalConfigError):
        _ = build_systems(SystemOptions(configs=("postgres-lexical",)), None)
    with pytest.raises(SearchEvalConfigError):
        _ = build_systems(
            SystemOptions(configs=("memory-hybrid",), embeddings="bge-m3"), None
        )


def test_review_sheet_pools_candidates_with_blank_reviewer_grades(
    dataset: SearchDataset,
) -> None:
    sheet = build_review_sheet(dataset, _memory(), depth=5)
    lines = sheet.csv_text.splitlines()
    assert lines[0].split(",") == list(COLUMNS)
    assert sheet.rows >= len(dataset.questions)
    assert "reviewer_grade" in sheet.html_text


class _FakeOllama:
    """Stands in for ``OllamaClient``; answers from the cited evidence."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.unloaded: list[str] = []

    def chat(self, model: str, system: str, user: str) -> tuple[str, dict[str, object]]:
        del system
        self.calls.append(model)
        body: dict[str, object]
        if "SJC" in user or "chung kết" in user:
            body = {"abstain": True, "answer": None, "claims": []}
        else:
            body = {
                "abstain": False,
                "answer": "x",
                "claims": [{"text": "x", "sources": ["S1"]}],
            }
        return json.dumps(body), {"prompt_eval_count": 100, "eval_count": 20}

    def unload(self, model: str) -> None:
        self.unloaded.append(model)

    def digest(self, model: str) -> str:
        return hashlib.sha256(model.encode()).hexdigest()


def test_ollama_bench_runs_models_sequentially_on_identical_prompts(
    dataset: SearchDataset, tmp_path: Path
) -> None:
    (system,) = build_systems(SystemOptions(configs=("memory-bm25",)), None)
    _ = system.prepare(dataset.articles)
    fake = _FakeOllama()
    with no_network():
        report = run_bench(
            dataset,
            lambda q: system.search(q).ranked,
            fake,  # type: ignore[arg-type]
            ["qwen3.5:4b", "qwen3.5:9b"],
            store=CheckpointStore(tmp_path / ".private" / "bench"),
            allow_evidence_truncation=True,
        )
    count = len(dataset.questions)
    assert fake.calls == ["qwen3.5:4b"] * count + ["qwen3.5:9b"] * count
    assert fake.unloaded == ["qwen3.5:4b", "qwen3.5:9b"]
    models = cast("dict[str, dict[str, Json]]", report["models"])
    first = models["qwen3.5:4b"]
    assert first["model_digest"] == hashlib.sha256(b"qwen3.5:4b").hexdigest()
    assert cast("dict[str, Json]", first["schema_validity"])["value"] == 1.0
    assert report["offline_only"] is True
    assert report["seed"] == 42


def test_judge_flags_bad_json_dangling_citations_and_unsupported_claims(
    dataset: SearchDataset,
) -> None:
    question = next(q for q in dataset.questions if q.question_id == "q-001")
    (system,) = build_systems(SystemOptions(configs=("memory-bm25",)), None)
    _ = system.prepare(dataset.articles)
    evidence: Sequence[RankedPassage] = system.search(question).ranked[:5]
    assert not judge(question, evidence, "not json").valid
    dangling = judge(
        question,
        evidence,
        json.dumps(
            {
                "abstain": False,
                "answer": "a",
                "claims": [{"text": "a", "sources": ["S9"]}],
            }
        ),
    )
    assert dangling.valid
    assert not dangling.linked
    supported = judge(
        question,
        evidence,
        json.dumps(
            {
                "abstain": False,
                "answer": "a",
                "claims": [{"text": "a", "sources": ["S1"]}],
            }
        ),
    )
    assert supported.linked
    assert supported.supported


def test_ollama_endpoint_must_be_loopback_or_private() -> None:
    assert bench_endpoint("http://127.0.0.1:11434/") == "http://127.0.0.1:11434"
    assert bench_endpoint("http://100.105.48.32:11434") == "http://100.105.48.32:11434"
    assert bench_endpoint("http://192.168.0.7:11434") == "http://192.168.0.7:11434"
    for url in (
        "http://ollama.internal:11434",
        "http://8.8.8.8:11434",
        "https://ollama.example.com",
        "ftp://127.0.0.1",
    ):
        with pytest.raises(OllamaBenchConfigError):
            _ = OllamaClient(url)
