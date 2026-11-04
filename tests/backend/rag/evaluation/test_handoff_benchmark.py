"""External-corpus benchmark: corpus selection, labels, chunkers, reports.

Synthetic handoff batches only (CI never reads private data or models).
"""

import json
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unicodedata import normalize
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from backend.rag.application.ports.search_sync import ProjectionRevision
from backend.rag.application.use_cases.search_projection import (
    ProjectRevision,
    chunk_revision,
)
from backend.rag.cli_main import app
from backend.rag.domain.article_document import normalized_content_hash
from backend.rag.domain.chunking import DEFAULT_POLICY, chunk_document
from backend.rag.domain.paragraph_chunking import DEFAULT_PARAGRAPH_POLICY
from backend.rag.domain.search_index import GenerationStatus, IndexGeneration
from backend.rag.evaluation.search_eval.breakdown import span_ceiling
from backend.rag.evaluation.search_eval.compare import paired
from backend.rag.evaluation.search_eval.dataset import corpus_digest_of
from backend.rag.evaluation.search_eval.evidence_sheet import (
    REFERENCE_NOTE,
    EvidenceSheet,
)
from backend.rag.evaluation.search_eval.factory import (
    SearchEvalConfigError,
    SystemOptions,
    build_systems,
)
from backend.rag.evaluation.search_eval.handoff_dataset import (
    HandoffDatasetError,
    load_handoff_dataset,
)
from backend.rag.evaluation.search_eval.runner import evaluate, run_key
from handoff_support import batch, record

if TYPE_CHECKING:
    from backend.rag.evaluation.metrics import Json
    from backend.rag.evaluation.search_eval.systems import ChunkSpan

PORT = (
    "Cảng Lạch Huyện Xanh tạm dừng tiếp nhận tàu vì bão số 2.\n\n"
    + " ".join(f"Câu đệm số {n} về thời tiết và luồng hàng hải." for n in range(40))
    + "\n\nTheo giám đốc Trần Minh Khoa, 42 tàu container đang neo chờ ngoài phao số 0."
    + "\n\n"
    + " ".join(f"Câu kết số {n} về kế hoạch khôi phục." for n in range(30))
)
METRO = (
    "Tuyến metro số 4 Hà Đông - Mê Linh chạy thử với 12 đoàn tàu.\n\n"
    "Ban quản lý cho biết tuyến dài 23,6 km và có 18 nhà ga."
)
QUOTE = "42 tàu container đang neo chờ ngoài phao số 0"
RUNNER = CliRunner()


def _batch() -> tuple[bytes, bytes, list[dict[str, object]]]:
    records = [
        record(content=PORT, word_count=len(PORT.split()), title="Cảng dừng tàu"),
        record(
            content=METRO,
            word_count=len(METRO.split()),
            title="Metro số 4",
            source_domain="metro.example.vn",
            canonical_url=f"https://metro.example.vn/{uuid4().hex}",
        ),
    ]
    articles, manifest = batch(records)
    return articles, manifest, records


def _questions(
    records: list[dict[str, object]], **overrides: object
) -> dict[str, object]:
    port, metro = records
    port_hash = normalized_content_hash(normalize("NFC", str(port["content"])))
    metro_hash = normalized_content_hash(normalize("NFC", str(metro["content"])))
    start = str(port["content"]).index(QUOTE)
    pairs = sorted(
        f"{r['article_id']}:{h}" for r, h in ((port, port_hash), (metro, metro_hash))
    )
    payload: dict[str, object] = {
        "schema_version": "search-questions.v1",
        "corpus_export_id": "unit-batch",
        "corpus_digest": f"sha256:{sha256(chr(10).join(pairs).encode()).hexdigest()}",
        "chunker_version": "codepoint-1200-200.v1",
        "split_seed": "unit",
        "dev_fraction": 0.5,
        "questions": [
            {
                "qid": "u-1",
                "question": "Bao nhiêu tàu container neo chờ ngoài phao số 0?",
                "category": "proper_noun",
                "split": "test",
                "answerable": True,
                "filters": {
                    "source_domains": [],
                    "published_after": None,
                    "published_before": None,
                },
                "gold": [
                    {
                        "article_id": port["article_id"],
                        "content_hash": port_hash,
                        "grade": 2,
                        "spans": [
                            {
                                "start": start,
                                "end": start + len(QUOTE),
                                "excerpt_sha256": sha256(QUOTE.encode()).hexdigest(),
                            }
                        ],
                    },
                    {
                        "article_id": metro["article_id"],
                        "content_hash": metro_hash,
                        "grade": 1,
                        "spans": [],
                    },
                ],
                "reference_answer": "42 tàu.",
                "label_status": "candidate",
                "origin": "machine_candidate",
                "reviewer": None,
                "reviewed_at": None,
                "notes": "unit",
            },
            {
                "qid": "u-2",
                "question": "Tuyến metro số 4 dài bao nhiêu km?",
                "category": "filter",
                "split": "dev",
                "answerable": True,
                "filters": {
                    "source_domains": ["example.vn"],
                    "published_after": None,
                    "published_before": None,
                },
                "gold": [
                    {
                        "article_id": metro["article_id"],
                        "content_hash": metro_hash,
                        "grade": 2,
                        "spans": [
                            {
                                "start": 0,
                                "end": 10,
                                "excerpt_sha256": "0" * 64,  # wrong on purpose
                            }
                        ],
                    }
                ],
                "reference_answer": "23,6 km.",
                "label_status": "candidate",
                "origin": "machine_candidate",
                "reviewer": None,
                "reviewed_at": None,
                "notes": "unit",
            },
        ],
    }
    payload.update(overrides)
    return payload


def _load(**overrides: object):  # noqa: ANN202 - test helper
    articles, manifest, records = _batch()
    questions = json.dumps(_questions(records, **overrides)).encode()
    return load_handoff_dataset(articles, manifest, questions)


def test_handoff_dataset_binds_labels_to_the_exact_batch() -> None:
    loaded = _load()
    dataset = loaded.dataset

    assert dataset.kind == "handoff"
    assert dataset.export_id == "unit-batch"
    assert len(dataset.articles) == 2
    assert dataset.corpus_digest == corpus_digest_of(dataset.articles)
    assert dataset.articles_sha256 == loaded.validation.articles_sha256
    port = dataset.questions[0]
    assert port.split == "test"
    assert port.label_status == "candidate"  # verbatim, never upgraded
    assert port.reference_answer == "42 tàu."
    assert port.required_chunk_ids == ()  # labels never depend on old chunk IDs
    (gold,) = port.passages
    text = next(a for a in dataset.articles if a.article_id == gold.article_id)
    assert text.normalized_content[gold.char_start : gold.char_end] == QUOTE
    codes = {(i["question_id"], i["code"]) for i in dataset.label_issues}
    # Recorded, not repaired: bad excerpt hash and gold outside the filter.
    assert ("u-2", "gold_excerpt_mismatch") in codes
    assert ("u-2", "gold_outside_question_filter") in codes
    assert dataset.questions[1].passages == ()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"corpus_digest": "sha256:" + "0" * 64}, "another corpus digest"),
        ({"corpus_export_id": "other"}, "another export"),
        ({"schema_version": "x"}, "labels schema"),
    ],
)
def test_labels_for_another_corpus_are_refused(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(HandoffDatasetError, match=message):
        _ = _load(**overrides)


def test_manifest_mismatch_and_wrong_export_are_refused() -> None:
    articles, manifest, records = _batch()
    questions = json.dumps(_questions(records)).encode()
    bad = cast("dict[str, object]", json.loads(manifest))
    bad["articles_sha256"] = "1" * 64
    with pytest.raises(HandoffDatasetError, match="articles_sha256_mismatch"):
        _ = load_handoff_dataset(articles, json.dumps(bad).encode(), questions)
    with pytest.raises(HandoffDatasetError, match="expected"):
        _ = load_handoff_dataset(
            articles, manifest, questions, expected_export_id="search-test-200-v1"
        )


def test_stored_revisions_must_equal_the_batch() -> None:
    articles, manifest, records = _batch()
    questions = json.dumps(_questions(records)).encode()
    stored = _load().dataset.articles[:1]

    with pytest.raises(HandoffDatasetError, match="stored revisions"):
        _ = load_handoff_dataset(articles, manifest, questions, stored=stored)


def test_prescreen_is_a_separate_field_and_never_a_label() -> None:
    articles, manifest, records = _batch()
    questions = json.dumps(_questions(records)).encode()
    prescreen = json.dumps({"verdicts": {"u-1": "no_obvious_issue"}}).encode()

    dataset = load_handoff_dataset(
        articles, manifest, questions, prescreen=prescreen
    ).dataset

    assert dataset.questions[0].ai_prescreen == "no_obvious_issue"
    assert dataset.questions[0].label_status == "candidate"
    with pytest.raises(HandoffDatasetError, match="verdict"):
        _ = load_handoff_dataset(
            articles,
            manifest,
            questions,
            prescreen=json.dumps({"verdicts": {"u-1": "human_approved"}}).encode(),
        )


def test_codepoint_projection_keeps_baseline_offsets_and_fingerprints() -> None:
    document = next(
        a for a in _load().dataset.articles if a.normalized_content.startswith("Cảng")
    )

    chunks = chunk_revision(document, None, DEFAULT_POLICY)
    baseline = chunk_document(document, DEFAULT_POLICY)

    assert [(c.char_start, c.char_end, c.fingerprint) for c in chunks] == [
        (b.start_offset, b.end_offset, b.fingerprint) for b in baseline
    ]
    assert all(
        c.content == document.normalized_content[c.char_start : c.char_end]
        for c in chunks
    )
    generation = IndexGeneration(
        generation_id=1,
        index_name="unit",
        chunk_version=DEFAULT_POLICY.version,
        embedding_identity=None,
        embedding_model=None,
        embedding_dimensions=None,
        status=GenerationStatus.ACTIVE,
    )
    projected = ProjectRevision(None, None, policy=DEFAULT_POLICY).execute(
        ProjectionRevision(
            article_id=document.article_id,
            document=document,
            tombstoned=False,
            metadata=None,
        ),
        generation,
    )
    assert {d.chunk_version for d in projected} == {DEFAULT_POLICY.version}


def test_gold_spans_are_remapped_per_chunker_from_offsets() -> None:
    dataset = _load().dataset
    layouts: dict[str, tuple[ChunkSpan, ...]] = {}
    for chunker in (DEFAULT_POLICY.version, DEFAULT_PARAGRAPH_POLICY.version):
        (system,) = build_systems(
            SystemOptions(configs=(f"memory-bm25@{chunker}",)), None
        )
        _ = system.prepare(dataset.articles)
        layouts[chunker] = system.chunk_layout()
        answer = system.search(dataset.questions[0])
        top = answer.ranked[0]
        assert top.chunk_version == chunker
        assert top.char_start <= dataset.questions[0].passages[0].char_start
        system.close()

    assert layouts[DEFAULT_POLICY.version] != layouts[DEFAULT_PARAGRAPH_POLICY.version]
    for layout in layouts.values():
        ceiling = span_ceiling(dataset, layout)
        contained = cast("dict[str, Json]", ceiling["contained"])
        assert contained["numerator"] == contained["denominator"] == 1


def test_postgres_lexical_refuses_a_paragraph_chunker() -> None:
    with pytest.raises(SearchEvalConfigError, match="only exists"):
        _ = build_systems(
            SystemOptions(configs=("postgres-lexical@paragraph-900-1.v1",)),
            lambda: pytest.fail("must not be built"),
        )
    with pytest.raises(SearchEvalConfigError, match="unknown chunker"):
        _ = build_systems(SystemOptions(configs=("memory-bm25@nope.v1",)), None)


def test_report_identifies_corpus_labels_chunker_and_stays_text_free() -> None:
    dataset = _load().dataset
    systems = build_systems(
        SystemOptions(
            configs=(
                "memory-bm25@codepoint-1200-200.v1",
                "memory-bm25@paragraph-900-1.v1",
            )
        ),
        None,
    )

    report, measured = evaluate(dataset, systems, 3, {"git_commit": "unit"})

    block = cast("dict[str, Json]", report["dataset"])
    assert block["kind"] == "handoff"
    assert block["export_id"] == "unit-batch"
    assert block["articles"] == 2
    assert block["label_status"] == {"candidate": 2}
    assert block["splits"] == {"dev": 1, "test": 1}
    assert block["query_clock"] == "2026-10-26T14:26:14+00:00"
    configs = cast("dict[str, dict[str, Json]]", report["configurations"])
    paragraph = configs["memory-bm25@paragraph-900-1.v1"]
    assert cast("dict[str, Json]", paragraph["configuration"])["chunker_version"] == (
        "paragraph-900-1.v1"
    )
    assert cast("dict[str, Json]", paragraph["chunks"])["chunks"]
    assert "test" in cast("dict[str, Json]", paragraph["metrics_by_split"])
    rendered = json.dumps(report, ensure_ascii=False)
    assert "Trần Minh Khoa" not in rendered
    assert "tàu container" not in rendered  # neither article text nor quotes
    assert measured["configurations"]


def test_paired_comparison_is_seeded_and_counts_moves() -> None:
    base: list[dict[str, Json]] = [
        {"question_id": f"q{i}", "ndcg@10": 0.5} for i in range(10)
    ]
    cand: list[dict[str, Json]] = [
        {"question_id": f"q{i}", "ndcg@10": 1.0 if i < 3 else 0.5} for i in range(10)
    ]

    first = paired(base, cand, {f"q{i}" for i in range(10)}, "ndcg@10", samples=500)
    again = paired(base, cand, {f"q{i}" for i in range(10)}, "ndcg@10", samples=500)

    assert first == again
    assert (first["wins"], first["losses"], first["ties"]) == (3, 0, 7)
    assert first["mean_diff"] == 0.15
    assert paired(base, cand, set(), "ndcg@10") == {"metric": "ndcg@10", "n": 0}


def test_evidence_sheet_quotes_gold_and_marks_reference_as_summary(
    tmp_path: Path,
) -> None:
    dataset = _load().dataset
    (system,) = build_systems(SystemOptions(configs=("memory-bm25",)), None)
    _ = system.prepare(dataset.articles)
    sheet = EvidenceSheet(dataset)
    for question in dataset.questions:
        sheet.add(system.name, question, system.search(question))

    files = sheet.write(tmp_path)

    page = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert files == 2 + len(dataset.articles)
    assert QUOTE in page
    assert REFERENCE_NOTE.split(" ")[0] in page
    assert "candidate" in page
    assert "https://www.example.vn/" in page
    reviewer = (tmp_path / "review.csv").read_text(encoding="utf-8").splitlines()
    assert reviewer[1].startswith("u-1,test,proper_noun,candidate,")


def test_cli_never_substitutes_the_fixture_for_a_named_corpus(tmp_path: Path) -> None:
    result = RUNNER.invoke(
        app,
        [
            "search-eval",
            "--output",
            str(tmp_path),
            "--expect-export-id",
            "search-test-200-v1",
        ],
    )
    assert result.exit_code == 2
    assert "never substituted" in result.output

    articles, _, _ = _batch()
    _ = (tmp_path / "a.jsonl").write_bytes(articles)
    partial = RUNNER.invoke(
        app,
        [
            "search-eval",
            "--output",
            str(tmp_path),
            "--articles",
            str(tmp_path / "a.jsonl"),
        ],
    )
    assert partial.exit_code == 2
    assert "go together" in partial.output


def test_cli_scores_an_external_batch_end_to_end(tmp_path: Path) -> None:
    articles, manifest, records = _batch()
    paths = {
        "articles": tmp_path / "articles.jsonl",
        "manifest": tmp_path / "manifest.json",
        "questions": tmp_path / "questions.json",
    }
    _ = paths["articles"].write_bytes(articles)
    _ = paths["manifest"].write_bytes(manifest)
    _ = paths["questions"].write_text(json.dumps(_questions(records)), encoding="utf-8")

    result = RUNNER.invoke(
        app,
        [
            "search-eval",
            "--output",
            str(tmp_path / "out"),
            "--label",
            "unit",
            "--configs",
            "memory-bm25@codepoint-1200-200.v1,memory-bm25",
            "--articles",
            str(paths["articles"]),
            "--manifest",
            str(paths["manifest"]),
            "--questions",
            str(paths["questions"]),
            "--expect-export-id",
            "unit-batch",
            "--expect-articles",
            "2",
            "--compare",
            "memory-bm25@codepoint-1200-200.v1:memory-bm25",
            "--checkpoint-dir",
            str(tmp_path / "ckpt"),
        ],
    )

    assert result.exit_code == 0, result.output
    report = cast(
        "dict[str, dict[str, object]]",
        json.loads((tmp_path / "out/unit.json").read_text(encoding="utf-8")),
    )
    assert report["dataset"]["export_id"] == "unit-batch"
    comparison = cast(
        "dict[str, dict[str, object]]",
        json.loads((tmp_path / "out/unit.compare.json").read_text(encoding="utf-8")),
    )
    assert set(comparison["splits"]) == {"dev", "test"}
    assert sorted(p.name for p in (tmp_path / "ckpt").iterdir()) == [
        "memory-bm25.json",
        "memory-bm25_codepoint-1200-200.v1.json",
    ]
    wrong = RUNNER.invoke(
        app,
        [
            "search-eval",
            "--output",
            str(tmp_path / "out2"),
            "--articles",
            str(paths["articles"]),
            "--manifest",
            str(paths["manifest"]),
            "--questions",
            str(paths["questions"]),
            "--expect-articles",
            "200",
        ],
    )
    assert wrong.exit_code == 2
    assert "expected 200" in wrong.output


def test_query_clock_is_the_export_time_not_wall_clock() -> None:
    dataset = _load().dataset
    assert dataset.as_of == datetime(2026, 10, 26, 14, 26, 14, tzinfo=UTC)
    assert replace(dataset, as_of=dataset.as_of).as_of == dataset.as_of


@pytest.mark.parametrize(
    "option",
    [
        "embeddings",
        "bge_m3_revision",
        "rerank_revision",
        "rerank",
        "device",
        "default_chunker",
    ],
)
def test_checkpoint_key_binds_execution_options(option: str) -> None:
    dataset = _load().dataset
    assert run_key(dataset, 3, {}) != run_key(dataset, 3, {option: "changed"})
    assert run_key(dataset, 3, {}) != run_key(
        replace(dataset, as_of=datetime(2026, 10, 27, tzinfo=UTC)),
        3,
        {},
    )


def test_resume_changed_chunker_recomputes(tmp_path: Path) -> None:
    common = [
        "search-eval",
        "--configs",
        "memory-bm25",
        "--checkpoint-dir",
        str(tmp_path / "ckpt"),
    ]
    for name, chunker, resume in [
        ("first", "codepoint-1200-200.v1", []),
        ("second", "paragraph-900-1.v1", ["--resume"]),
        ("third", "paragraph-900-1.v1", ["--resume"]),
    ]:
        out = tmp_path / name
        result = RUNNER.invoke(
            app, [*common, "--output", str(out), "--chunker", chunker, *resume]
        )
        assert result.exit_code == 0, result.output
        report = cast(
            "dict[str, dict[str, dict[str, dict[str, str]]]]",
            json.loads((out / "search-eval.json").read_text()),
        )
        measured = cast(
            "dict[str, dict[str, list[str]]]",
            json.loads((out / "search-eval.measured.json").read_text()),
        )
        assert (
            report["configurations"]["memory-bm25"]["configuration"]["chunker_version"]
            == chunker
        )
        assert measured["metadata"]["resumed_configurations"] == (
            ["memory-bm25"] if name == "third" else []
        )
