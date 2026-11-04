"""Run every configuration N times and build the two report halves.

* deterministic report (``search_eval_report.v1``): configuration identity,
  quality metrics, scope violations, repeat consistency; digested.
* measured report: latency percentiles, indexing time, errors, run metadata
  (git commit, model digests) that legitimately change between machines.
"""

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from backend.rag.domain.query_intent import INTENT_CLASSIFIER_VERSION, classify_intent
from backend.rag.evaluation.metrics import Json, digest, latency_summary, ratio, strs
from backend.rag.evaluation.search_eval.breakdown import (
    breakdown,
    by_split,
    question_rows,
    span_ceiling,
)
from backend.rag.evaluation.search_eval.dataset import EvalQuestion, SearchDataset
from backend.rag.evaluation.search_eval.evidence_scoring import (
    EvidenceScore,
    aggregate_evidence,
    call_counts,
    evidence_rows,
    score_evidence,
)
from backend.rag.evaluation.search_eval.scoring import (
    QuestionScore,
    aggregate,
    score_question,
)
from backend.rag.evaluation.search_eval.systems import (
    SearchSystem,
    SystemAnswer,
    chunk_length_summary,
)

SCHEMA = "search_eval_report.v1"
MIN_REPEATS = 3


@dataclass(slots=True)
class _Run:
    scores: list[QuestionScore] = field(default_factory=list[QuestionScore])
    latencies: dict[str, list[float]] = field(default_factory=dict[str, list[float]])
    query_tokens: int = 0
    errors: list[str] = field(default_factory=list[str])
    rerank: dict[str, int] = field(default_factory=dict[str, int])
    evidence: list[EvidenceScore] = field(default_factory=list[EvidenceScore])


AnswerSink = Callable[[str, EvalQuestion, SystemAnswer], None]


class Checkpoints(Protocol):
    """Finished configurations of an interrupted run (resume support)."""

    def load(self, name: str, key: str) -> tuple[Json, Json] | None:
        """``(entry, measured)`` saved under the same run key, else ``None``."""
        ...

    def save(self, name: str, key: str, entry: Json, measured: Json) -> None:
        """Persist one finished configuration."""
        ...


def run_key(dataset: SearchDataset, repeats: int, metadata: dict[str, Json]) -> str:
    """Identity a checkpoint must match: corpus, labels, repeats, code tree."""
    return digest(
        {
            "corpus": dataset.corpus_digest,
            "labels": dataset.fixture_digest,
            "repeats": repeats,
            "query_clock": dataset.as_of.isoformat(),
            "articles_sha256": dataset.articles_sha256,
            "export_id": dataset.export_id,
            "execution": {
                k: metadata.get(k)
                for k in (
                    "embeddings",
                    "bge_m3_revision",
                    "rerank_revision",
                    "rerank",
                    "device",
                    "default_chunker",
                    "python",
                )
            },
            "worktree": metadata.get("worktree_digest"),
            "commit": metadata.get("git_commit"),
        }
    )


def _one_run(
    system: SearchSystem, dataset: SearchDataset, sink: AnswerSink | None = None
) -> _Run:
    run = _Run()
    for question in dataset.questions:
        try:
            answer = system.search(question)
        except Exception as error:  # noqa: BLE001 - recorded, never hidden
            run.errors.append(f"{question.question_id}: {type(error).__name__}")
            continue
        if sink is not None:
            sink(system.name, question, answer)
        run.scores.append(
            score_question(question, answer.ranked, trap_hits=answer.trap_hits)
        )
        if answer.evidence is not None:
            run.evidence.append(score_evidence(question, answer.evidence))
        run.query_tokens += answer.query_tokens
        for stage, value in answer.timings_ms.items():
            run.latencies.setdefault(stage, []).append(value)
        if answer.rerank_status is not None:
            run.rerank[answer.rerank_status] = (
                run.rerank.get(answer.rerank_status, 0) + 1
            )
    return run


def _consistency(runs: Sequence[_Run]) -> dict[str, Json]:
    per_question: dict[str, set[tuple[str, ...]]] = {}
    for run in runs:
        for score in run.scores:
            per_question.setdefault(score.question_id, set()).add(score.top_chunks)
    stable = sum(1 for orders in per_question.values() if len(orders) == 1)
    return ratio(
        stable,
        len(per_question),
        "questions whose top-10 chunk list is identical in every repeat",
    )


def _intent_accuracy(dataset: SearchDataset) -> dict[str, Json]:
    if all(q.intent == "unlabeled" for q in dataset.questions):
        return {
            "classifier_version": INTENT_CLASSIFIER_VERSION,
            "accuracy": None,
            "note": "labels carry categories, not query intents; not scored",
        }
    hits = sum(
        classify_intent(q.query).intent.value == q.intent for q in dataset.questions
    )
    confusion: dict[str, Json] = {}
    for question in dataset.questions:
        predicted = classify_intent(question.query).intent.value
        key = f"{question.intent}->{predicted}"
        current = confusion.get(key, 0)
        confusion[key] = (current if isinstance(current, int) else 0) + 1
    return {
        "classifier_version": INTENT_CLASSIFIER_VERSION,
        "accuracy": ratio(hits, len(dataset.questions), "rule intent == label"),
        "confusion": confusion,
    }


def evaluate(  # noqa: PLR0913 - keyword-only hooks after the inputs
    dataset: SearchDataset,
    systems: Sequence[SearchSystem],
    repeats: int,
    metadata: dict[str, Json],
    *,
    on_progress: Callable[[str], None] = lambda _: None,
    sink: AnswerSink | None = None,
    checkpoints: Checkpoints | None = None,
) -> tuple[dict[str, Json], dict[str, Json]]:
    """Run ``repeats`` (>= 3) passes per system; returns (deterministic, measured).

    ``sink`` receives every answer of the first repeat (with chunk text) so
    the caller can write private review material; reports stay text-free.
    """
    repeats = max(repeats, MIN_REPEATS)
    configs: dict[str, Json] = {}
    measured: dict[str, Json] = {}
    key = run_key(dataset, repeats, metadata)
    resumed: list[str] = []
    for system in systems:
        # Generation is assigned by prepare(), not an experiment parameter.
        configuration = {
            k: v for k, v in system.describe().items() if k != "index_generation"
        }
        config_key = digest({"run": key, "configuration": configuration})
        # Checkpoints store scores, not private evidence; regenerate review data.
        saved = (
            None
            if checkpoints is None or sink is not None
            else checkpoints.load(system.name, config_key)
        )
        if saved is not None:
            on_progress(f"{system.name}: resumed from checkpoint")
            configs[system.name], measured[system.name] = saved
            resumed.append(system.name)
            system.close()
            continue
        on_progress(f"{system.name}: indexing")
        try:
            indexing = system.prepare(dataset.articles)
            runs: list[_Run] = []
            for repeat in range(repeats):
                on_progress(f"{system.name}: run {repeat + 1}/{repeats}")
                runs.append(_one_run(system, dataset, sink if repeat == 0 else None))
            description = system.describe()
            layout = system.chunk_layout()
        finally:
            system.close()
        first = runs[0]
        entry: dict[str, Json] = {
            "configuration": description,
            "metrics": aggregate(dataset.questions, first.scores),
            "repeat_consistency": _consistency(runs),
            "metrics_identical_across_repeats": all(
                aggregate(dataset.questions, r.scores)
                == aggregate(dataset.questions, first.scores)
                for r in runs
            ),
            "errors": strs(sorted({e for r in runs for e in r.errors})),
            "questions_scored": len(first.scores),
            "chunks": chunk_length_summary(layout),
            "gold_span_ceiling": span_ceiling(dataset, layout),
            "metrics_by_split": by_split(dataset, first.scores),
            "breakdown": breakdown(dataset, first.scores),
            "per_question": question_rows(first.scores),
        }
        if first.evidence:
            _add_evidence(entry, dataset, runs)
        configs[system.name] = entry
        stages = sorted({stage for r in runs for stage in r.latencies})
        latency: dict[str, Json] = {
            stage: latency_summary(
                [v for r in runs for v in r.latencies.get(stage, [])]
            )
            for stage in stages
        }
        rerank: dict[str, Json] = dict(first.rerank)
        measured[system.name] = {
            "indexing": indexing,
            "latency": latency,
            # Repeat 1 runs right after prepare() (index built, model loaded);
            # repeats 2+ reuse warm process, engine and OS caches.
            "latency_by_repeat": [
                {stage: latency_summary(r.latencies.get(stage, [])) for stage in stages}
                for r in runs
            ],
            "query_tokens_whitespace": first.query_tokens,
            "rerank_outcomes": rerank,
            "repeats": repeats,
        }
        if checkpoints is not None:
            checkpoints.save(system.name, config_key, entry, measured[system.name])
    deterministic: dict[str, Json] = {
        "schema_version": SCHEMA,
        "dataset": {
            "kind": dataset.kind,
            "version": str(dataset.manifest["version"]),
            "export_id": dataset.export_id,
            "articles_sha256": dataset.articles_sha256,
            "fixture_digest": dataset.fixture_digest,
            "label_digest": dataset.label_digest,
            "corpus_digest": dataset.corpus_digest,
            "articles": len(dataset.articles),
            "questions": len(dataset.questions),
            "labels": dataset.labels,
            "label_status": dict(
                sorted(Counter(q.label_status for q in dataset.questions).items())
            ),
            "ai_prescreen": dict(
                sorted(
                    Counter(q.ai_prescreen or "none" for q in dataset.questions).items()
                )
            ),
            "splits": dict(sorted(Counter(q.split for q in dataset.questions).items())),
            "label_issues": [dict(i) for i in dataset.label_issues],
            "query_clock": dataset.as_of.isoformat(),
        },
        "repeats": repeats,
        "intent_classifier": _intent_accuracy(dataset),
        "configurations": configs,
    }
    deterministic["report_digest"] = digest(deterministic)
    return deterministic, {
        "metadata": {**metadata, "resumed_configurations": strs(resumed)},
        "configurations": measured,
    }


def _add_evidence(
    entry: dict[str, Json], dataset: SearchDataset, runs: Sequence[_Run]
) -> None:
    """B/C metrics, splits, categories, payload repeatability and calls."""
    first = runs[0]
    split_of = {q.question_id: q.split for q in dataset.questions}
    entry["evidence"] = aggregate_evidence(first.evidence)
    entry["evidence_by_split"] = {
        split: aggregate_evidence(
            [s for s in first.evidence if split_of[s.question_id] == split]
        )
        for split in sorted(set(split_of.values()))
    }
    entry["evidence_by_category"] = {
        category: aggregate_evidence(
            [s for s in first.evidence if (s.category or "none") == category]
        )
        for category in sorted({s.category or "none" for s in first.evidence})
    }
    entry["calls"] = call_counts(first.evidence)
    payloads: dict[str, set[tuple[tuple[str, int, int], ...]]] = {}
    for run in runs:
        for score in run.evidence:
            payloads.setdefault(score.question_id, set()).add(
                tuple((p.article_id, p.start, p.end) for p in score.stages.payload)
            )
    entry["payload_repeat_consistency"] = ratio(
        sum(1 for v in payloads.values() if len(v) == 1),
        len(payloads),
        "questions whose payload spans are identical in every repeat",
    )
    rows = evidence_rows(first.evidence)
    per_question = entry["per_question"]
    assert isinstance(per_question, list)  # noqa: S101
    for row in per_question:
        if isinstance(row, dict):
            row.update(rows.get(str(row["question_id"]), {}))


def failing_configurations(report: dict[str, Json]) -> list[str]:
    """Configurations with scope violations or per-question errors."""
    configs = report.get("configurations")
    if not isinstance(configs, dict):
        return ["<no configurations>"]
    failing: list[str] = []
    for name, entry in configs.items():
        if not isinstance(entry, dict):
            failing.append(name)
            continue
        metrics = entry.get("metrics")
        violations = metrics.get("scope_violations") if isinstance(metrics, dict) else 1
        if violations or entry.get("errors"):
            failing.append(name)
    return failing
