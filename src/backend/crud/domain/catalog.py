"""Catalog request values of the web API (no filter semantics here).

The single implementation of catalog semantics lives behind the RAG catalog
API; crud-api only bounds request sizes and forwards the parameters
verbatim, as repeated query parameters.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


def _iso(value: datetime) -> str:
    return value.isoformat()


@dataclass(frozen=True, slots=True)
class CatalogFilterParams:
    """Public filter parameters exactly as received."""

    q: str | None = None
    topic_ids: tuple[str, ...] = ()
    location_ids: tuple[str, ...] = ()
    source_ids: tuple[UUID, ...] = ()
    published_after: datetime | None = None
    published_before: datetime | None = None
    include_descendants: bool = True

    def items(self) -> tuple[tuple[str, str], ...]:
        """Repeated ``(name, value)`` pairs for the upstream query string."""
        pairs: list[tuple[str, str]] = []
        if self.q is not None:
            pairs.append(("q", self.q))
        pairs.extend(("topic_ids", t) for t in self.topic_ids)
        pairs.extend(("location_ids", loc) for loc in self.location_ids)
        pairs.extend(("source_ids", str(s)) for s in self.source_ids)
        if self.published_after is not None:
            pairs.append(("published_after", _iso(self.published_after)))
        if self.published_before is not None:
            pairs.append(("published_before", _iso(self.published_before)))
        pairs.append(
            ("include_descendants", "true" if self.include_descendants else "false")
        )
        return tuple(pairs)


@dataclass(frozen=True, slots=True)
class ArticleListParams:
    """Filters plus sort, opaque cursor and page size."""

    filters: CatalogFilterParams
    sort: str | None = None
    cursor: str | None = None
    limit: int = 20

    def items(self) -> tuple[tuple[str, str], ...]:
        """Upstream query parameters."""
        pairs = list(self.filters.items())
        if self.sort is not None:
            pairs.append(("sort", self.sort))
        if self.cursor is not None:
            pairs.append(("cursor", self.cursor))
        pairs.append(("limit", str(self.limit)))
        return tuple(pairs)
