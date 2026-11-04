"""crud-api entry point (port 8000): health plus the public catalog API.

Catalog data is owned by the RAG context and read through
``ArticleCatalogPort`` (HTTP adapter to the rag-api internal catalog); crud-api
holds no article tables and never connects to the RAG database.
"""

import os

import uvicorn
from fastapi import FastAPI

from backend.crud.composition import CrudRuntime, runtime_from_environment
from backend.crud.presentation.articles import build_articles_router
from backend.crud.presentation.errors import install_errors
from backend.crud.presentation.health import router as health_router
from backend.crud.presentation.taxonomy import build_taxonomy_router

HOST = "127.0.0.1"
PORT = 8000


def create_app(runtime: CrudRuntime | None = None) -> FastAPI:
    """Compose CRUD HTTP adapters into application."""
    runtime = runtime or runtime_from_environment(os.environ)
    application = FastAPI(title="EventLens CRUD API")
    application.include_router(health_router)
    application.include_router(build_articles_router(runtime.articles))
    application.include_router(build_taxonomy_router(runtime.taxonomy))
    install_errors(application)
    return application


app = create_app()


def run() -> None:
    """Run crud-api server."""
    uvicorn.run("backend.crud.main:app", host=HOST, port=PORT)
