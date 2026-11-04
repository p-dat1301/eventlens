"""Regression tests for Elasticsearch bulk acknowledgement handling."""

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import timedelta
from typing import cast, override

import pytest

from backend.rag.application.ports.search_index import (
    BulkResult,
    RevisionWrite,
    SearchIndexError,
)
from backend.rag.application.ports.search_sync import ProjectionRevision
from backend.rag.application.use_cases.search_projection import ProjectRevision
from backend.rag.application.use_cases.search_sync import DrainOutbox
from backend.rag.domain.outbox import OutboxStatus
from backend.rag.domain.paragraph_chunking import PARAGRAPH_CHUNKER_VERSION
from backend.rag.domain.search_embedding import FAKE_BGE_M3
from backend.rag.domain.search_index import (
    GenerationStatus,
    IndexGeneration,
    SearchChunkDocument,
)
from backend.rag.infra.elasticsearch.client import ElasticsearchClient, Json
from backend.rag.infra.elasticsearch.indexes import ElasticsearchSearchIndex
from backend.rag.infra.embeddings.fake_bge_m3 import FakeBgeM3Embeddings
from search_support import (
    MemoryGenerations,
    MemoryOutbox,
    MemorySearchWorld,
    article,
)


class _BulkClient:
    """Typed fake for the tiny client surface exercised by the adapter."""

    def __init__(self, responses: Sequence[Json]) -> None:
        self.responses: list[Json] = list(responses)
        self.requests: list[tuple[str, str, Mapping[str, object] | None]] = []
        self.fences: dict[str, int] = {}
        self.chunk_actions: list[Mapping[str, object]] = []

    def bulk(self, _path: str, lines: Sequence[Mapping[str, object]]) -> Json:
        # Fence claims (doc_kind=fence) are acknowledged like Elasticsearch
        # external_gte versioning; chunk bulks pop the scripted response.
        if lines and lines[1].get("doc_kind") == "fence":
            items: list[Json] = []
            for action, _source in zip(lines[::2], lines[1::2], strict=True):
                meta = cast("dict[str, object]", action["index"])
                fence_id, version = str(meta["_id"]), cast("int", meta["version"])
                if self.fences.get(fence_id, -1) > version:
                    items.append({"index": {"status": 409}})
                else:
                    self.fences[fence_id] = version
                    items.append({"index": {"status": 201}})
            return {"items": items}
        self.chunk_actions.extend(lines[::2])
        return self.responses.pop(0)

    def _mget(self, body: Mapping[str, object] | None) -> Json:
        ids = cast("list[str]", (body or {})["ids"])
        return {"docs": [{"_id": i, "_version": self.fences[i]} for i in ids]}

    def request(
        self,
        method: str,
        path: str,
        body: Mapping[str, object] | None = None,
        *,
        allow: Sequence[int] = (),
    ) -> tuple[int, Json]:
        del allow
        self.requests.append((method, path, body))
        if path.endswith("/_mget"):
            return 200, self._mget(body)
        return 200, {"deleted": 2}


def _generation() -> IndexGeneration:
    return IndexGeneration(
        1,
        "eventlens-rag-search-test",
        PARAGRAPH_CHUNKER_VERSION,
        FAKE_BGE_M3.key,
        FAKE_BGE_M3.model,
        FAKE_BGE_M3.dimensions,
        GenerationStatus.ACTIVE,
    )


def _documents() -> tuple[SearchChunkDocument, ...]:
    document = article("Cảng Hải Phòng mở cửa trở lại.")
    projector = ProjectRevision(FakeBgeM3Embeddings(), None)
    return projector.execute_many(
        [
            ProjectionRevision(
                document.article_id, document, tombstoned=False, metadata=None
            )
        ],
        _generation(),
    )


def _adapter(fake: _BulkClient) -> ElasticsearchSearchIndex:
    return ElasticsearchSearchIndex(cast("ElasticsearchClient", cast("object", fake)))


@pytest.mark.parametrize(
    "response",
    [{}, {"items": []}, {"items": [{"index": {"status": 201}}]}],
)
def test_bulk_rejects_empty_missing_or_truncated_response(
    response: Json,
) -> None:
    fake = _BulkClient([response])
    submitted = _documents() * 2
    with pytest.raises(SearchIndexError, match="bulk response"):
        _ = _adapter(fake).bulk_index(_generation(), submitted)


def test_bulk_counts_item_failure_even_when_http_succeeds() -> None:
    documents = _documents() * 2
    fake = _BulkClient(
        [{"items": [{"index": {"status": 201}}, {"index": {"status": 409}}]}]
    )
    result = _adapter(fake).bulk_index(_generation(), documents[:2])
    assert result.indexed == 1
    assert result.failed == 1


def test_bulk_all_items_successful() -> None:
    documents = _documents()
    fake = _BulkClient([{"items": [{"index": {"status": 201}} for _ in documents]}])
    result = _adapter(fake).bulk_index(_generation(), documents)
    assert result == BulkResult(indexed=len(documents), deleted=0, failed=0)


def test_partial_response_can_succeed_on_retry() -> None:
    documents = _documents()
    fake = _BulkClient(
        [
            {"items": []},
            {"items": [{"index": {"status": 200}} for _ in documents]},
        ]
    )
    adapter = _adapter(fake)
    with pytest.raises(SearchIndexError):
        _ = adapter.bulk_index(_generation(), documents)
    result = adapter.bulk_index(_generation(), documents)
    assert result.failed == 0


def test_upsert_does_not_delete_old_chunks_when_write_fails() -> None:
    documents = _documents()
    fake = _BulkClient([{"items": [{"index": {"status": 503}} for _ in documents]}])
    result = _adapter(fake).upsert_revision(
        _generation(), documents[0].article_id, documents, version=1
    )
    assert result.failed == len(documents)
    assert fake.requests == []


@pytest.mark.parametrize(
    "items",
    [
        [
            {"index": {"status": 201}},
            {"index": {"status": 201}},
            {"index": {"status": 201}},
        ],
        [{"index": {"status": 201}}],
    ],
)
def test_bulk_rejects_item_count_mismatch_in_both_directions(
    items: list[Json],
) -> None:
    fake = _BulkClient([{"items": items}])
    with pytest.raises(SearchIndexError, match="bulk response"):
        _ = _adapter(fake).bulk_index(_generation(), _documents() * 2)


@pytest.mark.parametrize(
    "item",
    [{"index": {}}, {"index": {"status": "201"}}, {"index": None}, {}],
)
def test_bulk_counts_missing_action_or_status_as_failed(item: Json) -> None:
    fake = _BulkClient([{"items": [item]}])
    result = _adapter(fake).bulk_index(_generation(), _documents()[:1])
    assert result == BulkResult(indexed=0, deleted=0, failed=1)


# --- outbox behaviour on top of the Elasticsearch adapter ---------------------


class _OutboxEsClient(_BulkClient):
    """Fake client whose delete_by_query can fail and which records deletes."""

    delete_failures: bool

    def __init__(self, responses: Sequence[Json], *, delete_failures: bool) -> None:
        super().__init__(responses)
        self.delete_failures = delete_failures

    @override
    def request(
        self,
        method: str,
        path: str,
        body: Mapping[str, object] | None = None,
        *,
        allow: Sequence[int] = (),
    ) -> tuple[int, Json]:
        del allow
        self.requests.append((method, path, body))
        if path.endswith("/_mget"):
            return 200, self._mget(body)
        if "_delete_by_query" in path and self.delete_failures:
            return 200, {"deleted": 0, "failures": [{"cause": "shard"}]}
        return 200, {"deleted": 1}


def _drain(
    client: _OutboxEsClient,
) -> tuple[MemorySearchWorld, MemoryOutbox, DrainOutbox]:
    world = MemorySearchWorld()
    generations = MemoryGenerations(world)
    generation = generations.create(
        PARAGRAPH_CHUNKER_VERSION, FAKE_BGE_M3, "eventlens-rag-search"
    )
    _ = generations.activate(generation.generation_id)
    outbox = MemoryOutbox(world)
    _ = outbox.complete(outbox.claim(10, 60))  # activation event
    drain = DrainOutbox(
        outbox,
        world,
        _adapter(client),
        generations,
        ProjectRevision(FakeBgeM3Embeddings(), None),
    )
    return world, outbox, drain


def _ok(count: int) -> Json:
    return {"items": [{"index": {"status": 201}} for _ in range(count)]}


def test_failed_bulk_keeps_old_chunks_and_never_marks_the_event_done() -> None:
    client = _OutboxEsClient(
        [{"items": [{"index": {"status": 503}}]}], delete_failures=False
    )
    world, outbox, drain = _drain(client)
    world.persist(article("Cảng Hải Phòng mở cửa trở lại."))

    report = drain.execute()

    assert (report.done, report.retried) == (0, 1)
    assert outbox.counts()[OutboxStatus.DONE] == 1
    assert not any("_delete_by_query" in path for _, path, _ in client.requests)


def test_cleanup_failure_after_a_good_bulk_is_retried_not_done() -> None:
    client = _OutboxEsClient([_ok(1)], delete_failures=True)
    world, outbox, drain = _drain(client)
    world.persist(article("Cảng Hải Phòng mở cửa trở lại."))

    report = drain.execute()

    assert (report.done, report.retried) == (0, 1)
    counts = outbox.counts()
    assert counts[OutboxStatus.PENDING] == 1
    assert counts[OutboxStatus.DONE] == 1  # activation only


def test_event_is_done_only_after_bulk_and_cleanup_are_acknowledged() -> None:
    client = _OutboxEsClient([_ok(1)], delete_failures=False)
    world, outbox, drain = _drain(client)
    world.persist(article("Cảng Hải Phòng mở cửa trở lại."))

    report = drain.execute()

    assert (report.done, report.retried, report.failed) == (1, 0, 0)
    paths = [path for _, path, _ in client.requests]
    assert any("_delete_by_query" in path for path in paths)
    assert outbox.counts()[OutboxStatus.DONE] == 2


def test_partial_write_then_retry_completes_the_event() -> None:
    client = _OutboxEsClient(
        [{"items": [{"index": {"status": 429}}]}, _ok(1)], delete_failures=False
    )
    world, _, drain = _drain(client)
    world.persist(article("Cảng Hải Phòng mở cửa trở lại."))
    first = drain.execute()
    world.now += timedelta(minutes=5)
    second = drain.execute()
    assert (first.retried, second.done) == (1, 1)


class _OvertakingClient(_BulkClient):
    """A newer writer advances the fence between claim and re-check."""

    @override
    def request(
        self,
        method: str,
        path: str,
        body: Mapping[str, object] | None = None,
        *,
        allow: Sequence[int] = (),
    ) -> tuple[int, Json]:
        if path.endswith("/_mget"):
            for fence_id in list(self.fences):
                self.fences[fence_id] += 1
        return super().request(method, path, body, allow=allow)


def test_write_overtaken_in_flight_deletes_its_own_chunks_and_is_superseded() -> None:
    documents = _documents()
    client = _OvertakingClient([_ok(len(documents))])
    report = _adapter(client).write_revisions(
        _generation(),
        (RevisionWrite(documents[0].article_id, 5, documents),),
    )
    assert (report.applied, report.superseded, report.failed) == (0, 1, 0)
    deletes = [body for _, path, body in client.requests if "_delete_by_query" in path]
    assert len(deletes) == 1  # own chunks only; no cleanup of other versions
    assert '"projection_version": 5' in json.dumps(deletes[0])
    assert '"lt"' not in json.dumps(deletes[0])


def test_older_write_is_fenced_out_before_any_chunk_is_written() -> None:
    documents = _documents()
    client = _BulkClient([_ok(len(documents))])
    adapter = _adapter(client)
    article_id = documents[0].article_id
    newer = adapter.write_revisions(
        _generation(), (RevisionWrite(article_id, 9, documents),)
    )
    older = adapter.write_revisions(
        _generation(), (RevisionWrite(article_id, 3, documents),)
    )
    assert (newer.applied, older.superseded, older.indexed) == (1, 1, 0)
    assert client.responses == []  # the older write never reached the chunk bulk


@pytest.mark.parametrize("version", [0, 7])
def test_chunk_writes_carry_the_projection_version_to_elasticsearch(
    version: int,
) -> None:
    documents = tuple(replace(d, projection_version=version) for d in _documents())
    client = _BulkClient([_ok(len(documents))])

    result = _adapter(client).bulk_index(_generation(), documents)

    assert result.failed == 0
    assert client.chunk_actions == [
        {
            "index": {
                "_id": str(document.chunk_id),
                "version": version + 1,
                "version_type": "external_gte",
            }
        }
        for document in documents
    ]


def test_chunk_conflict_is_superseded_only_after_a_newer_fence_is_confirmed() -> None:
    documents = _documents()
    client = _OvertakingClient([{"items": [{"index": {"status": 409}}]}])

    report = _adapter(client).write_revisions(
        _generation(), (RevisionWrite(documents[0].article_id, 5, documents),)
    )

    assert (report.applied, report.superseded, report.failed, report.indexed) == (
        0,
        1,
        0,
        0,
    )


def test_chunk_conflict_without_a_newer_fence_fails_without_cleanup() -> None:
    documents = _documents()
    client = _BulkClient([{"items": [{"index": {"status": 409}}]}])

    report = _adapter(client).write_revisions(
        _generation(), (RevisionWrite(documents[0].article_id, 5, documents),)
    )

    assert report.failed == 1
    assert not any("_delete_by_query" in path for _, path, _ in client.requests)


class _OneOvertakenClient(_BulkClient):
    """Only the first article is overtaken in a multi-article page."""

    @override
    def request(
        self,
        method: str,
        path: str,
        body: Mapping[str, object] | None = None,
        *,
        allow: Sequence[int] = (),
    ) -> tuple[int, Json]:
        if path.endswith("/_mget"):
            first_fence = next(iter(self.fences))
            self.fences[first_fence] += 1
        return super().request(method, path, body, allow=allow)


@pytest.mark.parametrize("second_status", [201, 409, 503])
def test_chunk_conflicts_are_attributed_to_their_article(second_status: int) -> None:
    first, second = _documents(), _documents()
    client = _OneOvertakenClient(
        [{"items": [{"index": {"status": 409}}, {"index": {"status": second_status}}]}]
    )

    report = _adapter(client).write_revisions(
        _generation(),
        (
            RevisionWrite(first[0].article_id, 5, first),
            RevisionWrite(second[0].article_id, 5, second),
        ),
    )

    if second_status == 201:
        assert (report.applied, report.superseded, report.failed) == (1, 1, 0)
    else:
        assert report.failed > 0
        assert not any("_delete_by_query" in path for _, path, _ in client.requests)
