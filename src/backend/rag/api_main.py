"""rag-api entry point (port 8001): ``/health`` and the briefing routes.

The runtime (and its compiled query graph) is composed once at import time by
``backend.rag.composition``; no adapter is constructed here.
"""

import os

import uvicorn
from fastapi import FastAPI

from backend.rag.composition import BriefingRuntime, runtime_from_environment
from backend.rag.presentation.briefing_http import (
    build_briefing_router,
    install_error_handlers,
)
from backend.rag.presentation.health import router as health_router

HOST = "127.0.0.1"
PORT = 8001


def create_app(runtime: BriefingRuntime | None = None) -> FastAPI:
    """Compose HTTP adapters into application."""
    runtime = runtime or runtime_from_environment(os.environ)
    application = FastAPI(title="EventLens RAG API")
    application.include_router(health_router)
    application.include_router(
        build_briefing_router(runtime.use_case, runtime.sources, runtime.callers)
    )
    install_error_handlers(application)
    return application


app = create_app()


def run() -> None:
    """Run rag-api server."""
    uvicorn.run("backend.rag.api_main:app", host=HOST, port=PORT)
