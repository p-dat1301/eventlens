"""Enrichment field metrics: fake output vs manual gold, with denominators."""

from collections.abc import Callable, Iterable
from typing import get_args
from uuid import UUID

from backend.rag.domain.article_document import ArticleDocument
from backend.shared.contracts import ArticleEnrichment
from backend.shared.contracts.taxonomy import TopicTag

from .metrics import Json, calibration, precision_recall, ratio, set_counts

TAXONOMY = frozenset(get_args(TopicTag))
ABSTAIN_TOPIC = "other"
ABSTAIN_RELEVANCE = "uncertain"
type Extract = Callable[[ArticleEnrichment], Iterable[str]]


def _names(values: Iterable[object]) -> set[str]:
    return {str(getattr(v, "canonical_name", "")).casefold() for v in values}


SET_FIELDS: dict[str, Extract] = {
    "topic": lambda m: set(m.topics),
    "entity": lambda m: _names(m.entities),
    "location": lambda m: _names(m.locations),
}
LABEL_FIELDS: dict[str, Callable[[ArticleEnrichment], str | None]] = {
    "content_type": lambda m: m.content_type,
    "event_relevance": lambda m: m.event_relevance,
}


def set_field(
    name: str,
    extract: Extract,
    predicted: dict[UUID, ArticleEnrichment],
    gold: dict[UUID, ArticleEnrichment],
) -> dict[str, Json]:
    """Micro precision/recall over (article, value) pairs of one set field."""
    tp = fp = fn = 0
    for article_id, truth in gold.items():
        guess = predicted.get(article_id)
        counts = set_counts(() if guess is None else extract(guess), extract(truth))
        tp, fp, fn = tp + counts[0], fp + counts[1], fn + counts[2]
    report = precision_recall(tp, fp, fn, f"(article, {name}) pairs")
    report["gold_articles_with_labels"] = sum(
        1 for truth in gold.values() if set(extract(truth))
    )
    return report


def label_field(
    extract: Callable[[ArticleEnrichment], str | None],
    predicted: dict[UUID, ArticleEnrichment],
    gold: dict[UUID, ArticleEnrichment],
) -> dict[str, Json]:
    """Accuracy and per-class precision/recall of one single-label field."""
    classes = sorted({c for t in gold.values() if (c := extract(t)) is not None})
    per_class: dict[str, Json] = {}
    for label in classes:
        tp = fp = fn = 0
        for article_id, truth in gold.items():
            guess = predicted.get(article_id)
            said = None if guess is None else extract(guess)
            is_gold = extract(truth) == label
            tp += said == label and is_gold
            fp += said == label and not is_gold
            fn += said != label and is_gold
        per_class[label] = precision_recall(tp, fp, fn, f"articles labelled {label}")
    correct = sum(
        1
        for article_id, truth in gold.items()
        if (guess := predicted.get(article_id)) is not None
        and extract(guess) == extract(truth)
    )
    return {
        "accuracy": ratio(correct, len(gold), "articles with the gold label / gold"),
        "per_class": per_class,
    }


def evidence_metrics(
    predicted: dict[UUID, ArticleEnrichment], articles: dict[UUID, ArticleDocument]
) -> dict[str, Json]:
    """Structural evidence-span validity; semantic support not measured."""
    spans = [
        (articles[m.article_id], s)
        for m in predicted.values()
        for s in m.evidence_spans
    ]
    valid = sum(
        1
        for doc, s in spans
        if s.content_hash == doc.content_hash
        and doc.normalized_content[s.start_offset : s.end_offset] == s.excerpt
    )
    return {
        "structural_span_validity": ratio(
            valid,
            len(spans),
            "published spans whose hash matches and offsets select the excerpt",
        ),
        "semantic_support": {
            "value": None,
            "status": "NOT MEASURED",
            "reason": "no labelled topic-evidence set; spans checked structurally",
        },
    }


def _topic_marker(metadata: ArticleEnrichment) -> str:
    return ABSTAIN_TOPIC if metadata.topics == (ABSTAIN_TOPIC,) else ""


def _relevance_marker(metadata: ArticleEnrichment) -> str:
    return metadata.event_relevance or ""


def abstention_metrics(
    predicted: dict[UUID, ArticleEnrichment], gold: dict[UUID, ArticleEnrichment]
) -> dict[str, Json]:
    """How often 'other'/'uncertain' was chosen and whether gold agrees."""
    report: dict[str, Json] = {}
    markers: tuple[tuple[str, str, Callable[[ArticleEnrichment], str]], ...] = (
        ("topic", ABSTAIN_TOPIC, _topic_marker),
        ("event_relevance", ABSTAIN_RELEVANCE, _relevance_marker),
    )
    for field, marker, extract in markers:
        abstained = [a for a, m in predicted.items() if extract(m) == marker]
        should = [a for a, m in gold.items() if extract(m) == marker]
        report[field] = {
            "abstained": len(abstained),
            "gold_abstain": len(should),
            "appropriate": ratio(
                len(set(abstained) & set(should)),
                len(abstained),
                f"predicted '{marker}' where gold is also '{marker}' / predicted",
            ),
            "abstention_recall": ratio(
                len(set(abstained) & set(should)),
                len(should),
                f"gold '{marker}' that the enricher also abstained on / gold",
            ),
        }
    return report


def hallucination_metrics(
    predicted: dict[UUID, ArticleEnrichment], gold: dict[UUID, ArticleEnrichment]
) -> dict[str, Json]:
    """Predicted values absent from gold, per field."""
    report: dict[str, Json] = {}
    for name in ("entity", "location", "topic"):
        extract = SET_FIELDS[name]
        made = unsupported = 0
        for article_id, guess in predicted.items():
            values = set(extract(guess))
            made += len(values)
            unsupported += len(values - set(extract(gold[article_id])))
        report[name] = ratio(
            unsupported, made, f"predicted {name} values absent from gold / predicted"
        )
    return report


def calibration_metrics(
    predicted: dict[UUID, ArticleEnrichment], gold: dict[UUID, ArticleEnrichment]
) -> dict[str, Json]:
    """Topic confidence vs exact topic-set correctness (Brier, ECE)."""
    pairs = [
        (fc.confidence, set(m.topics) == set(gold[a].topics))
        for a, m in sorted(predicted.items(), key=lambda item: str(item[0]))
        for fc in m.field_confidence
        if fc.field == "topics"
    ]
    report = calibration(pairs)
    report["correct_definition"] = "predicted topic set equals gold topic set"
    return report


def taxonomy_metrics(predicted: dict[UUID, ArticleEnrichment]) -> dict[str, Json]:
    """Published topics inside the controlled taxonomy."""
    values = [t for m in predicted.values() for t in m.topics]
    return {
        "published_topics_in_taxonomy": ratio(
            sum(t in TAXONOMY for t in values),
            len(values),
            "published topic values inside the controlled TopicTag set",
        ),
        "suggested_tags_promoted_to_topics": sum(
            1 for m in predicted.values() for tag in m.suggested_tags if tag in m.topics
        ),
    }
