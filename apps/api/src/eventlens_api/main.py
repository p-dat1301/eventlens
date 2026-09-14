"""API composition root and process entry point."""

import uvicorn
from fastapi import FastAPI

from eventlens_api.http import router


def create_app() -> FastAPI:
    """Compose HTTP adapters into application."""
    application = FastAPI(title="EventLens API")
    application.include_router(router)
    return application


app = create_app()


def run() -> None:
    """Run API server."""
    uvicorn.run("eventlens_api.main:app", host="127.0.0.1", port=8000)
