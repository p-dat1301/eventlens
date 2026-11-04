"""``/api/v1/topics``, ``/api/v1/locations``, ``/api/v1/sources`` (crud-api)."""

from typing import Annotated

from fastapi import APIRouter, Query

from backend.crud.application.use_cases.taxonomy import BrowseTaxonomy
from backend.crud.presentation.articles import ERRORS, off_loop
from backend.shared.contracts.catalog import LocationListV1, SourceListV1, TopicListV1


def build_taxonomy_router(taxonomy: BrowseTaxonomy) -> APIRouter:
    """Reference-data routes for building filters."""
    router = APIRouter(prefix="/api/v1", responses=ERRORS)

    @router.get("/topics")
    async def topics() -> TopicListV1:
        return await off_loop(taxonomy.topics)

    @router.get("/locations")
    async def locations(
        parent_id: Annotated[str | None, Query(pattern=r"^[a-z0-9-]{1,80}$")] = None,
        q: Annotated[str | None, Query(max_length=200)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
    ) -> LocationListV1:
        return await off_loop(lambda: taxonomy.locations(parent_id, q, limit))

    @router.get("/sources")
    async def sources() -> SourceListV1:
        return await off_loop(taxonomy.sources)

    return router
