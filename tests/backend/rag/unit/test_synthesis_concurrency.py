"""Shared process-local limiter: 30/31, no leaks, in-flight calls stay pinned.

Ordering uses Events/Barriers and thread joins, never sleeps. The two
timeout cases necessarily wait out a tiny real time slice (0.05 s).
"""

import threading
from collections.abc import Callable
from threading import Barrier, Event, Thread

import pytest

from backend.rag.application.concurrency import ProcessConcurrencyLimiter
from backend.rag.application.ports.synthesis import SynthesisRequest, SynthesisResponse
from backend.rag.application.use_cases.synthesize import SynthesizeAnswer
from backend.rag.domain.retrieval import QueryControl, RetrievalResult
from backend.rag.domain.synthesis_errors import (
    ProviderFailureCode,
    SynthesisCancelledError,
    SynthesisConcurrencyRejectedError,
    SynthesisConfigurationError,
    SynthesisDeadlineExceededError,
    SynthesisError,
    SynthesisProviderError,
)
from backend.rag.domain.synthesis_policy import SynthesisPolicy
from backend.rag.infra.llm.fake_synthesis import DeterministicFakeSynthesizer
from backend.rag.infra.llm.scripted_synthesis import ScriptedSynthesizer
from synthesis_support import (
    CALLER,
    COUNTER,
    DECISION,
    IDENTITY,
    SYNTH_POLICY,
    MemoryStore,
    MutableAccess,
    RecordingTelemetry,
    article,
    briefing,
    ledger,
    retrieve,
    run,
    sequential_ids,
    store_with,
)

CAPACITY = SYNTH_POLICY.concurrency.max_concurrent_requests


def _store() -> MemoryStore:
    return store_with(article("Harbor bridge closed after the storm."))


def _blocking(
    entered: Callable[[], object], finish: Event
) -> Callable[[SynthesisRequest], SynthesisResponse]:
    fake = DeterministicFakeSynthesizer(IDENTITY)

    def call(request: SynthesisRequest) -> SynthesisResponse:
        _ = entered()
        assert finish.wait(timeout=10)
        return fake.synthesize(request)

    return call


def _join_synthesis_threads() -> None:
    for thread in threading.enumerate():
        if thread.name == "eventlens-synthesis":
            thread.join(timeout=10)


def test_limiter_capacity_comes_from_policy_and_is_hard_capped() -> None:
    assert ProcessConcurrencyLimiter(CAPACITY).capacity == 30
    for bad in (0, 31):
        with pytest.raises(SynthesisConfigurationError):
            _ = ProcessConcurrencyLimiter(bad)


def test_thirty_in_flight_requests_admitted_and_thirty_first_rejected() -> None:
    limiter = ProcessConcurrencyLimiter(CAPACITY)
    store = _store()
    query = briefing("harbor")
    retrieval = retrieve(store, query)
    all_entered = Barrier(CAPACITY + 1)
    finish = Event()
    call = _blocking(lambda: all_entered.wait(timeout=10), finish)
    model = ScriptedSynthesizer(IDENTITY, [call] * (CAPACITY + 1))
    errors: list[BaseException] = []

    def request() -> None:
        try:
            _ = run(model, store, query, retrieval, limiter=limiter)
        except BaseException as error:  # noqa: BLE001 - surfaced by the assert
            errors.append(error)

    threads = [Thread(target=request) for _ in range(CAPACITY)]
    for thread in threads:
        thread.start()
    _ = all_entered.wait(timeout=10)

    assert limiter.in_use == CAPACITY
    with pytest.raises(SynthesisConcurrencyRejectedError):
        _ = run(model, store, query, retrieval, limiter=limiter)
    assert len(model.requests) == CAPACITY

    finish.set()
    for thread in threads:
        thread.join(timeout=10)
    _join_synthesis_threads()
    assert errors == []
    assert limiter.in_use == 0


def test_rejected_request_does_no_storage_work() -> None:
    limiter = ProcessConcurrencyLimiter(1)
    held = limiter.try_acquire()
    store = _store()
    query = briefing("harbor")
    retrieval = retrieve(store, query)
    before = len(store.calls)

    with pytest.raises(SynthesisConcurrencyRejectedError):
        _ = run(
            ScriptedSynthesizer(IDENTITY, []), store, query, retrieval, limiter=limiter
        )

    assert store.calls[before:] == []
    held.release()
    assert limiter.in_use == 0


@pytest.mark.parametrize(
    "item",
    [
        SynthesisProviderError(ProviderFailureCode.UNAVAILABLE),
        SynthesisResponse("not json", None, "missing", None),  # pyright: ignore[reportArgumentType]
    ],
)
def test_failures_never_leak_slots(item: object) -> None:
    limiter = ProcessConcurrencyLimiter(2)
    store = _store()
    query = briefing("harbor")
    retrieval = retrieve(store, query)

    for _ in range(5):
        model = ScriptedSynthesizer(IDENTITY, [item])  # pyright: ignore[reportArgumentType]
        with pytest.raises(SynthesisError):
            _ = run(model, store, query, retrieval, limiter=limiter)

    _join_synthesis_threads()
    assert limiter.in_use == 0


def _service(
    model: ScriptedSynthesizer,
    store: MemoryStore,
    telemetry: RecordingTelemetry | None = None,
    policy: SynthesisPolicy = SYNTH_POLICY,
) -> SynthesizeAnswer:
    return SynthesizeAnswer(
        model=model,
        sources=store,
        access=MutableAccess(),
        tokens=COUNTER,
        policy=policy,
        activation=DECISION,
        telemetry=telemetry,
        ids=sequential_ids(),
    )


def _in_thread(target: Callable[[], None]) -> Thread:
    thread = Thread(target=target)
    thread.start()
    return thread


def test_cancel_during_call_keeps_slot_until_call_returns() -> None:
    limiter = ProcessConcurrencyLimiter(CAPACITY)
    store = _store()
    query = briefing("harbor")
    retrieval = retrieve(store, query)
    entered, finish = Event(), Event()
    model = ScriptedSynthesizer(IDENTITY, [_blocking(entered.set, finish)])
    telemetry = RecordingTelemetry()
    query_control = QueryControl(20.0)
    request_ledger = ledger()
    outcome: list[BaseException] = []
    lease = limiter.try_acquire()

    def request() -> None:
        try:
            _ = _service(model, store, telemetry).execute(
                query, retrieval, CALLER, query_control, request_ledger, lease
            )
        except BaseException as error:  # noqa: BLE001 - asserted below
            outcome.append(error)
        finally:
            lease.release()

    caller = _in_thread(request)
    assert entered.wait(timeout=10)
    reads_before_cancel = store.calls.count("current_articles")
    query_control.cancel()
    caller.join(timeout=10)

    assert len(outcome) == 1
    assert isinstance(outcome[0], SynthesisCancelledError)
    assert lease.released
    assert limiter.in_use == 1  # the call is still in flight
    snapshot = request_ledger.snapshot()
    assert snapshot.output_charged == SYNTH_POLICY.budget.output_reservation_per_call

    finish.set()
    _join_synthesis_threads()
    assert limiter.in_use == 0
    # The late result is dropped: no post-call read, no answer telemetry.
    assert store.calls.count("current_articles") == reads_before_cancel
    assert [event.outcome for event in telemetry.events] == ["synthesis_cancelled"]


def _timed_out(  # noqa: PLR0913, PLR0917
    limiter: ProcessConcurrencyLimiter,
    finish: Event,
    query_control: QueryControl,
    policy: SynthesisPolicy,
    retrieval: RetrievalResult,
    store: MemoryStore,
) -> BaseException:
    entered = Event()
    model = ScriptedSynthesizer(IDENTITY, [_blocking(entered.set, finish)])
    lease = limiter.try_acquire()
    try:
        _ = _service(model, store, policy=policy).execute(
            briefing("harbor"), retrieval, CALLER, query_control, ledger(), lease
        )
    except SynthesisError as error:
        return error
    finally:
        lease.release()
    message = "expected a typed failure"
    raise AssertionError(message)


def _short_timeout_policy() -> SynthesisPolicy:
    raw = SYNTH_POLICY.model_dump_json()
    short = raw.replace('"call_timeout_seconds":15.0', '"call_timeout_seconds":0.05')
    assert short != raw
    return SynthesisPolicy.model_validate_json(short)


def test_per_call_timeout_is_provider_timeout_and_slot_stays_pinned() -> None:
    limiter = ProcessConcurrencyLimiter(CAPACITY)
    store = _store()
    retrieval = retrieve(store, briefing("harbor"))
    finish = Event()

    error = _timed_out(
        limiter, finish, QueryControl(20.0), _short_timeout_policy(), retrieval, store
    )

    assert isinstance(error, SynthesisProviderError)
    assert error.reason is ProviderFailureCode.TIMEOUT
    assert limiter.in_use == 1
    finish.set()
    _join_synthesis_threads()
    assert limiter.in_use == 0


def test_request_deadline_during_call_is_deadline_error_not_provider() -> None:
    limiter = ProcessConcurrencyLimiter(CAPACITY)
    store = _store()
    retrieval = retrieve(store, briefing("harbor"))
    finish = Event()

    error = _timed_out(
        limiter, finish, QueryControl(0.05), SYNTH_POLICY, retrieval, store
    )

    assert isinstance(error, SynthesisDeadlineExceededError)
    assert limiter.in_use == 1
    finish.set()
    _join_synthesis_threads()
    assert limiter.in_use == 0


def test_repeated_timeouts_cannot_grow_in_flight_calls_past_capacity() -> None:
    limiter = ProcessConcurrencyLimiter(2)
    store = _store()
    retrieval = retrieve(store, briefing("harbor"))
    finish = Event()
    policy = _short_timeout_policy()

    for _ in range(2):
        error = _timed_out(
            limiter, finish, QueryControl(20.0), policy, retrieval, store
        )
        assert isinstance(error, SynthesisProviderError)

    assert limiter.in_use == 2
    with pytest.raises(SynthesisConcurrencyRejectedError):
        _ = limiter.try_acquire()
    finish.set()
    _join_synthesis_threads()
    assert limiter.in_use == 0


def test_lease_release_and_unpin_are_idempotent_and_pin_after_release_fails() -> None:
    limiter = ProcessConcurrencyLimiter(1)
    lease = limiter.try_acquire()
    unpin = lease.pin()
    lease.release()
    lease.release()
    assert limiter.in_use == 1
    unpin()
    unpin()
    assert limiter.in_use == 0
    with pytest.raises(SynthesisConfigurationError):
        _ = lease.pin()
    assert limiter.try_acquire().released is False


def test_released_lease_cannot_start_synthesis() -> None:
    limiter = ProcessConcurrencyLimiter(1)
    lease = limiter.try_acquire()
    lease.release()
    store = _store()
    query = briefing("harbor")
    model = ScriptedSynthesizer(IDENTITY, [])

    with pytest.raises(SynthesisConfigurationError):
        _ = _service(model, store).execute(
            query, retrieve(store, query), CALLER, QueryControl(20.0), ledger(), lease
        )
    assert model.requests == []
