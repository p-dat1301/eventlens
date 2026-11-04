"""paragraph-900-1.v1: exact offsets, sentence-safe cuts, overlap, Unicode."""

from itertools import pairwise
from uuid import UUID

import pytest

from backend.rag.domain.chunking import DEFAULT_POLICY, chunk_document
from backend.rag.domain.paragraph_chunking import (
    PARAGRAPH_CHUNKER_VERSION,
    ChunkMetadata,
    InvalidParagraphChunkingError,
    ParagraphChunk,
    ParagraphChunkingPolicy,
    chunk_paragraphs,
    paragraph_chunk_bounds,
    paragraph_spans,
    sentence_starts,
)
from backend.rag.domain.retrieval import build_span
from backend.rag.domain.search_index import search_chunk_id
from search_support import START, article

ARTICLE = UUID("11111111-1111-4111-8111-111111111111")
HASH = "sha256:" + "a" * 64
VI_SENTENCE = (
    "Thủ tướng Phạm Minh Chính chủ trì phiên họp về hạ tầng giao thông tại Hà Nội."
)


def _chunks(
    text: str, policy: ParagraphChunkingPolicy | None = None
) -> tuple[ParagraphChunk, ...]:
    return chunk_paragraphs(
        ARTICLE, HASH, text, policy=policy or ParagraphChunkingPolicy()
    )


def _long_article(paragraphs: int = 12) -> str:
    body: list[str] = []
    for index in range(paragraphs):
        sentences = " ".join(
            f"Câu {index}.{n}: Ủy ban Nhân dân TP. HCM công bố số liệu {n * 7}%."
            for n in range(5)
        )
        body.append(sentences)
    return "\n\n".join(body)


def _covered(text: str, spans: list[tuple[int, int]]) -> bool:
    covered = [False] * len(text)
    for start, end in spans:
        for i in range(start, end):
            covered[i] = True
    return all(covered[i] or text[i].isspace() for i in range(len(text)))


def test_every_chunk_is_the_exact_slice_of_the_text() -> None:
    text = _long_article()
    chunks = _chunks(text)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.content == text[chunk.char_start : chunk.char_end]
        assert chunk.chunker_version == PARAGRAPH_CHUNKER_VERSION
    assert _covered(text, [(c.char_start, c.char_end) for c in chunks])


def test_chunks_stay_within_bounds_and_end_on_sentences() -> None:
    text = _long_article()
    for chunk in _chunks(text)[:-1]:
        assert len(chunk.content) <= 1000
        assert len(chunk.content) >= 600
        assert chunk.content.rstrip().endswith((".", "%."))


def test_overlap_is_one_trailing_sentence_of_the_previous_chunk() -> None:
    chunks = _chunks(_long_article())
    for previous, current in pairwise(chunks):
        assert current.char_start < previous.char_end  # overlaps
        overlap = previous.content[current.char_start - previous.char_start :]
        assert len(overlap) <= 300
        assert overlap.startswith("Câu")  # a whole sentence, not a fragment


def test_paragraph_provenance_is_recorded() -> None:
    text = "Đoạn một ngắn.\n\nĐoạn hai ngắn.\n\nĐoạn ba ngắn."
    (chunk,) = _chunks(text)
    assert (chunk.paragraph_start, chunk.paragraph_end) == (0, 2)
    assert paragraph_spans(text)[1] == (16, 30)


def test_vietnamese_combining_text_offsets_are_code_points() -> None:
    # Given: NFC text with multi-byte Vietnamese letters and an emoji.
    text = ("Đà Nẵng 🌊 đón khách quốc tế. " * 30).strip()
    chunks = _chunks(text)
    for chunk in chunks:
        assert chunk.content == text[chunk.char_start : chunk.char_end]
        span = build_span(ARTICLE, HASH, text, chunk.char_start, chunk.char_end)
        assert span.excerpt == chunk.content


def test_abbreviation_does_not_split_a_sentence() -> None:
    text = "Theo TP. HCM, dự án hoàn thành. Ông Nguyễn Văn A. phát biểu."
    starts = sentence_starts(text, 0, len(text))
    assert text[starts[1] :].startswith("Ông Nguyễn")
    assert len(starts) == 2


def test_article_without_paragraph_breaks_is_split_by_sentences() -> None:
    text = " ".join([VI_SENTENCE] * 30)
    chunks = _chunks(text)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.content.endswith("Hà Nội.")
        assert len(chunk.content) <= 1000


def test_text_without_any_punctuation_falls_back_to_whitespace_cuts() -> None:
    text = " ".join(["từ"] * 1200)
    chunks = _chunks(text)
    assert len(chunks) >= 3
    for chunk in chunks:
        assert not chunk.content.startswith(" ")
        assert chunk.content.split()[0] == "từ"
        assert len(chunk.content) <= 1000


def test_very_short_article_is_one_chunk() -> None:
    (chunk,) = _chunks("Ngắn.")
    assert (chunk.char_start, chunk.char_end) == (0, 5)


def test_blank_text_has_no_chunks() -> None:
    assert _chunks("  \n\n  ") == ()


def test_long_article_chunk_count_and_bounds_function_agree() -> None:
    text = _long_article(60)
    chunks = _chunks(text)
    assert len(chunks) > 10
    for chunk in chunks:
        assert paragraph_chunk_bounds(text, chunk.index) == (
            chunk.char_start,
            chunk.char_end,
        )
    assert paragraph_chunk_bounds(text, len(chunks)) is None
    assert paragraph_chunk_bounds(text, -1) is None


def test_chunker_version_is_part_of_identity() -> None:
    paragraph = search_chunk_id(ARTICLE, HASH, PARAGRAPH_CHUNKER_VERSION, 0)
    codepoint = search_chunk_id(ARTICLE, HASH, DEFAULT_POLICY.version, 0)
    assert paragraph != codepoint
    other = _chunks(_long_article(), ParagraphChunkingPolicy(version="paragraph-x"))
    assert other[0].fingerprint != _chunks(_long_article())[0].fingerprint


def test_metadata_is_attached_to_every_chunk() -> None:
    metadata = ChunkMetadata("Tiêu đề", "Fixture Wire", "fixture.example", START)
    chunks = chunk_paragraphs(ARTICLE, HASH, _long_article(), metadata=metadata)
    assert {chunk.metadata for chunk in chunks} == {metadata}


def test_baseline_codepoint_chunker_is_unchanged() -> None:
    document = article("x" * 2500)
    chunks = chunk_document(document)
    assert [(c.start_offset, c.end_offset) for c in chunks] == [
        (0, 1200),
        (1000, 2200),
        (2000, 2500),
    ]


@pytest.mark.parametrize(
    ("min_chars", "target_chars", "max_chars", "max_overlap_chars"),
    [
        (0, 900, 1000, 300),
        (700, 600, 1000, 300),
        (600, 1200, 1000, 300),
        (600, 900, 1000, 600),
    ],
)
def test_invalid_policies_are_rejected(
    min_chars: int, target_chars: int, max_chars: int, max_overlap_chars: int
) -> None:
    with pytest.raises(InvalidParagraphChunkingError):
        _ = ParagraphChunkingPolicy(
            min_chars=min_chars,
            target_chars=target_chars,
            max_chars=max_chars,
            max_overlap_chars=max_overlap_chars,
        )
