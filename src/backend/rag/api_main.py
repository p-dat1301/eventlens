"""rag-api entry point (port 8001): health, briefing, catalog and chat routes.

The runtimes (and the compiled query graph) are composed once at import time
by ``backend.rag.composition``; no adapter is constructed here. Routes:

* ``/health``, ``POST /briefing``, ``GET /briefing/sources/{ref}`` (legacy);
* ``/internal/v1/catalog/*`` (service token; never proxied publicly);
* ``/api/v1/sessions``, ``/api/v1/conversations*``, ``/api/v1/runs*``.
"""

import os

import uvicorn
from fastapi import FastAPI

from backend.rag.composition import (
    BriefingRuntime,
    NewsRuntime,
    news_runtime,
    runtime_from_environment,
)
from backend.rag.presentation.api_errors import install_api_errors
from backend.rag.presentation.briefing_http import (
    build_briefing_router,
    error_response,
    install_error_handlers,
)
from backend.rag.presentation.catalog_http import build_catalog_router
from backend.rag.presentation.conversations_http import build_conversation_router
from backend.rag.presentation.health import router as health_router
from backend.rag.presentation.unavailable import unavailable_catalog

HOST = "127.0.0.1"
PORT = 8001


def create_app(
    runtime: BriefingRuntime | None = None, news: NewsRuntime | None = None
) -> FastAPI:
    """Compose HTTP adapters into application."""
    runtime = runtime or runtime_from_environment(os.environ)
    news = news or news_runtime(os.environ, runtime)
    application = FastAPI(title="EventLens RAG API")
    application.include_router(health_router)
    application.include_router(
        build_briefing_router(runtime.use_case, runtime.sources, runtime.callers)
    )
    application.include_router(
        build_catalog_router(news.catalog or unavailable_catalog(), news.service_auth)
    )
    application.include_router(build_conversation_router(news.chat))
    install_error_handlers(application)
    install_api_errors(application, legacy_validation=error_response)
    return application


app = create_app()


def run() -> None:
    """Run rag-api server."""
    uvicorn.run("backend.rag.api_main:app", host=HOST, port=PORT)
