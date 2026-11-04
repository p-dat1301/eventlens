"""Conversations and durable runs on real PostgreSQL through the HTTP API.

The rag-api app is composed over a per-test schema loaded with the 30 fixture
articles; runs are executed by the worker-side ``QueryRunExecutor`` with the
deterministic fixture synthesizer selected explicitly
(``EVENTLENS_RAG_ANSWER_PROVIDER=fixture``). No provider is contacted.
"""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import cast
from uuid import UUID, uuid4

import httpx2
import psycopg
import pytest

from backend.rag.api_main import create_app
from backend.rag.application.ports.conversations import NewRun
from backend.rag.application.use_cases.query_runs import QueryRunExecutor
from backend.rag.composition import (
    FIXTURES,
    build_run_executor,
    news_runtime,
    runtime_from_environment,
)
from backend.rag.infra.postgres.article_catalog import PostgresArticleCatalog
from backend.rag.infra.postgres.engine import rag_engine
from backend.rag.infra.postgres.store import PostgresRagStore
from backend.rag.infra.retrieval.memory_retrieval import load_fixture_articles
from briefing_support import NO_MATCH, QUESTION, revise

pytestmark = pytest.mark.anyio
Json = dict[str, object]


@pytest.fixture
def chat_url(retrieval_url: str) -> str:
    store = PostgresRagStore(rag_engine(retrieval_url))
    for document in load_fixture_articles(FIXTURES):
        store.persist_article(document)
    return retrieval_url


def _env(url: str, answers: str = "fixture") -> dict[str, str]:
    return {
        "EVENTLENS_API_RAG_DATABASE_URL": url,
        "EVENTLENS_RAG_ANSWER_PROVIDER": answers,
        "EVENTLENS_RAG_INTERNAL_SERVICE_TOKEN": "t" * 40,
    }


def _client(url: str, answers: str = "fixture") -> httpx2.AsyncClient:
    env = _env(url, answers)
    runtime = runtime_from_environment(env)
    app = create_app(runtime, news_runtime(env, runtime))
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://testserver"
    )


def _executor(url: str) -> QueryRunExecutor:
    return build_run_executor(url, _env(url))


def _drain(url: str, executor: QueryRunExecutor | None = None) -> list[str]:
    worker = executor or _executor(url)
    results: list[str] = []
    while claimed := worker.claim("test-worker", 1):
        results.append(worker.execute(claimed[0]).status)
    return results


async def _session(client: httpx2.AsyncClient) -> dict[str, str]:
    response = await client.post("/api/v1/sessions")
    assert response.status_code == 201, response.text
    token = cast("str", cast("Json", response.json())["session_token"])
    assert response.cookies.get("eventlens_session") == token
    return {"Authorization": f"Bearer {token}"}


async def _conversation(
    client: httpx2.AsyncClient, owner: dict[str, str], scope: Json
) -> str:
    response = await client.post(
        "/api/v1/conversations", json={"scope": scope}, headers=owner
    )
    assert response.status_code == 201, response.text
    return cast("str", cast("Json", response.json())["conversation_id"])


async def _submit(
    client: httpx2.AsyncClient,
    owner: dict[str, str],
    conversation: str,
    question: str = QUESTION,
    key: str | None = None,
) -> httpx2.Response:
    headers = owner | ({"Idempotency-Key": key} if key else {})
    return await client.post(
        f"/api/v1/conversations/{conversation}/runs",
        json={"question": question},
        headers=headers,
    )


async def _run(client: httpx2.AsyncClient, owner: dict[str, str], run_id: str) -> Json:
    response = await client.get(f"/api/v1/runs/{run_id}", headers=owner)
    assert response.status_code == 200, response.text
    return cast("Json", response.json())


def _id(response: httpx2.Response) -> str:
    assert response.status_code == 202, response.text
    return cast("str", cast("Json", response.json())["run_id"])


ALL: Json = {"type": "all"}
EXPIRE_LEASES = """UPDATE rag_work_jobs SET lease_expires_at = now() - interval '1 s'
WHERE status = 'PROCESSING'"""
EXPIRE_REFS = (
    "UPDATE rag_source_references SET expires_at = created_at + interval '1 us'"
)


async def test_answer_is_stored_with_scope_versions_and_citations(
    chat_url: str,
) -> None:
    async with _client(chat_url) as client:
        owner = await _session(client)
        conversation = await _conversation(client, owner, ALL)
        run_id = _id(await _submit(client, owner, conversation))
        queued = await _run(client, owner, run_id)
        assert _drain(chat_url) == ["completed"]
        run = await _run(client, owner, run_id)
        history = await client.get(
            f"/api/v1/conversations/{conversation}/messages", headers=owner
        )

    assert queued["status"] == "queued"
    assert run["status"] == "completed"
    assert run["outcome"] == "answered"
    citations = cast("list[Json]", run["citations"])
    versions = cast("dict[str, str]", run["source_versions"])
    assert citations
    assert all(
        versions[str(c["article_id"])] == c["article_version_id"] for c in citations
    )
    assert cast("Json", run["effective_scope"])["type"] == "all"
    roles = [
        m["role"] for m in cast("list[Json]", cast("Json", history.json())["items"])
    ]
    assert roles == ["user", "assistant"]


async def test_no_evidence_completes_as_abstained(chat_url: str) -> None:
    async with _client(chat_url) as client:
        owner = await _session(client)
        conversation = await _conversation(client, owner, ALL)
        run_id = _id(await _submit(client, owner, conversation, NO_MATCH))
        _ = _drain(chat_url)
        run = await _run(client, owner, run_id)
    assert (run["status"], run["outcome"]) == ("completed", "abstained")
    assert run["error"] is None


async def test_conversations_runs_and_citations_are_owner_only(chat_url: str) -> None:
    async with _client(chat_url) as client:
        alice, bob = await _session(client), await _session(client)
        conversation = await _conversation(client, alice, ALL)
        run_id = _id(await _submit(client, alice, conversation))
        _ = _drain(chat_url)
        citation = cast("list[Json]", (await _run(client, alice, run_id))["citations"])[
            0
        ]
        probes = [
            await client.get(
                f"/api/v1/conversations/{conversation}/messages", headers=bob
            ),
            await client.get(f"/api/v1/runs/{run_id}", headers=bob),
            await client.post(f"/api/v1/runs/{run_id}/cancel", headers=bob),
            await _submit(client, bob, conversation),
            await client.post(str(citation["source_ref_url"]), headers=bob),
            await client.delete(f"/api/v1/conversations/{conversation}", headers=bob),
        ]
        listed = await client.get("/api/v1/conversations", headers=bob)
        client.cookies.clear()  # no session at all: a forged header is not identity
        forged = await client.get(
            f"/api/v1/runs/{run_id}", headers={"X-User-ID": "alice"}
        )
        still_there = await _run(client, alice, run_id)

    assert [p.status_code for p in probes] == [404] * len(probes)
    assert cast("Json", listed.json())["items"] == []
    assert forged.status_code == 401
    assert still_there["status"] == "completed"


async def test_idempotent_retry_conflicting_body_and_one_active_run(
    chat_url: str,
) -> None:
    async with _client(chat_url) as client:
        owner = await _session(client)
        conversation = await _conversation(client, owner, ALL)
        first = await _submit(client, owner, conversation, key="k-1")
        replay = await _submit(client, owner, conversation, key="k-1")
        conflict = await _submit(client, owner, conversation, "khác", key="k-1")
        second = await _submit(client, owner, conversation, "khác", key="k-2")

    assert _id(first) == _id(replay)
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert conflict.status_code == 409
    assert cast("Json", conflict.json())["error"] == {
        "code": "idempotency_key_reused",
        "reason": None,
        "request_id": conflict.headers["X-Request-ID"],
    }
    assert second.status_code == 409
    assert (
        cast("Json", cast("Json", second.json())["error"])["code"] == "run_in_progress"
    )


async def test_concurrent_submits_create_exactly_one_run(chat_url: str) -> None:
    async with _client(chat_url) as client:
        owner = await _session(client)
        conversation = await _conversation(client, owner, ALL)
    executor = _executor(chat_url)
    conversations = executor.store
    principal_owner = _owner_of(chat_url, conversation)
    barrier = Barrier(6)

    def submit(_: int) -> str:
        _ = barrier.wait()
        try:
            result = conversations.submit(
                principal_owner,
                UUID(conversation),
                NewRun(uuid4(), uuid4(), uuid4(), "q", "same", "0" * 64, 2, 600.0),
            )
        except Exception as error:  # noqa: BLE001 - count failures by type
            return type(error).__name__
        return f"created={result.created}:{result.run.run_id}"

    with ThreadPoolExecutor(6) as pool:
        outcomes = list(pool.map(submit, range(6)))
    created = [o for o in outcomes if o.startswith("created=True")]
    replayed = {o.split(":")[1] for o in outcomes if o.startswith("created=False")}
    assert len(created) == 1
    assert replayed <= {created[0].split(":")[1]}
    with psycopg.connect(chat_url) as connection:
        row = connection.execute("SELECT count(*) FROM rag_query_runs").fetchone()
    assert row == (1,)


def _owner_of(url: str, conversation: str) -> UUID:
    with psycopg.connect(url) as connection:
        row = connection.execute(
            "SELECT owner_id FROM rag_conversations WHERE conversation_id = %s",
            (conversation,),
        ).fetchone()
    assert row is not None
    return cast("UUID", row[0])


async def test_worker_crash_is_recovered_without_losing_the_run(chat_url: str) -> None:
    async with _client(chat_url) as client:
        owner = await _session(client)
        conversation = await _conversation(client, owner, ALL)
        run_id = _id(await _submit(client, owner, conversation))
        crashed = _executor(chat_url)
        job = crashed.claim("crashed", 1)[0]
        assert crashed.store.start(job.subject_id) is not None  # running, then dies
        with psycopg.connect(chat_url) as connection:
            _ = connection.execute(EXPIRE_LEASES)
        assert _drain(chat_url) == ["completed"]
        run = await _run(client, owner, run_id)
    assert run["status"] == "completed"
    with psycopg.connect(chat_url) as connection:
        row = connection.execute(
            "SELECT attempt_count, status FROM rag_work_jobs WHERE kind = 'query_run'"
        ).fetchone()
    assert row == (2, "DONE")


async def test_cancel_queued_and_running_runs(chat_url: str) -> None:
    async with _client(chat_url) as client:
        owner = await _session(client)
        conversation = await _conversation(client, owner, ALL)
        queued = _id(await _submit(client, owner, conversation))
        cancelled = await client.post(f"/api/v1/runs/{queued}/cancel", headers=owner)
        assert _drain(chat_url) == []  # the job was cancelled with the run

        running = _id(await _submit(client, owner, conversation, key="r"))
        executor = _executor(chat_url)
        job = executor.claim("w", 1)[0]
        assert executor.store.start(job.subject_id) is not None
        flagged = await client.post(f"/api/v1/runs/{running}/cancel", headers=owner)
        result = executor.execute(job)
        final = await _run(client, owner, running)

    assert cast("Json", cancelled.json())["status"] == "cancelled"
    assert cast("Json", flagged.json())["status"] == "running"
    assert cast("Json", flagged.json())["cancel_requested"] is True
    assert result.status == "cancelled"
    assert final["status"] == "cancelled"
    assert final["answer"] is None


async def test_filtered_scope_is_the_whole_matching_set(chat_url: str) -> None:
    async with _client(chat_url) as client:
        owner = await _session(client)
        conversation = await _conversation(
            client, owner, {"type": "filtered", "filters": {"q": "pier"}}
        )
        run_id = _id(await _submit(client, owner, conversation))
        _ = _drain(chat_url)
        run = await _run(client, owner, run_id)
    catalog = PostgresArticleCatalog(rag_engine(chat_url))
    from backend.rag.domain.catalog import catalog_filters  # noqa: PLC0415

    expected = catalog.resolve_scope(catalog_filters(q="pier"), 5000)
    assert cast("Json", run["effective_scope"])["resolved_articles"] == expected.total
    used = set(cast("dict[str, str]", run["source_versions"]))
    assert used <= {str(a) for a in expected.versions}


async def test_article_scope_with_pin_and_stale_pin(chat_url: str) -> None:
    store = PostgresRagStore(rag_engine(chat_url))
    documents = load_fixture_articles(FIXTURES)
    target = documents[0]
    async with _client(chat_url) as client:
        owner = await _session(client)
        pinned = await _conversation(
            client,
            owner,
            {
                "type": "articles",
                "articles": [
                    {
                        "article_id": str(target.article_id),
                        "article_version_id": target.content_hash,
                    }
                ],
            },
        )
        ok = _id(await _submit(client, owner, pinned))
        _ = _drain(chat_url)
        answered = await _run(client, owner, ok)
        store.persist_article(revise(target))
        stale = _id(await _submit(client, owner, pinned, key="after-edit"))
        _ = _drain(chat_url)
        failed = await _run(client, owner, stale)
        bad_pin = await client.post(
            "/api/v1/conversations",
            json={
                "scope": {
                    "type": "articles",
                    "articles": [
                        {
                            "article_id": str(target.article_id),
                            "article_version_id": target.content_hash,
                        }
                    ],
                }
            },
            headers=owner,
        )

    assert answered["status"] == "completed"
    assert set(cast("dict[str, str]", answered["source_versions"])) <= {
        str(target.article_id)
    }
    assert failed["status"] == "failed"
    assert failed["error"] == {
        "code": "scope_stale",
        "reason": "pinned_version_superseded",
    }
    assert bad_pin.status_code == 400
    assert (
        cast("Json", cast("Json", bad_pin.json())["error"])["reason"] == "scope_stale"
    )


async def test_stored_citation_survives_ref_expiry_but_never_points_at_new_text(
    chat_url: str,
) -> None:
    async with _client(chat_url) as client:
        owner = await _session(client)
        conversation = await _conversation(client, owner, ALL)
        run_id = _id(await _submit(client, owner, conversation))
        _ = _drain(chat_url)
        citation = cast("list[Json]", (await _run(client, owner, run_id))["citations"])[
            0
        ]
        with psycopg.connect(chat_url) as connection:  # every issued ref expires
            _ = connection.execute(EXPIRE_REFS)
        fresh = await client.post(str(citation["source_ref_url"]), headers=owner)
        viewer = await client.get(str(cast("Json", fresh.json())["viewer_url"]))
        cited_id = UUID(str(citation["article_id"]))
        document = next(
            d for d in load_fixture_articles(FIXTURES) if d.article_id == cited_id
        )
        PostgresRagStore(rag_engine(chat_url)).persist_article(revise(document))
        after_edit = await client.post(str(citation["source_ref_url"]), headers=owner)
        old_viewer = await client.get(str(cast("Json", fresh.json())["viewer_url"]))

    assert fresh.status_code == 201
    assert viewer.status_code == 200
    span = cast("Json", cast("Json", viewer.json())["span"])
    assert span["excerpt"] == cast("Json", citation["span"])["excerpt"]
    assert after_edit.status_code == 410
    assert (
        cast("Json", cast("Json", after_edit.json())["error"])["code"] == "source_stale"
    )
    assert old_viewer.status_code == 410


async def test_runs_are_refused_without_an_answer_provider(chat_url: str) -> None:
    async with _client(chat_url, answers="none") as client:
        owner = await _session(client)
        conversation = await _conversation(client, owner, ALL)
        response = await _submit(client, owner, conversation)
    assert response.status_code == 503
    assert (
        cast("Json", cast("Json", response.json())["error"])["code"]
        == "model_unavailable"
    )


async def test_worker_without_model_fails_runs_instead_of_faking(chat_url: str) -> None:
    async with _client(chat_url) as client:
        owner = await _session(client)
        conversation = await _conversation(client, owner, ALL)
        run_id = _id(await _submit(client, owner, conversation))
        executor = build_run_executor(chat_url, _env(chat_url, "none"))
        assert _drain(chat_url, executor) == ["failed"]
        run = await _run(client, owner, run_id)
    assert run["error"] == {
        "code": "model_unavailable",
        "reason": "provider_not_configured",
    }
    assert run["answer"] is None


async def test_catalog_storage_outage_fails_the_run(chat_url: str) -> None:
    async with _client(chat_url) as client:
        owner = await _session(client)
        conversation = await _conversation(
            client, owner, {"type": "filtered", "filters": {"q": "pier"}}
        )
        run_id = _id(await _submit(client, owner, conversation))
        healthy = _executor(chat_url)
        broken = QueryRunExecutor(
            store=healthy.store,
            queue=healthy.queue,
            catalog=PostgresArticleCatalog(
                rag_engine("postgresql://x:y@127.0.0.1:1/none?connect_timeout=1")
            ),
            briefing=healthy.briefing,
            caller=healthy.caller,
        )
        assert _drain(chat_url, broken) == ["failed"]
        run = await _run(client, owner, run_id)
    assert cast("Json", run["error"])["code"] == "retrieval_unavailable"


async def test_deleting_a_conversation_cancels_its_queued_run(chat_url: str) -> None:
    async with _client(chat_url) as client:
        owner = await _session(client)
        conversation = await _conversation(client, owner, ALL)
        run_id = _id(await _submit(client, owner, conversation))
        deleted = await client.delete(
            f"/api/v1/conversations/{conversation}", headers=owner
        )
        gone = await client.get(
            f"/api/v1/conversations/{conversation}/messages", headers=owner
        )
        run = await _run(client, owner, run_id)
    assert deleted.status_code == 204
    assert gone.status_code == 404
    assert run["status"] == "cancelled"
    assert _drain(chat_url) == []
