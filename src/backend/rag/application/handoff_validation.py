"""Validate a handoff batch (``articles.jsonl`` + ``manifest.json``) in memory.

Pure: the caller reads the files and passes their bytes. The report carries IDs,
counts, line numbers and error codes only, never article text, so it can be
committed or shared. ``errors`` block import; ``warnings`` are recorded facts
that the benchmark report must disclose (for example invisible format
characters or HTML entities left by the parser).
"""

import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from itertools import combinations
from typing import cast
from urllib.parse import urlsplit

from pydantic import TypeAdapter, ValidationError

from backend.rag.domain.article_document import normalized_content_hash
from backend.rag.domain.handoff import HandoffArticle, HandoffManifest

NEAR_DUPLICATE_SHINGLE = 5
NEAR_DUPLICATE_JACCARD = 0.5
NEAR_DUPLICATE_CONTAINMENT = 0.8
# Shingles shared by more documents than this are boilerplate, not evidence of
# duplication; skipping them keeps candidate generation near-linear.
SHINGLE_DOCUMENT_CAP = 20

_WORD = re.compile(r"\w+")
_HTML_TAG = re.compile(r"</?[A-Za-z][A-Za-z0-9]*(\s[^<>]*)?/?>")
_HTML_ENTITY = re.compile(r"&(#\d+|#x[0-9A-Fa-f]+|[A-Za-z]{2,8});")
_ALLOWED_CONTROLS = frozenset("\n\t")
_JSON: TypeAdapter[object] = TypeAdapter(object)


@dataclass(frozen=True, slots=True)
class Finding:
    """One problem: stable code, 1-based line (0 = batch level), detail."""

    code: str
    line: int
    detail: str


@dataclass(frozen=True, slots=True)
class NearDuplicate:
    """Article pair whose word 5-gram sets overlap above the frozen thresholds."""

    article_id: str
    other_article_id: str
    jaccard: float
    containment: float


@dataclass(slots=True)
class HandoffReport:
    """Validation outcome; ``ok`` means no error-level finding."""

    export_id: str | None
    articles_sha256: str
    articles_bytes: int
    line_count: int
    valid_records: int
    errors: list[Finding] = field(default_factory=list)
    warnings: list[Finding] = field(default_factory=list)
    near_duplicates: list[NearDuplicate] = field(default_factory=list)
    counts: dict[str, dict[str, int]] = field(default_factory=dict)
    hash_mapping: dict[str, int] = field(default_factory=dict)
    records: list[HandoffArticle] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """No error-level finding."""
        return not self.errors

    def to_json(self) -> dict[str, object]:
        """Text-free summary (records are omitted; they hold full content)."""
        return {
            "schema": "handoff-validation-report.v1",
            "ok": self.ok,
            "export_id": self.export_id,
            "articles_sha256": self.articles_sha256,
            "articles_bytes": self.articles_bytes,
            "line_count": self.line_count,
            "valid_records": self.valid_records,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "errors": [vars_of(item) for item in self.errors],
            "warning_summary": dict(
                sorted(Counter(item.code for item in self.warnings).items())
            ),
            "warnings": [vars_of(item) for item in self.warnings],
            "near_duplicates": [vars_of(item) for item in self.near_duplicates],
            "counts": self.counts,
            "hash_mapping": self.hash_mapping,
        }


def vars_of(item: Finding | NearDuplicate) -> dict[str, object]:
    """Plain dict of a slotted dataclass."""
    return {name: getattr(item, name) for name in item.__slots__}


def _validation_codes(error: ValidationError) -> str:
    # Field locations and error types only; input values may hold article text.
    return ";".join(
        f"{'.'.join(str(part) for part in item['loc'])}:{item['type']}"
        for item in error.errors(include_input=False, include_url=False)
    )


def _format_characters(text: str) -> Counter[str]:
    found: Counter[str] = Counter()
    for char in text:
        category = unicodedata.category(char)
        if (
            (category == "Cc" and char not in _ALLOWED_CONTROLS)
            or category
            in {
                "Cf",
                "Co",
                "Cs",
            }
            or (category == "Zs" and char != " ")
        ):
            found[f"U+{ord(char):04X}"] += 1
    return found


def _shingles(text: str) -> frozenset[str]:
    words = _WORD.findall(text.casefold())
    size = NEAR_DUPLICATE_SHINGLE
    return frozenset(
        " ".join(words[index : index + size]) for index in range(len(words) - size + 1)
    )


def near_duplicates(records: list[HandoffArticle]) -> list[NearDuplicate]:
    """Cross-article pairs above the Jaccard or containment threshold."""
    sets = [_shingles(record.content) for record in records]
    postings: defaultdict[str, list[int]] = defaultdict(list)
    for index, shingles in enumerate(sets):
        for shingle in shingles:
            postings[shingle].append(index)
    shared: Counter[tuple[int, int]] = Counter()
    for members in postings.values():
        if 1 < len(members) <= SHINGLE_DOCUMENT_CAP:
            shared.update(combinations(members, 2))
    pairs: list[NearDuplicate] = []
    for left, right in shared:
        if records[left].article_id == records[right].article_id:
            continue
        a, b = sets[left], sets[right]
        overlap = len(a & b)
        jaccard = overlap / len(a | b)
        containment = overlap / min(len(a), len(b))
        if jaccard >= NEAR_DUPLICATE_JACCARD or containment >= (
            NEAR_DUPLICATE_CONTAINMENT
        ):
            first, second = sorted(
                (str(records[left].article_id), str(records[right].article_id))
            )
            pairs.append(
                NearDuplicate(first, second, round(jaccard, 4), round(containment, 4))
            )
    return sorted(pairs, key=lambda pair: (pair.article_id, pair.other_article_id))


@dataclass(slots=True)
class _Checker:
    report: HandoffReport
    manifest: HandoffManifest | None
    seen_ids: dict[object, int] = field(default_factory=dict)
    seen_versions: dict[object, int] = field(default_factory=dict)
    seen_urls: dict[object, int] = field(default_factory=dict)
    seen_titles: dict[object, int] = field(default_factory=dict)

    def error(self, code: str, line: int, detail: str = "") -> None:
        self.report.errors.append(Finding(code, line, detail))

    def warn(self, code: str, line: int, detail: str = "") -> None:
        self.report.warnings.append(Finding(code, line, detail))

    def unique(
        self,
        table: dict[object, int],
        key: object,
        line: int,
        code: str,
        *,
        fatal: bool = True,
    ) -> None:
        first = table.get(key)
        if first is None:
            table[key] = line
            return
        report = self.error if fatal else self.warn
        report(code, line, f"first seen on line {first}")

    def record(self, record: HandoffArticle, line: int) -> None:
        self.unique(self.seen_ids, record.article_id, line, "duplicate_article_id")
        self.unique(
            self.seen_versions,
            record.article_version_id,
            line,
            "duplicate_article_version_id",
        )
        self.unique(self.seen_urls, str(record.canonical_url), line, "duplicate_url")
        self.unique(
            self.seen_titles, record.title, line, "duplicate_title", fatal=False
        )
        self.times(record, line)
        self.text(record, line)
        self.url(record, line)
        if (
            self.manifest is not None
            and self.manifest.selection.current_only
            and not record.is_current
        ):
            self.error("not_current_in_current_only_batch", line)
        if self.manifest is not None and record.quality_status not in (
            self.manifest.selection.quality_status
        ):
            self.error("quality_status_outside_selection", line, record.quality_status)

    def times(self, record: HandoffArticle, line: int) -> None:
        manifest = self.manifest
        if record.published_at is not None and record.published_at > (
            record.fetched_at
        ):
            self.warn("published_after_fetched", line)
        if (
            record.updated_at_source is not None
            and record.published_at is not None
            and record.updated_at_source < record.published_at
        ):
            self.warn("updated_before_published", line)
        if manifest is None:
            return
        for name, value in (
            ("published_at", record.published_at),
            ("fetched_at", record.fetched_at),
            ("updated_at_source", record.updated_at_source),
        ):
            if value is not None and value > manifest.exported_at:
                self.error("timestamp_after_export", line, name)

    def text(self, record: HandoffArticle, line: int) -> None:
        for name, value in (("title", record.title), ("content", record.content)):
            if unicodedata.normalize("NFC", value) != value:
                self.warn("not_nfc", line, name)
            chars = _format_characters(value)
            if chars:
                detail = ",".join(f"{code}x{n}" for code, n in sorted(chars.items()))
                self.warn("invisible_or_format_characters", line, f"{name}:{detail}")
            entities = _HTML_ENTITY.findall(value)
            if entities:
                self.warn("html_entity", line, f"{name}:{len(entities)}")
            if _HTML_TAG.search(value):
                self.warn("html_tag", line, name)
        if record.content != record.content.strip():
            self.warn("content_edge_whitespace", line)
        words = len(record.content.split())
        if words != record.word_count:
            self.warn("word_count_mismatch", line, f"declared={record.word_count}")
        if record.title.strip() and record.content.startswith(record.title):
            self.warn("content_starts_with_title", line)

    def url(self, record: HandoffArticle, line: int) -> None:
        parts = urlsplit(str(record.canonical_url))
        host = (parts.hostname or "").lower()
        domain = record.source_domain
        if parts.scheme != "https":
            self.warn("non_https_url", line, parts.scheme)
        if host != domain and not host.endswith(f".{domain}"):
            self.error("url_host_outside_source_domain", line, host)
        if parts.fragment:
            self.warn("url_fragment", line)


def _bounds(values: list[datetime]) -> tuple[datetime | None, datetime | None]:
    return (min(values), max(values)) if values else (None, None)


def _compare_manifest(checker: _Checker, records: list[HandoffArticle]) -> None:
    manifest = checker.manifest
    report = checker.report
    derived = {
        "source_counts": Counter(record.source_domain for record in records),
        "language_counts": Counter(record.language for record in records),
        "parse_version_counts": Counter(record.parse_version for record in records),
        "quality_status_counts": Counter(record.quality_status for record in records),
        "normalization_version_counts": Counter(
            record.normalization_version for record in records
        ),
        "parse_normalization_pairs": Counter(
            f"{record.parse_version}/{record.normalization_version}"
            for record in records
        ),
    }
    report.counts = {
        name: dict(sorted(counter.items())) for name, counter in derived.items()
    }
    if manifest is None:
        return
    for name in (
        "source_counts",
        "language_counts",
        "parse_version_counts",
        "quality_status_counts",
    ):
        declared = cast("dict[str, int]", getattr(manifest, name))
        if dict(derived[name]) != declared:
            checker.error("manifest_count_mismatch", 0, name)
    missing = sum(1 for record in records if record.published_at is None)
    if missing != manifest.missing_published_at_count:
        checker.error("manifest_count_mismatch", 0, "missing_published_at_count")
    published = [r.published_at for r in records if r.published_at is not None]
    fetched = [record.fetched_at for record in records]
    expected = (
        (
            "published_at",
            _bounds(published),
            (manifest.published_at_min, manifest.published_at_max),
        ),
        (
            "fetched_at",
            _bounds(fetched),
            (manifest.fetched_at_min, manifest.fetched_at_max),
        ),
    )
    for name, actual, declared in expected:
        if actual != declared:
            checker.error("manifest_range_mismatch", 0, name)


def _load_manifest(
    report: HandoffReport, manifest_bytes: bytes | None
) -> HandoffManifest | None:
    if manifest_bytes is None:
        report.errors.append(Finding("manifest_missing", 0, ""))
        return None
    try:
        return HandoffManifest.model_validate_json(manifest_bytes)
    except ValidationError as error:
        report.errors.append(Finding("manifest_schema", 0, _validation_codes(error)))
        return None


def _check_file(checker: _Checker, articles: bytes, line_count: int) -> None:
    manifest = checker.manifest
    if manifest is not None:
        checker.report.export_id = manifest.export_id
        if manifest.articles_sha256 != checker.report.articles_sha256:
            checker.error("articles_sha256_mismatch", 0)
        if manifest.articles_bytes != len(articles):
            checker.error("articles_bytes_mismatch", 0)
        if manifest.record_count != line_count:
            checker.error("record_count_mismatch", 0)
    if articles.startswith(b"\xef\xbb\xbf"):
        checker.error("utf8_bom", 1)
    if articles and not articles.endswith(b"\n"):
        checker.warn("missing_final_newline", line_count)


def _parse_line(checker: _Checker, raw: bytes, number: int) -> HandoffArticle | None:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        checker.error("invalid_utf8", number)
        return None
    if not text.strip():
        checker.error("blank_line", number)
        return None
    try:
        payload = _JSON.validate_json(text)
    except ValidationError:
        checker.error("invalid_json", number)
        return None
    if not isinstance(payload, dict):
        checker.error("not_an_object", number)
        return None
    try:
        return HandoffArticle.model_validate_json(text)
    except ValidationError as error:
        checker.error("schema", number, _validation_codes(error))
        return None


def validate_handoff(articles: bytes, manifest_bytes: bytes | None) -> HandoffReport:
    """Check checksum, schema, identity, time, text, URL and manifest counts."""
    lines = articles.split(b"\n")
    if lines and lines[-1] == b"":
        _ = lines.pop()
    report = HandoffReport(
        export_id=None,
        articles_sha256=sha256(articles).hexdigest(),
        articles_bytes=len(articles),
        line_count=len(lines),
        valid_records=0,
    )
    checker = _Checker(report=report, manifest=_load_manifest(report, manifest_bytes))
    _check_file(checker, articles, len(lines))
    records: list[HandoffArticle] = []
    for number, raw in enumerate(lines, start=1):
        record = _parse_line(checker, raw, number)
        if record is not None:
            checker.record(record, number)
            records.append(record)
    report.valid_records = len(records)
    _compare_manifest(checker, records)
    upstream_equal = sum(
        1
        for record in records
        if normalized_content_hash(unicodedata.normalize("NFC", record.content))
        == f"sha256:{record.content_hash}"
    )
    report.hash_mapping = {
        "records": len(records),
        "upstream_equals_rag_nfc_sha256": upstream_equal,
        "upstream_differs_from_rag_nfc_sha256": len(records) - upstream_equal,
    }
    report.near_duplicates = near_duplicates(records)
    report.records = records
    return report
