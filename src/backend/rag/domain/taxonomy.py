"""Topic/location labels derived from one validated enrichment publication.

Labels describe what the article content is about: locations come only from
the enrichment's evidence-backed ``locations`` (never from the publisher's
domain or newsroom). A location is labelled only when its canonical name or a
surface form matches exactly one gazetteer alias key; anything else stays
unknown and is counted, never guessed.
"""

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from backend.shared.contracts import ArticleEnrichment

LOCATION_LABEL_TYPES: Final = frozenset({"country", "region", "city"})


def location_alias_key(text: str) -> str:
    """Casefold, đ→d, strip combining marks, keep alphanumeric words.

    Identical to the frozen ``alias_key`` of revision ``rag_0004``.
    """
    folded = unicodedata.normalize("NFD", text.casefold().replace("đ", "d"))
    plain = "".join(c for c in folded if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^0-9a-z]+", " ", plain).split())


@dataclass(frozen=True, slots=True)
class DerivedLabel:
    """One label proposed by an enrichment publication."""

    kind: str
    label_id: str
    match_method: str
    confidence: float | None
    evidence_span_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DerivedLabels:
    """Labels plus the number of enrichment locations left unknown."""

    labels: tuple[DerivedLabel, ...]
    unmatched_locations: int


def derive_labels(
    metadata: ArticleEnrichment, aliases: Mapping[str, str]
) -> DerivedLabels:
    """Topics as published; locations through exact alias-key matches."""
    topic_confidence = next(
        (f.confidence for f in metadata.field_confidence if f.field == "topics"), None
    )
    labels: dict[tuple[str, str], DerivedLabel] = {}
    for topic in metadata.topics:
        labels[("topic", topic)] = DerivedLabel(
            "topic", topic, "enrichment_topic", topic_confidence, ()
        )
    unmatched = 0
    for location in metadata.locations:
        if location.location_type not in LOCATION_LABEL_TYPES:
            continue
        names = (location.canonical_name, *location.surface_forms)
        matches = {
            aliases[key]
            for key in (location_alias_key(n) for n in names)
            if key in aliases
        }
        if len(matches) != 1:
            unmatched += 1
            continue
        location_id = matches.pop()
        _ = labels.setdefault(
            ("location", location_id),
            DerivedLabel(
                "location",
                location_id,
                "gazetteer_alias",
                location.confidence,
                tuple(str(s) for s in location.evidence_span_ids),
            ),
        )
    return DerivedLabels(tuple(labels.values()), unmatched)
