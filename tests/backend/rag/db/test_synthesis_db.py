"""T6 current-evidence and access guards on real PostgreSQL (spec section 9.10).

Each test gets a fresh schema (``retrieval_url``). They skip in ``make check``
without a DSN and run with 0 skips in ``make rag-db-test``. Model calls are
scripted fakes whose callable commits a real correction/tombstone (or flips
the fixture access policy) while the "model" runs; the final batch fetch is
raced through a thin wrapper around the real ``PostgresRetrievalStore``.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from backend.rag.application.concurrency import ProcessConcurrencyLimiter
from backend.rag.application.ledger import RequestTokenLedger
from backend.rag.application.ports.retrieval import CurrentArticleState
from backend.rag.application.ports.synthesis import (
    SynthesisModelPort,
    SynthesisRequest,
    SynthesisResponse,
)
from backend.rag.application.use_cases.retrieve import RetrievalOptions
from backend.rag.application.use_cases.synthesize import (
    PrepareSynthesis,
    SynthesizeAnswer,
)
from backend.rag.domain.article_document import ArticleDocument
from backend.rag.domain.query import BriefingQuery
from backend.rag.domain.retrieval import RetrievalResult, RetrievalStorageError
from backend.rag.domain.synthesis import SynthesisOutcome, SynthesisResult
from backend.rag.domain.synthesis_errors import (
    SynthesisAccessDeniedError,
    SynthesisEvidenceStaleError,
    SynthesisStorageError,
)
from backend.rag.infra.llm.fake_synthesis import DeterministicFakeSynthesizer
from backend.rag.infra.llm.scripted_synthesis import ScriptedSynthesizer
from backend.rag.infra.postgres.engine import rag_engine
from backend.rag.infra.postgres.store import PostgresRagStore
from backend.rag.infra.retrieval.postgres_retrieval import PostgresRetrievalStore
from retrieval_support import (
    CALLER,
    MutableAccess,
    article,
    briefing,
    control,
    service,
)
from synthesis_support import (
    COUNTER,
    DECISION,
    IDENTITY,
    SYNTH_POLICY,
    RecordingTelemetry,
    sequential_ids,
)

T0 = datetime(2026, 3, 1, tzinfo=UTC)
QUESTION = "harbor crane"
FAKE = DeterministicFakeSynthesizer(IDENTITY)


def harbor(text: str = "") -> ArticleDocument:
    filler = " ".join(f"w{char}{index}" for index, char in enumerate(uuid4().hex))
    return article(
        f"Harbor crane inspection closes the East Pier gate. {text} {filler}.",
        published=T0,
    )


def persist(url: str, *documents: ArticleDocument) -> None:
    store = PostgresRagStore(rag_engine(url))
    for document in documents:
        store.persist_article(document)


@dataclass(slots=True)
class RacingStore:
    """Real store; optional hooks run around the N-th ``current_articles``."""

    inner: PostgresRetrievalStore
    before: dict[int, Callable[[], None]] = field(
        default_factory=dict[int, Callable[[], None]]
    )
    after: dict[int, Callable[[], None]] = field(
        default_factory=dict[int, Callable[[], None]]
    )
    reads: int = 0
    requested: list[int] = field(default_factory=list[int])

    def current_articles(
        self, article_ids: tuple[UUID, ...]
    ) -> tuple[CurrentArticleState, ...]:
        self.reads += 1
        self.requested.append(len(article_ids))
        if (hook := self.before.get(self.reads)) is not None:
            hook()
        states = self.inner.current_articles(article_ids)
        if (hook := self.after.get(self.reads)) is not None:
            hook()
        return states


@dataclass(slots=True)
class CountdownAccess:
    """Allows the first ``allowed`` checks, then denies (revocation mid-path)."""

    allowed: int
    checks: int = 0

    def allows(self, scope: object) -> bool:
        del scope
        self.checks += 1
        return self.checks <= self.allowed


def during_call(
    mutate: Callable[[], None], *, abstain: bool = False
) -> ScriptedSynthesizer:
    """A model whose call commits ``mutate`` before answering."""

    def answer(request: SynthesisRequest) -> SynthesisResponse:
        mutate()
        if abstain:
            return SynthesisResponse(
                content=(
                    '{"schema_version":"synthesis_draft.v1",'
                    '"evidence_status":"insufficient","claims":[],'
                    '"uncertainties":[{"reason_code":"insufficient_evidence",'
                    '"claim_ref":null}]}'
                ),
                usage=None,
                usage_status=FAKE.synthesize(request).usage_status,
                reported_model=IDENTITY.model_id,
            )
        return FAKE.synthesize(request)

    return ScriptedSynthesizer(IDENTITY, [answer])


def prepared(
    url: str, query: BriefingQuery
) -> tuple[RetrievalResult, RequestTokenLedger]:
    ledger = RequestTokenLedger(COUNTER, SYNTH_POLICY)
    plan = PrepareSynthesis(COUNTER, SYNTH_POLICY).execute(query, ledger)
    result = service(PostgresRetrievalStore(rag_engine(url))).execute(
        query,
        CALLER,
        control(),
        RetrievalOptions(remaining_context_budget=plan.remaining_context_budget),
    )
    return result, ledger


def synthesize(  # noqa: PLR0913
    url: str,
    model: SynthesisModelPort,
    *,
    query: BriefingQuery | None = None,
    store: RacingStore | None = None,
    access: MutableAccess | CountdownAccess | None = None,
    telemetry: RecordingTelemetry | None = None,
) -> SynthesisResult:
    query = query or briefing(QUESTION)
    retrieval, ledger = prepared(url, query)
    use_case = SynthesizeAnswer(
        model=model,
        sources=store or RacingStore(PostgresRetrievalStore(rag_engine(url))),
        access=access or MutableAccess(),
        tokens=COUNTER,
        policy=SYNTH_POLICY,
        activation=DECISION,
        telemetry=telemetry,
        ids=sequential_ids(),
    )
    lease = ProcessConcurrencyLimiter(30).try_acquire()
    try:
        return use_case.execute(query, retrieval, CALLER, control(), ledger, lease)
    finally:
        lease.release()


def test_happy_path_on_real_db_returns_cited_current_answer(retrieval_url: str) -> None:
    documents = harbor("one"), harbor("two")
    persist(retrieval_url, *documents)
    store = RacingStore(PostgresRetrievalStore(rag_engine(retrieval_url)))

    result = synthesize(retrieval_url, FAKE, store=store)

    assert result.outcome is SynthesisOutcome.ANSWERED
    assert {c.article_id for c in result.answer.citations} == {
        d.article_id for d in documents
    }
    current = {d.article_id: d.content_hash for d in documents}
    for source in result.answer.source_documents:
        assert source.content_hash == current[source.article_id]
    assert store.reads == 2
    assert result.metrics.source_batch_reads == 2


# Evidence path: change committed while the model call runs ------------------


def test_correction_during_model_call_fails_whole_answer(retrieval_url: str) -> None:
    changed, stable = harbor("changing"), harbor("stable")
    persist(retrieval_url, changed, stable)
    revision = article("Rewritten text.", article_id=changed.article_id, published=T0)
    model = during_call(lambda: persist(retrieval_url, revision))

    with pytest.raises(SynthesisEvidenceStaleError):
        _ = synthesize(retrieval_url, model)


def test_tombstone_during_model_call_fails_whole_answer(retrieval_url: str) -> None:
    gone, stable = harbor("gone"), harbor("stable")
    persist(retrieval_url, gone, stable)
    model = during_call(
        lambda: PostgresRagStore(rag_engine(retrieval_url)).tombstone_article(
            gone.article_id
        )
    )

    with pytest.raises(SynthesisEvidenceStaleError):
        _ = synthesize(retrieval_url, model)


def test_access_revocation_during_model_call_returns_nothing(
    retrieval_url: str,
) -> None:
    persist(retrieval_url, harbor("one"))
    access = MutableAccess()
    telemetry = RecordingTelemetry()

    def revoke() -> None:
        access.revoked = True

    with pytest.raises(SynthesisAccessDeniedError):
        _ = synthesize(
            retrieval_url, during_call(revoke), access=access, telemetry=telemetry
        )

    (event,) = telemetry.events
    assert event.outcome == SynthesisAccessDeniedError.code
    assert event.usage is None
    assert event.metrics is None
    assert event.provenance is None


# Evidence path: change racing the final (post-call) batch fetch -------------


def test_correction_racing_final_batch_fetch(retrieval_url: str) -> None:
    changed, stable = harbor("changing"), harbor("stable")
    persist(retrieval_url, changed, stable)
    revision = article("Rewritten text.", article_id=changed.article_id, published=T0)
    store = RacingStore(
        PostgresRetrievalStore(rag_engine(retrieval_url)),
        before={2: lambda: persist(retrieval_url, revision)},
    )

    with pytest.raises(SynthesisEvidenceStaleError):
        _ = synthesize(retrieval_url, FAKE, store=store)


def test_tombstone_racing_final_batch_fetch(retrieval_url: str) -> None:
    gone = harbor("gone")
    persist(retrieval_url, gone, harbor("stable"))
    store = RacingStore(
        PostgresRetrievalStore(rag_engine(retrieval_url)),
        before={
            2: lambda: PostgresRagStore(rag_engine(retrieval_url)).tombstone_article(
                gone.article_id
            )
        },
    )

    with pytest.raises(SynthesisEvidenceStaleError):
        _ = synthesize(retrieval_url, FAKE, store=store)


def test_revocation_committed_during_final_fetch_still_wins(retrieval_url: str) -> None:
    persist(retrieval_url, harbor("one"))
    access = MutableAccess()

    def revoke() -> None:
        access.revoked = True

    store = RacingStore(
        PostgresRetrievalStore(rag_engine(retrieval_url)), after={2: revoke}
    )

    with pytest.raises(SynthesisAccessDeniedError):
        _ = synthesize(retrieval_url, FAKE, store=store, access=access)


def test_storage_failure_in_final_fetch_is_typed_not_empty(retrieval_url: str) -> None:
    persist(retrieval_url, harbor("one"))

    def drop() -> None:
        # What the real adapter raises for an outage; T6 must map, not empty.
        raise RetrievalStorageError

    store = RacingStore(
        PostgresRetrievalStore(rag_engine(retrieval_url)), before={2: drop}
    )

    with pytest.raises(SynthesisStorageError):
        _ = synthesize(retrieval_url, FAKE, store=store)


# Abstention paths ------------------------------------------------------------


def test_no_evidence_abstention_makes_no_call_and_no_source_read(
    retrieval_url: str,
) -> None:
    persist(retrieval_url, harbor("one"))
    model = ScriptedSynthesizer(IDENTITY, [])
    store = RacingStore(PostgresRetrievalStore(rag_engine(retrieval_url)))

    result = synthesize(
        retrieval_url, model, query=briefing("zzzqqq xylophonequartz"), store=store
    )

    assert result.outcome is SynthesisOutcome.ABSTAINED_NO_EVIDENCE
    assert result.answer.claims == ()
    assert model.requests == []
    assert store.reads == 0


def test_revocation_on_no_evidence_abstention_returns_no_counts(
    retrieval_url: str,
) -> None:
    persist(retrieval_url, harbor("one"))
    telemetry = RecordingTelemetry()
    # Entry check passes; the return-boundary check sees the revocation.
    access = CountdownAccess(allowed=1)

    with pytest.raises(SynthesisAccessDeniedError):
        _ = synthesize(
            retrieval_url,
            ScriptedSynthesizer(IDENTITY, []),
            query=briefing("zzzqqq xylophonequartz"),
            access=access,
            telemetry=telemetry,
        )

    assert access.checks == 2
    (event,) = telemetry.events
    assert (event.usage, event.metrics, event.provenance) == (None, None, None)


def test_tombstone_during_model_abstention_is_stale_not_abstention(
    retrieval_url: str,
) -> None:
    gone = harbor("gone")
    persist(retrieval_url, gone)
    model = during_call(
        lambda: PostgresRagStore(rag_engine(retrieval_url)).tombstone_article(
            gone.article_id
        ),
        abstain=True,
    )

    with pytest.raises(SynthesisEvidenceStaleError):
        _ = synthesize(retrieval_url, model)


def test_revocation_during_model_abstention_is_denied(retrieval_url: str) -> None:
    persist(retrieval_url, harbor("one"))
    access = MutableAccess()

    def revoke() -> None:
        access.revoked = True

    with pytest.raises(SynthesisAccessDeniedError):
        _ = synthesize(retrieval_url, during_call(revoke, abstain=True), access=access)


def test_model_abstention_without_changes_is_validated(retrieval_url: str) -> None:
    persist(retrieval_url, harbor("one"))

    result = synthesize(retrieval_url, during_call(lambda: None, abstain=True))

    assert result.outcome is SynthesisOutcome.ABSTAINED_BY_MODEL
    assert result.answer.claims == ()
    assert result.answer.citations == ()


# Query counts: batched reads, no N+1 -----------------------------------------


@pytest.mark.parametrize("articles", [1, 3, 8])
def test_source_reads_are_two_batched_statements_regardless_of_size(
    retrieval_url: str, articles: int
) -> None:
    persist(retrieval_url, *(harbor(f"n{index}") for index in range(articles)))
    inner = PostgresRetrievalStore(rag_engine(retrieval_url))
    store = RacingStore(inner)

    result = synthesize(retrieval_url, FAKE, store=store)

    assert result.outcome is SynthesisOutcome.ANSWERED
    assert len(result.answer.citations) == articles
    assert store.reads == 2
    assert inner.statements == 2
    assert store.requested == [articles, articles]
