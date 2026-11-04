"""Deterministic paragraph-aware chunking (``paragraph-900-1.v1``).

Adjacent paragraphs of the NFC ``normalized_content`` are packed into chunks of
roughly 600-1,000 code points. A chunk never ends inside a sentence when a
sentence boundary is available; a paragraph longer than the maximum is split at
sentence boundaries, and a sentence longer than the maximum at whitespace (a
hard code-point cut only when no whitespace exists). Consecutive chunks share
one short trailing sentence (or a short trailing paragraph) as overlap.

Every chunk is the exact slice ``normalized_content[char_start:char_end]``, so
citations cut by these offsets match the stored text. The baseline chunker
``codepoint-1200-200.v1`` (``domain/chunking.py``) stays unchanged.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from hashlib import sha256
from typing import Final
from uuid import UUID

PARAGRAPH_CHUNKER_VERSION: Final = "paragraph-900-1.v1"

# A paragraph break is a newline run (optionally with horizontal whitespace).
_PARAGRAPH_BREAK = re.compile(r"[ \t\f\v]*(?:\r?\n[ \t\f\v]*)+")
# Sentence end: terminal punctuation, optional closing quotes/brackets, then
# whitespace followed by a non-space character.
_SENTENCE_END = re.compile(r"[.!?…]+[\"'\u201d\u2019\u00bb)\]]*(?=\s+\S)")
# Tokens that end with a period but rarely end a sentence (Vietnamese and
# English news abbreviations). Compared case-sensitively on the last token.
_ABBREVIATIONS: Final = frozenset(
    {
        "TP",
        "Q",
        "P",
        "TS",
        "ThS",
        "PGS",
        "GS",
        "BS",
        "KS",
        "NXB",
        "Mr",
        "Mrs",
        "Ms",
        "Dr",
        "St",
        "No",
        "Tr",
        "vs",
        "etc",
        "v.v",
    }
)
_LAST_TOKEN = re.compile(r"(\S+)$")


class InvalidParagraphChunkingError(ValueError):
    """Chunk bounds must satisfy 0 < min <= target <= max and overlap < min."""

    def __init__(self, detail: str) -> None:
        """Keep the violated bound in the message."""
        super().__init__(f"invalid paragraph chunking policy: {detail}")


@dataclass(frozen=True, slots=True)
class ParagraphChunkingPolicy:
    """Bounds in Unicode code points; the version is part of chunk identity."""

    min_chars: int = 600
    target_chars: int = 900
    max_chars: int = 1000
    max_overlap_chars: int = 300
    version: str = PARAGRAPH_CHUNKER_VERSION

    def __post_init__(self) -> None:
        """Reject bounds that could stall or overlap a whole chunk."""
        if not 0 < self.min_chars <= self.target_chars <= self.max_chars:
            msg = "0 < min_chars <= target_chars <= max_chars"
            raise InvalidParagraphChunkingError(msg)
        if not 0 <= self.max_overlap_chars < self.min_chars:
            msg = "0 <= max_overlap_chars < min_chars"
            raise InvalidParagraphChunkingError(msg)


DEFAULT_PARAGRAPH_POLICY: Final = ParagraphChunkingPolicy()
# A unit this short joins the current chunk even past the target size.
_SHORT: Final = 150


@dataclass(frozen=True, slots=True)
class ChunkMetadata:
    """Source metadata copied onto every chunk of one revision."""

    title: str
    source_name: str
    source_domain: str
    published_at: datetime | None


@dataclass(frozen=True, slots=True)
class ParagraphChunk:
    """Exact code-point slice of one revision plus paragraph provenance."""

    article_id: UUID
    content_hash: str
    chunker_version: str
    index: int
    char_start: int
    char_end: int
    paragraph_start: int
    paragraph_end: int
    content: str
    fingerprint: str
    metadata: ChunkMetadata | None


@dataclass(frozen=True, slots=True)
class _Unit:
    """Smallest packable range: a paragraph or a piece of one."""

    start: int
    end: int
    paragraph: int
    sentence_starts: tuple[int, ...]


def paragraph_spans(text: str) -> tuple[tuple[int, int], ...]:
    """Trimmed ``(start, end)`` of every non-blank paragraph, in order."""
    spans: list[tuple[int, int]] = []
    cursor = 0
    for match in _PARAGRAPH_BREAK.finditer(text):
        spans.append((cursor, match.start()))
        cursor = match.end()
    spans.append((cursor, len(text)))
    trimmed: list[tuple[int, int]] = []
    for start, end in spans:
        segment = text[start:end]
        left = len(segment) - len(segment.lstrip())
        right = len(segment.rstrip())
        if right > left:
            trimmed.append((start + left, start + right))
    return tuple(trimmed)


def _is_abbreviation(text: str, start: int, end: int) -> bool:
    match = _LAST_TOKEN.search(text, start, end)
    if match is None:
        return False
    token = match.group(1).rstrip(".")
    return token in _ABBREVIATIONS or (len(token) == 1 and token.isupper())


def sentence_starts(text: str, start: int, end: int) -> tuple[int, ...]:
    """Start offsets of the sentences inside ``text[start:end]`` (first = start)."""
    starts = [start]
    for match in _SENTENCE_END.finditer(text, start, end):
        if _is_abbreviation(text, starts[-1], match.start() + 1):
            continue
        following = match.end()
        while following < end and text[following].isspace():
            following += 1
        if start < following < end:
            starts.append(following)
    return tuple(starts)


def _whitespace_cut(text: str, start: int, end: int, limit: int) -> int:
    """Largest whitespace-delimited cut in ``(start, start+limit]``."""
    hard = min(end, start + limit)
    if hard >= end:
        return end
    for position in range(hard, start, -1):
        if text[position - 1].isspace() and not text[position].isspace():
            return position
    return hard


def _trim(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _split_long(
    text: str, start: int, end: int, paragraph: int, limit: int
) -> list[_Unit]:
    """Split one over-long paragraph at sentences, then at whitespace."""
    starts = sentence_starts(text, start, end)
    bounds = [*starts, end]
    sentences = [(bounds[i], bounds[i + 1]) for i in range(len(starts))]
    units: list[_Unit] = []
    group_start, group_end = sentences[0][0], sentences[0][0]
    group_sentences: list[int] = []
    for s_start, s_end in sentences:
        if s_end - group_start <= limit:
            group_end = s_end
            group_sentences.append(s_start)
            continue
        if group_end > group_start:
            a, b = _trim(text, group_start, group_end)
            units.append(_Unit(a, b, paragraph, tuple(group_sentences)))
        cursor = s_start
        pieces = -(-(s_end - cursor) // limit)
        piece = -(-(s_end - cursor) // pieces)
        while s_end - cursor > limit:
            cut = _whitespace_cut(text, cursor, s_end, piece)
            a, b = _trim(text, cursor, cut)
            if b > a:
                units.append(_Unit(a, b, paragraph, (a,)))
            cursor = cut
        group_start, group_end, group_sentences = cursor, s_end, [cursor]
    if group_end > group_start:
        a, b = _trim(text, group_start, group_end)
        if b > a:
            units.append(_Unit(a, b, paragraph, tuple(group_sentences)))
    return units


def _units(text: str, policy: ParagraphChunkingPolicy) -> list[_Unit]:
    units: list[_Unit] = []
    for paragraph, (start, end) in enumerate(paragraph_spans(text)):
        if end - start <= policy.max_chars:
            units.append(
                _Unit(start, end, paragraph, sentence_starts(text, start, end))
            )
        else:
            units.extend(_split_long(text, start, end, paragraph, policy.max_chars))
    return units


def _overlap_start(
    text: str, units: list[_Unit], first: int, last: int, limit: int
) -> int | None:
    """Start of the trailing sentence/short paragraph reused by the next chunk."""
    tail = units[last]
    candidates = [s for s in tail.sentence_starts if s > units[first].start]
    if candidates:
        start = candidates[-1]
        if tail.end - start <= limit and text[start : tail.end].strip():
            return start
    if last > first and tail.end - tail.start <= limit:
        return tail.start
    return None


def chunk_spans(
    text: str, policy: ParagraphChunkingPolicy = DEFAULT_PARAGRAPH_POLICY
) -> tuple[tuple[int, int, int, int], ...]:
    """``(char_start, char_end, paragraph_start, paragraph_end)`` per chunk."""
    units = _units(text, policy)
    spans: list[tuple[int, int, int, int]] = []
    first = 0
    # Overlap carried into the next chunk: (start offset, its paragraph).
    lead: tuple[int, int] | None = None
    while first < len(units):
        start, paragraph = (
            (units[first].start, units[first].paragraph) if lead is None else lead
        )
        last = first
        while (
            last + 1 < len(units)
            and units[last + 1].end - start <= policy.max_chars
            and (
                units[last].end - start < policy.target_chars
                or units[last + 1].end - units[last + 1].start < _SHORT
            )
        ):
            last += 1
        spans.append((start, units[last].end, paragraph, units[last].paragraph))
        if last + 1 >= len(units):
            break
        overlap = _overlap_start(text, units, first, last, policy.max_overlap_chars)
        lead = None
        if (
            overlap is not None
            and overlap > start
            and units[last + 1].end - overlap <= policy.max_chars
        ):
            lead = (overlap, units[last].paragraph)
        first = last + 1
    return tuple(spans)


def _fingerprint(content_hash: str, version: str, index: int, content: str) -> str:
    digest = sha256(f"{content_hash}:{version}:{index}:{content}".encode()).hexdigest()
    return f"sha256:{digest}"


def chunk_paragraphs(
    article_id: UUID,
    content_hash: str,
    text: str,
    *,
    metadata: ChunkMetadata | None = None,
    policy: ParagraphChunkingPolicy = DEFAULT_PARAGRAPH_POLICY,
) -> tuple[ParagraphChunk, ...]:
    """Paragraph chunks of one revision's ``normalized_content``."""
    return tuple(
        ParagraphChunk(
            article_id=article_id,
            content_hash=content_hash,
            chunker_version=policy.version,
            index=index,
            char_start=start,
            char_end=end,
            paragraph_start=p_start,
            paragraph_end=p_end,
            content=text[start:end],
            fingerprint=_fingerprint(
                content_hash, policy.version, index, text[start:end]
            ),
            metadata=metadata,
        )
        for index, (start, end, p_start, p_end) in enumerate(
            _cached_spans(text, policy)
        )
    )


@lru_cache(maxsize=256)
def _cached_spans(
    text: str, policy: ParagraphChunkingPolicy
) -> tuple[tuple[int, int, int, int], ...]:
    return chunk_spans(text, policy)


def paragraph_chunk_bounds(
    text: str, index: int, policy: ParagraphChunkingPolicy = DEFAULT_PARAGRAPH_POLICY
) -> tuple[int, int] | None:
    """Offsets chunk ``index`` has in ``text``; ``None`` when it does not exist."""
    spans = _cached_spans(text, policy)
    if not 0 <= index < len(spans):
        return None
    start, end, _, _ = spans[index]
    return start, end
