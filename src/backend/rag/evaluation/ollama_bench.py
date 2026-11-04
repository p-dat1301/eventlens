"""Offline synthesis benchmark against a local Ollama server (never production).

Reads ``EVENTLENS_EVAL_OLLAMA_BASE_URL`` (loopback ``http`` only) and
``EVENTLENS_EVAL_OLLAMA_MODEL`` (comma list). It bypasses nothing: it does not
touch provider activation, the production synthesizer or its timeouts, and
the composition root never imports it. Models run sequentially and are
unloaded afterwards. Every model sees the same prompt, the same evidence, the
same seed and the same output cap.

Run protocol (``run_bench``): the model digest is fetched before any
generation (no digest, no run); every response is persisted atomically as soon
as it arrives (``ollama_checkpoint``); a rerun reuses only records whose
request identity still matches; unload / final digest failures are recorded as
auxiliary errors and never discard generations; the report is a pure function
of the manifest, the records and the labels, so ``rebuild_report`` reproduces
it offline. Denominators always include every scheduled question.
"""

import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.client import HTTPException
from ipaddress import ip_address, ip_network
from typing import TYPE_CHECKING, Annotated, ClassVar, Final, Protocol, cast
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from backend.rag.evaluation.metrics import (
    Json,
    canonical,
    digest,
    latency_summary,
    ratio,
)
from backend.rag.evaluation.ollama_checkpoint import (
    CHECKPOINT_SCHEMA,
    RECORD_SCHEMA,
    CheckpointStore,
    EvidenceRef,
    ResponseRecord,
    sha256_text,
)
from backend.rag.evaluation.search_eval.dataset import EvalQuestion, SearchDataset
from backend.rag.evaluation.search_eval.scoring import RankedPassage

if TYPE_CHECKING:
    from http.client import HTTPResponse

BASE_URL_ENV: Final = "EVENTLENS_EVAL_OLLAMA_BASE_URL"
MODEL_ENV: Final = "EVENTLENS_EVAL_OLLAMA_MODEL"
LOOPBACK: Final = frozenset({"127.0.0.1", "localhost", "::1"})
PRIVATE_NETWORKS: Final = (
    ip_network("10.0.0.0/8"),
    ip_network("172.16.0.0/12"),
    ip_network("192.168.0.0/16"),
    ip_network("100.64.0.0/10"),
)
SEED: Final = 42
OUTPUT_CAP: Final = 512
CONTEXT: Final = 8192
EVIDENCE_K: Final = 5
SUPPORT_DEFINITION: Final = (
    "answered questions with >= 1 claim citing a chunk that contains a gold "
    "span (mechanical proxy, not entailment)"
)
SCHEMA_DEFINITION: Final = (
    "strictly valid output (schema types, required keys, no extra keys, "
    "abstention invariants) / all questions incl. execution errors"
)
SCHEMA_VERSION: Final = "ollama_synthesis_bench.v2"
DIGEST_PATTERN: Final = re.compile(r"(sha256:)?[0-9a-f]{64}")
CALL_ERRORS: Final = (URLError, TimeoutError, OSError, ValueError, HTTPException)
PROMPT_VERSION: Final = "ollama-bench-prompt.v2"
SYSTEM_PROMPT: Final = (
    "Bạn là trợ lý tóm tắt tin tức. Chỉ dùng các đoạn nguồn được cung cấp. "
    "Mỗi khẳng định phải trích dẫn mã nguồn (S1, S2, ...). Nếu nguồn không đủ "
    "để trả lời, hoặc câu hỏi chứa tiền đề sai, đặt abstain=true và để answer "
    "là null. Trả lời bằng tiếng Việt, đúng JSON schema."
)
OUTPUT_SCHEMA: Final[dict[str, object]] = {
    "type": "object",
    "properties": {
        "abstain": {"type": "boolean"},
        "answer": {"type": ["string", "null"]},
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "sources": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["text", "sources"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["abstain", "answer", "claims"],
    "additionalProperties": False,
}


class OllamaBenchConfigError(ValueError):
    """Missing or non-loopback benchmark endpoint."""


class OllamaBenchPreflightError(RuntimeError):
    """Model identity or evidence is not pinned; nothing was generated."""


class OllamaBenchPort(Protocol):
    """Minimal client contract shared by the real and fake benchmark clients."""

    def chat(self, model: str, system: str, user: str) -> tuple[str, dict[str, object]]:
        """Generate one response."""
        ...

    def unload(self, model: str) -> None:
        """Release the model before the next one."""
        ...

    def digest(self, model: str) -> str | None:
        """Return the model digest."""
        ...


def bench_endpoint(base_url: str) -> str:
    """Accept loopback or private-network hosts only (offline benchmark).

    Private means RFC 1918, Tailscale/CGNAT ``100.64.0.0/10`` or loopback, given
    as an IP literal. Public hosts and names are refused: Ollama has no auth,
    so the benchmark never talks to an Internet endpoint.
    """
    parts = urlsplit(base_url)
    host = parts.hostname or ""
    if parts.scheme not in {"http", "https"} or not _private_host(host):
        msg = f"{BASE_URL_ENV} must be a loopback or private-network URL"
        raise OllamaBenchConfigError(msg)
    return base_url.rstrip("/")


def _private_host(host: str) -> bool:
    if host in LOOPBACK:
        return True
    try:
        address = ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or any(address in net for net in PRIVATE_NETWORKS)


@dataclass(frozen=True, slots=True)
class OllamaClient:
    """Minimal ``/api/chat`` and ``/api/show`` client (stdlib)."""

    base_url: str
    timeout_seconds: float = 600.0
    num_thread: int | None = None

    def __post_init__(self) -> None:
        """Loopback or private network only."""
        _ = bench_endpoint(self.base_url)

    def _post(self, path: str, body: dict[str, object]) -> dict[str, object]:
        request = Request(  # noqa: S310 - loopback validated
            self.base_url.rstrip("/") + path,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return self._read(request, self.timeout_seconds)

    @staticmethod
    def _read(request: Request, timeout: float) -> dict[str, object]:
        response = cast("HTTPResponse", urlopen(request, timeout=timeout))  # noqa: S310
        with response:
            body = response.read()
        return cast("dict[str, object]", json.loads(body))

    def digest(self, model: str) -> str | None:
        """Model digest from ``/api/tags`` (identifies the exact weights)."""
        request = Request(self.base_url.rstrip("/") + "/api/tags")  # noqa: S310
        tags = self._read(request, 30.0)
        for entry in cast("list[dict[str, object]]", tags.get("models", [])):
            if entry.get("name") == model or entry.get("model") == model:
                value = entry.get("digest")
                return value if isinstance(value, str) else None
        return None

    def chat(self, model: str, system: str, user: str) -> tuple[str, dict[str, object]]:
        """One deterministic, schema-constrained, non-thinking chat call."""
        response = self._post(
            "/api/chat",
            {
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "format": OUTPUT_SCHEMA,
                "stream": False,
                "think": False,
                "keep_alive": "5m",
                "options": ollama_options(self.num_thread),
            },
        )
        message = cast("dict[str, object]", response.get("message", {}))
        return str(message.get("content", "")), response

    def unload(self, model: str) -> None:
        """``keep_alive=0``: free memory before the next model."""
        _ = self._post("/api/generate", {"model": model, "keep_alive": 0})


def ollama_options(num_thread: int | None) -> dict[str, object]:
    """``/api/chat`` options; also part of every request identity."""
    return {
        **({} if num_thread is None else {"num_thread": num_thread}),
        "seed": SEED,
        "temperature": 0,
        "num_predict": OUTPUT_CAP,
        "num_ctx": CONTEXT,
    }


def generation_options(client: object) -> dict[str, Json]:
    """Everything sent besides the messages that can change the output."""
    num_thread = getattr(client, "num_thread", None)
    options = cast("dict[str, Json]", ollama_options(num_thread))
    return {
        "options": options,
        "think": False,
        "stream": False,
        "format_sha256": sha256_text(canonical(cast("Json", OUTPUT_SCHEMA))),
    }


def user_prompt(question: EvalQuestion, evidence: Sequence[RankedPassage]) -> str:
    """Same layout for every model."""
    sources = "\n\n".join(
        f"[S{index}] {passage.text}" for index, passage in enumerate(evidence, 1)
    )
    return f"Câu hỏi: {question.query}\n\nNguồn:\n{sources}"


@dataclass(frozen=True, slots=True)
class _Judged:
    valid: bool
    abstained: bool
    linked: bool
    supported: bool
    unsupported_claims: int
    claims: int


def _covers(passage: RankedPassage, question: EvalQuestion) -> bool:
    return any(
        p.grade == 2  # noqa: PLR2004 - gold grade
        and p.article_id == passage.article_id
        and passage.char_start <= p.char_start
        and p.char_end <= passage.char_end
        for p in question.passages
    )


class _Claim(BaseModel):
    """One cited claim (strict: exact JSON types, no extra keys)."""

    model_config: ClassVar[ConfigDict] = ConfigDict(strict=True, extra="forbid")

    text: Annotated[str, Field(min_length=1)]
    sources: Annotated[list[Annotated[str, Field(min_length=1)]], Field(min_length=1)]


class _Output(BaseModel):
    """``OUTPUT_SCHEMA`` as a strict model (``answer`` required, may be null)."""

    model_config: ClassVar[ConfigDict] = ConfigDict(strict=True, extra="forbid")

    abstain: bool
    answer: str | None
    claims: list[_Claim]


_INVALID: Final = _Judged(
    valid=False,
    abstained=False,
    linked=False,
    supported=False,
    unsupported_claims=0,
    claims=0,
)


def parse_output(content: str) -> _Output | None:
    """Strict schema plus abstention invariants, else ``None``.

    abstain=true needs ``answer: null`` and no claims; abstain=false needs a
    non-empty answer and at least one claim. Wrong JSON types (``"false"``),
    missing keys, extra keys and non-object JSON are all invalid.
    """
    try:
        output = _Output.model_validate_json(content)
    except ValidationError:
        return None
    if output.abstain:
        return output if output.answer is None and not output.claims else None
    has_answer = output.answer is not None and bool(output.answer.strip())
    return output if has_answer and output.claims else None


def judge(
    question: EvalQuestion, evidence: Sequence[RankedPassage], content: str
) -> _Judged:
    """Mechanical checks: strict schema, citation linkage, gold-span support."""
    output = parse_output(content)
    if output is None:
        return _INVALID
    cited = [claim.sources for claim in output.claims]
    labels = {f"S{index}": passage for index, passage in enumerate(evidence, 1)}
    linked = all(all(s in labels for s in sources) for sources in cited)
    supporting = [
        any(s in labels and _covers(labels[s], question) for s in sources)
        for sources in cited
    ]
    return _Judged(
        valid=True,
        abstained=output.abstain,
        linked=linked,
        supported=any(supporting),
        unsupported_claims=sum(1 for ok in supporting if not ok),
        claims=len(cited),
    )


# --- run protocol: preflight, durable records, resume, pure report ------------


def preflight(client: OllamaBenchPort, models: Sequence[str]) -> dict[str, str]:
    """Pin every model to its digest before any generation, or refuse.

    An alias whose digest cannot be read (endpoint down, model absent, odd
    value) never starts a run: the result could not be reproduced.
    """
    pinned: dict[str, str] = {}
    for model in models:
        try:
            value = client.digest(model)
        except CALL_ERRORS as error:
            msg = f"{model}: digest lookup failed ({type(error).__name__})"
            raise OllamaBenchPreflightError(msg) from error
        if value is None or not DIGEST_PATTERN.fullmatch(value):
            msg = f"{model}: no pinned digest on the endpoint ({value!r})"
            raise OllamaBenchPreflightError(msg)
        pinned[model] = value
    return pinned


def evidence_refs(passages: Sequence[RankedPassage]) -> list[EvidenceRef]:
    """Text-free identity of the spans in the order they are sent."""
    return [
        EvidenceRef(
            label=f"S{index}",
            chunk_id=str(p.chunk_id),
            article_id=str(p.article_id),
            content_hash=p.content_hash,
            chunk_version=p.chunk_version,
            char_start=p.char_start,
            char_end=p.char_end,
            text_sha256=sha256_text(p.text),
        )
        for index, p in enumerate(passages, 1)
    ]


def request_identity(  # noqa: PLR0913 - every input that can change the output
    *,
    corpus_digest: str,
    question: EvalQuestion,
    refs: Sequence[EvidenceRef],
    model: str,
    model_digest: str,
    generation: Mapping[str, Json],
    configuration_digest: str,
    prompt: str,
) -> dict[str, Json]:
    """What must match for a stored response to stand for a new call.

    Labels are deliberately absent: relabelling re-judges, it never needs a
    new generation. The corpus, query, exact evidence (ids, offsets, text
    hashes, order), retrieval configuration (tuning, token counter, budget),
    model digest, prompt/schema versions and generation options are present.
    """
    return {
        "bench_schema": SCHEMA_VERSION,
        "record_schema": RECORD_SCHEMA,
        "corpus_digest": corpus_digest,
        "question_id": question.question_id,
        "query_sha256": sha256_text(question.query),
        "evidence": [cast("Json", ref.model_dump()) for ref in refs],
        "evidence_configuration_digest": configuration_digest,
        "model": model,
        "model_digest": model_digest,
        "prompt_version": PROMPT_VERSION,
        "system_prompt_sha256": sha256_text(SYSTEM_PROMPT),
        "prompt_sha256": sha256_text(SYSTEM_PROMPT + "\x00" + prompt),
        "generation": dict(generation),
    }


_USAGE_KEYS: Final = (
    "prompt_eval_count",
    "eval_count",
    "total_duration",
    "load_duration",
    "prompt_eval_duration",
    "eval_duration",
    "done_reason",
)


def _usage(raw: Mapping[str, object]) -> dict[str, int | str | None]:
    out: dict[str, int | str | None] = {}
    for key in _USAGE_KEYS:
        value = raw.get(key)
        out[key] = (
            value
            if isinstance(value, int | str) and not isinstance(value, bool)
            else None
        )
    return out


@dataclass(slots=True)
class _RunState:
    run_id: str
    started_at: str
    auxiliary_errors: dict[str, list[str]] = field(default_factory=dict)
    final_digest: dict[str, str] = field(default_factory=dict)
    rejected: list[dict[str, str]] = field(default_factory=list)

    def entry(self, state: str, finished_at: str | None) -> dict[str, Json]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": finished_at,
            "state": state,
            "auxiliary_errors": cast("Json", self.auxiliary_errors),
            "final_digest": cast("Json", self.final_digest),
            "checkpoint_rejected": cast("Json", self.rejected),
        }


def run_bench(  # noqa: PLR0913, PLR0917 - explicit benchmark inputs
    dataset: SearchDataset,
    evidence: Callable[[EvalQuestion], Sequence[RankedPassage]],
    client: OllamaBenchPort,
    models: Sequence[str],
    on_progress: Callable[[str], None] = lambda _: None,
    timer: Callable[[], float] = time.perf_counter,
    *,
    store: CheckpointStore,
    evidence_k: int = EVIDENCE_K,
    evidence_info: Callable[[EvalQuestion], Mapping[str, Json]] | None = None,
    evidence_configuration: Mapping[str, Json] | None = None,
    allow_evidence_truncation: bool = False,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    run_id: str | None = None,
) -> dict[str, Json]:
    """Sequential models over identical prompts; durable and resumable.

    Raises ``OllamaBenchPreflightError`` before any generation when a model
    digest is not pinned or a payload would be cut by ``evidence_k``
    (unless ``allow_evidence_truncation``; the cut is then reported).
    """
    pinned = preflight(client, models)
    payloads = {q.question_id: tuple(evidence(q)) for q in dataset.questions}
    cut = sorted(qid for qid, p in payloads.items() if len(p) > evidence_k)
    if cut and not allow_evidence_truncation:
        msg = f"{len(cut)} payloads exceed evidence_k={evidence_k}: {cut[:5]}"
        raise OllamaBenchPreflightError(msg)
    sent = {qid: p[:evidence_k] for qid, p in payloads.items()}
    configuration: dict[str, Json] = dict(evidence_configuration or {})
    configuration_digest = digest(configuration)
    generation = generation_options(client)
    prompts = {
        q.question_id: user_prompt(q, sent[q.question_id]) for q in dataset.questions
    }
    refs = {qid: evidence_refs(passages) for qid, passages in sent.items()}
    requests: dict[str, dict[str, str]] = {}
    identities: dict[tuple[str, str], dict[str, Json]] = {}
    for model in models:
        for question in dataset.questions:
            qid = question.question_id
            identity = request_identity(
                corpus_digest=dataset.corpus_digest,
                question=question,
                refs=refs[qid],
                model=model,
                model_digest=pinned[model],
                generation=generation,
                configuration_digest=configuration_digest,
                prompt=prompts[qid],
            )
            identities[model, qid] = identity
            requests.setdefault(model, {})[qid] = digest(identity)
    previous = store.load_manifest()
    runs = cast("list[Json]", (previous or {}).get("runs", []))
    run = _RunState(run_id or str(uuid4()), clock().isoformat())
    plan: dict[str, Json] = {
        "checkpoint_schema": CHECKPOINT_SCHEMA,
        "bench_schema": SCHEMA_VERSION,
        "record_schema": RECORD_SCHEMA,
        "prompt_version": PROMPT_VERSION,
        "generation": generation,
        "seed": SEED,
        "context_tokens": CONTEXT,
        "output_cap_tokens": OUTPUT_CAP,
        "evidence_k": evidence_k,
        "corpus_digest": dataset.corpus_digest,
        "fixture_digest": dataset.fixture_digest,
        "question_ids": [q.question_id for q in dataset.questions],
        "evidence_configuration": configuration,
        "evidence_configuration_digest": configuration_digest,
        "models": cast("Json", pinned),
        "requests": cast("Json", requests),
        "evidence": {
            q.question_id: {
                "payload_spans": len(payloads[q.question_id]),
                "sent_spans": len(sent[q.question_id]),
                **(dict(evidence_info(q)) if evidence_info else {}),
            }
            for q in dataset.questions
        },
    }
    store.save_manifest({**plan, "runs": [*runs, run.entry("running", None)]})
    for model in models:
        for index, question in enumerate(dataset.questions, 1):
            qid = question.question_id
            loaded = store.load(model, qid)
            record = loaded.record
            if (
                record is not None
                and record.status == "ok"
                and (record.request_id == requests[model][qid])
            ):
                on_progress(f"{model}: {index}/{len(dataset.questions)} reused")
                continue
            reason = loaded.rejection or (
                None
                if record is None
                else "retried-error"
                if record.status == "error"
                else "stale"
            )
            if reason is not None:
                _ = store.reject(model, qid, reason)
                run.rejected.append(
                    {"model": model, "question_id": qid, "reason": reason}
                )
            on_progress(f"{model}: {index}/{len(dataset.questions)}")
            _ = store.save(
                _generate(
                    client,
                    model,
                    question,
                    prompts[qid],
                    refs[qid],
                    identities[model, qid],
                    requests[model][qid],
                    pinned[model],
                    run,
                    timer,
                )
            )
        _finish_model(client, model, pinned[model], run)
    finished = run.entry("finished", clock().isoformat())
    manifest: dict[str, Json] = {**plan, "runs": [*runs, finished]}
    store.save_manifest(manifest)
    return build_report(manifest, store, dataset)


def _generate(  # noqa: PLR0913, PLR0917 - one call's full provenance
    client: OllamaBenchPort,
    model: str,
    question: EvalQuestion,
    prompt: str,
    refs: Sequence[EvidenceRef],
    identity: dict[str, Json],
    request_id: str,
    model_digest: str,
    run: _RunState,
    timer: Callable[[], float],
) -> ResponseRecord:
    common = {
        "record_schema": RECORD_SCHEMA,
        "request_id": request_id,
        "identity": identity,
        "question_id": question.question_id,
        "model": model,
        "model_digest": model_digest,
        "run_id": run.run_id,
        "started_at": datetime.now(UTC).isoformat(),
        "prompt": prompt,
        "evidence": list(refs),
    }
    started = timer()
    try:
        content, raw = client.chat(model, SYSTEM_PROMPT, prompt)
    except CALL_ERRORS as error:
        return ResponseRecord.model_validate(
            {
                **common,
                "status": "error",
                "latency_ms": round((timer() - started) * 1000.0, 1),
                "content": None,
                "usage": {},
                "error": f"{type(error).__name__}: {str(error)[:200]}",
            }
        )
    return ResponseRecord.model_validate(
        {
            **common,
            "status": "ok",
            "latency_ms": round((timer() - started) * 1000.0, 1),
            "content": content,
            "usage": _usage(raw),
            "error": None,
        }
    )


def _finish_model(
    client: OllamaBenchPort, model: str, pinned: str, run: _RunState
) -> None:
    """Unload and re-check the digest; failures are recorded, never raised."""
    errors = run.auxiliary_errors.setdefault(model, [])
    try:
        client.unload(model)
    except CALL_ERRORS as error:
        errors.append(f"unload: {type(error).__name__}")
    try:
        final = client.digest(model)
    except CALL_ERRORS as error:
        errors.append(f"final_digest: {type(error).__name__}")
        run.final_digest[model] = f"unavailable: {type(error).__name__}"
        return
    run.final_digest[model] = "match" if final == pinned else f"mismatch: {final}"


# --- report (pure: manifest + records + labels) ------------------------------


def _passages(record: ResponseRecord) -> tuple[RankedPassage, ...]:
    return tuple(
        RankedPassage(
            chunk_id=UUID(ref.chunk_id),
            article_id=UUID(ref.article_id),
            content_hash=ref.content_hash,
            chunk_version=ref.chunk_version,
            char_start=ref.char_start,
            char_end=ref.char_end,
            text="",
            fused_rank=index,
            lexical_rank=None,
            semantic_rank=None,
        )
        for index, ref in enumerate(record.evidence, 1)
    )


def _outcome(record: ResponseRecord | None, judged: _Judged | None) -> str:
    if record is None:
        return "missing"
    if record.status == "error":
        return "execution_error"
    if judged is None or not judged.valid:
        return "schema_invalid"
    return "abstained" if judged.abstained else "answered"


CONTEXT_NOTE: Final = (
    "model-reported prompt_eval_count; the server may exclude a cached prefix"
)
OUTCOMES: Final = (
    "answered",
    "abstained",
    "schema_invalid",
    "execution_error",
    "missing",
)


@dataclass(frozen=True, slots=True)
class _Plan:
    """The parts of a manifest a model report needs."""

    questions: tuple[EvalQuestion, ...]
    run_id: str
    context: int
    cap: int
    evidence: Mapping[str, Mapping[str, Json]]


@dataclass(slots=True)
class _Tally:
    counts: dict[str, dict[str, int]] = field(
        default_factory=lambda: {
            group: dict.fromkeys(OUTCOMES, 0)
            for group in ("all", "answerable", "unanswerable")
        }
    )
    judged: list[tuple[EvalQuestion, _Judged]] = field(default_factory=list)
    network: list[float] = field(default_factory=list)
    replayed: list[float] = field(default_factory=list)
    tokens: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(
            ("prompt", "output", "prompt_this_run", "output_this_run"), 0
        )
    )
    prompt_counts: list[int] = field(default_factory=list)
    at_risk: int = 0
    cap_hit: int = 0
    no_usage: int = 0
    errors: list[Json] = field(default_factory=list)
    rows: list[Json] = field(default_factory=list)
    responses: list[Json] = field(default_factory=list)


def _matching(
    record: ResponseRecord | None, request_id: str, question: EvalQuestion
) -> ResponseRecord | None:
    if record is None or record.request_id != request_id:
        return None
    if record.identity.get("query_sha256") != sha256_text(question.query):
        return None
    return record


def _count_usage(
    tally: _Tally, record: ResponseRecord, plan: _Plan, *, this_run: bool
) -> None:
    usage = record.usage
    prompt_count = usage.get("prompt_eval_count")
    output_count = usage.get("eval_count")
    (tally.network if this_run else tally.replayed).append(record.latency_ms or 0.0)
    if isinstance(prompt_count, int) and isinstance(output_count, int):
        tally.prompt_counts.append(prompt_count)
        tally.tokens["prompt"] += prompt_count
        tally.tokens["output"] += output_count
        if this_run:
            tally.tokens["prompt_this_run"] += prompt_count
            tally.tokens["output_this_run"] += output_count
        tally.at_risk += prompt_count + plan.cap > plan.context
    else:
        tally.no_usage += 1
    tally.cap_hit += usage.get("done_reason") == "length"


def _tally_question(
    tally: _Tally, question: EvalQuestion, record: ResponseRecord | None, plan: _Plan
) -> None:
    qid = question.question_id
    judged = (
        judge(question, _passages(record), record.content or "")
        if record is not None and record.status == "ok"
        else None
    )
    outcome = _outcome(record, judged)
    tally.counts["all"][outcome] += 1
    tally.counts["answerable" if question.answerable else "unanswerable"][outcome] += 1
    this_run = record is not None and record.run_id == plan.run_id
    if judged is not None:
        tally.judged.append((question, judged))
    if record is not None and record.status == "error":
        tally.errors.append(f"{qid}: {record.error}")
    if record is not None and record.status == "ok":
        _count_usage(tally, record, plan, this_run=this_run)
        tally.responses.append({"question_id": qid, "content": record.content})
    usage = {} if record is None else record.usage
    tally.rows.append(
        {
            "question_id": qid,
            "answerable": question.answerable,
            "outcome": outcome,
            "claims": None if judged is None else judged.claims,
            "linked": None if judged is None else judged.linked,
            "supported": None if judged is None else judged.supported,
            "unsupported_claims": None if judged is None else judged.unsupported_claims,
            "source": None
            if record is None
            else ("network" if this_run else "replayed"),
            "origin_run_id": None if record is None else record.run_id,
            "request_id": None if record is None else record.request_id,
            "latency_ms": None if record is None else record.latency_ms,
            "prompt_eval_count": usage.get("prompt_eval_count"),
            "eval_count": usage.get("eval_count"),
            "done_reason": usage.get("done_reason"),
            "sent_spans": plan.evidence.get(qid, {}).get("sent_spans"),
            "contains_gold": None
            if record is None
            else [_covers(p, question) for p in _passages(record)],
        }
    )


def _rates(tally: _Tally, plan: _Plan) -> dict[str, Json]:
    valid = [(q, j) for q, j in tally.judged if j.valid]
    answerable = [j for q, j in valid if q.answerable]
    unanswerable = [j for q, j in valid if not q.answerable]
    scheduled_answerable = sum(1 for q in plan.questions if q.answerable)
    scheduled_unanswerable = len(plan.questions) - scheduled_answerable
    counts = tally.counts
    return {
        "schema_validity": ratio(len(valid), len(plan.questions), SCHEMA_DEFINITION),
        "citation_linkage": ratio(
            sum(j.linked for _, j in valid),
            len(valid),
            "every claim cites existing source labels (valid outputs)",
        ),
        "semantic_support": ratio(
            sum(j.supported for j in answerable if not j.abstained),
            sum(1 for j in answerable if not j.abstained),
            SUPPORT_DEFINITION,
        ),
        "false_abstention": ratio(
            sum(j.abstained for j in answerable),
            len(answerable),
            "answerable questions the model abstained on (valid outputs)",
        ),
        "false_abstention_scheduled": ratio(
            counts["answerable"]["abstained"],
            scheduled_answerable,
            "abstained / all scheduled answerable questions",
        ),
        "answered_scheduled": ratio(
            counts["answerable"]["answered"],
            scheduled_answerable,
            "answered / all scheduled answerable questions",
        ),
        "false_answer_on_no_answer": ratio(
            sum(not j.abstained for j in unanswerable),
            len(unanswerable),
            "no-answer/false-premise questions answered anyway (valid outputs)",
        ),
        "false_answer_scheduled": ratio(
            counts["unanswerable"]["answered"],
            scheduled_unanswerable,
            "answered / all scheduled no-answer questions",
        ),
        "unsupported_claims": ratio(
            sum(j.unsupported_claims for _, j in valid),
            sum(j.claims for _, j in valid),
            "claims whose cited chunks contain no gold span",
        ),
    }


def _model_report(
    plan: _Plan,
    planned: Mapping[str, str],
    stored: Mapping[str, ResponseRecord],
    run: Mapping[str, Json],
    identity: tuple[str, str],
) -> dict[str, Json]:
    model, model_digest = identity
    tally = _Tally()
    for question in plan.questions:
        qid = question.question_id
        _tally_question(
            tally, question, _matching(stored.get(qid), planned[qid], question), plan
        )
    final = cast("dict[str, str]", run.get("final_digest") or {}).get(model)
    auxiliary = cast("dict[str, list[str]]", run.get("auxiliary_errors") or {})
    return {
        "status": _model_status(tally.counts["all"], len(plan.questions), final),
        "model_digest": model_digest,
        "final_digest_check": final,
        "auxiliary_errors": cast("Json", auxiliary.get(model, [])),
        "scheduled": len(plan.questions),
        "outcomes": cast("Json", tally.counts),
        "calls": {
            "network_this_run": sum(
                1
                for r in tally.rows
                if isinstance(r, dict) and r["source"] == "network"
            ),
            "replayed_from_checkpoint": len(tally.replayed),
        },
        "latency_network": latency_summary(tally.network),
        "latency_replayed": latency_summary(tally.replayed),
        "tokens": cast("Json", tally.tokens),
        "context": {
            "num_ctx": plan.context,
            "num_predict": plan.cap,
            "max_prompt_eval_count": max(tally.prompt_counts, default=None),
            "prompt_plus_cap_over_ctx": tally.at_risk,
            "output_cap_hit": tally.cap_hit,
            "missing_usage": tally.no_usage,
            "note": CONTEXT_NOTE,
        },
        **_rates(tally, plan),
        "errors": tally.errors,
        "questions": tally.rows,
        "responses": tally.responses,
    }


def build_report(
    manifest: Mapping[str, Json], store: CheckpointStore, dataset: SearchDataset
) -> dict[str, Json]:
    """Report of the latest run in ``manifest``; no model call, deterministic."""
    runs = cast("list[dict[str, Json]]", manifest["runs"])
    run = runs[-1]
    known = {q.question_id: q for q in dataset.questions}
    question_ids = cast("list[str]", manifest["question_ids"])
    absent = [q for q in question_ids if q not in known]
    if absent:
        msg = f"labels missing for scheduled questions {absent[:5]}"
        raise OllamaBenchPreflightError(msg)
    evidence_rows = cast("dict[str, dict[str, Json]]", manifest["evidence"])
    plan = _Plan(
        questions=tuple(known[q] for q in question_ids),
        run_id=cast("str", run["run_id"]),
        context=cast("int", manifest["context_tokens"]),
        cap=cast("int", manifest["output_cap_tokens"]),
        evidence=evidence_rows,
    )
    pinned = cast("dict[str, str]", manifest["models"])
    requests = cast("dict[str, dict[str, str]]", manifest["requests"])
    report_models: dict[str, Json] = {
        model: _model_report(
            plan, planned, store.records(model), run, (model, pinned[model])
        )
        for model, planned in requests.items()
    }
    statuses = [
        cast("str", cast("dict[str, Json]", body)["status"])
        for body in report_models.values()
    ]
    state = cast("str", run.get("state"))
    finished = state == "finished"
    overall = (
        "failed"
        if "failed" in statuses
        else "complete"
        if statuses and finished and all(s == "complete" for s in statuses)
        else "partial"
    )
    report: dict[str, Json] = {
        "schema_version": SCHEMA_VERSION,
        "offline_only": True,
        "status": overall,
        "run_id": plan.run_id,
        "run_state": state,
        "runs_in_checkpoint": len(runs),
        "checkpoint_rejected": run.get("checkpoint_rejected") or [],
        "prompt_version": manifest["prompt_version"],
        "generation": manifest["generation"],
        "seed": manifest["seed"],
        "output_cap_tokens": plan.cap,
        "context_tokens": plan.context,
        "evidence_k": manifest["evidence_k"],
        "evidence_truncated_questions": sum(
            1
            for row in evidence_rows.values()
            if row.get("payload_spans") != row.get("sent_spans")
        ),
        "evidence_payloads": cast("Json", evidence_rows),
        "questions": len(plan.questions),
        "question_ids": cast("Json", question_ids),
        "fixture_digest": dataset.fixture_digest,
        "manifest_fixture_digest": manifest["fixture_digest"],
        "corpus_digest": manifest["corpus_digest"],
        "evidence_configuration": manifest["evidence_configuration"],
        "models": report_models,
    }
    report["report_digest"] = digest(report)
    return report


def _model_status(counts: Mapping[str, int], scheduled: int, final: str | None) -> str:
    if final is not None and final.startswith("mismatch"):
        return "failed"
    if counts["answered"] + counts["abstained"] + counts["schema_invalid"] == 0:
        return "failed"
    complete = counts["execution_error"] == 0 and counts["missing"] == 0
    return "complete" if complete and scheduled > 0 else "partial"


def rebuild_report(store: CheckpointStore, dataset: SearchDataset) -> dict[str, Json]:
    """Offline report from the stored manifest and records (no model call)."""
    manifest = store.load_manifest()
    if manifest is None:
        msg = f"no readable manifest in {store.root}"
        raise OllamaBenchPreflightError(msg)
    return build_report(manifest, store, dataset)


def shared_view(report: Mapping[str, Json]) -> dict[str, Json]:
    """Text-free copy (no model output, no prompt, no article text)."""
    models = cast("dict[str, dict[str, Json]]", report["models"])
    return {
        **report,
        "models": {
            name: {k: v for k, v in body.items() if k != "responses"}
            for name, body in models.items()
        },
        "text_free": True,
    }
