"""Typer adapter for worker use cases."""

from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from eventlens_worker.application import process_bundle_json, validate_bundle_json
from eventlens_worker.fake_extractor import FakeEventExtractor
from eventlens_worker.memory_writer import InMemoryEventWriter
from eventlens_worker.runner import PipelineRunner, RunnerPorts

app = typer.Typer(no_args_is_help=True)

BundlePath = Annotated[
    Path,
    typer.Argument(exists=True, file_okay=True, dir_okay=False, readable=True),
]
FILE_READ_ERROR = "error:file_read_failed"
VALIDATION_ERROR = "error:bundle_validation_failed"


def _read_bundle(bundle_path: Path) -> str:
    """Read UTF-8 bundle text or map file errors to stable CLI output."""
    try:
        return bundle_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        typer.echo(FILE_READ_ERROR, err=True)
        raise typer.Exit(code=2) from error


@app.callback()
def worker() -> None:
    """Run EventLens worker commands."""


@app.command("validate")
def validate_command(bundle_path: BundlePath) -> None:
    """Validate one article bundle file."""
    try:
        result = validate_bundle_json(_read_bundle(bundle_path))
    except ValidationError as error:
        typer.echo(VALIDATION_ERROR, err=True)
        raise typer.Exit(code=2) from error
    typer.echo(
        f"Valid article {result.article_id} with {result.media_count} media items."
    )


@app.command("process")
def process_command(bundle_path: BundlePath) -> None:
    """Process one article bundle file through local adapters."""
    pipeline = PipelineRunner(
        ports=RunnerPorts(
            extractor=FakeEventExtractor(),
            writer=InMemoryEventWriter(),
        ),
        max_items=1,
    )
    try:
        trace = process_bundle_json(
            _read_bundle(bundle_path),
            pipeline,
        )
    except ValidationError as error:
        typer.echo(VALIDATION_ERROR, err=True)
        raise typer.Exit(code=2) from error
    typer.echo(trace.model_dump_json())
