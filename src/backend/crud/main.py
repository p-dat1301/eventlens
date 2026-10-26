"""crud-api entry point (port 8000): mounts CRUD routers only."""

import uvicorn
from fastapi import FastAPI

from backend.crud.presentation.health import router as health_router

HOST = "127.0.0.1"
PORT = 8000


def create_app() -> FastAPI:
    """Compose CRUD HTTP adapters into application."""
    application = FastAPI(title="EventLens CRUD API")
    application.include_router(health_router)
    return application


app = create_app()


def run() -> None:
    """Run crud-api server."""
    uvicorn.run("backend.crud.main:app", host=HOST, port=PORT)
