"""Typer adapter for worker use cases."""

from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from eventlens_worker.application import validate_bundle_json

app = typer.Typer(no_args_is_help=True)

BundlePath = Annotated[
    Path,
    typer.Argument(exists=True, file_okay=True, dir_okay=False, readable=True),
]


@app.callback()
def worker() -> None:
    """Run EventLens worker commands."""


@app.command("validate")
def validate_command(bundle_path: BundlePath) -> None:
    """Validate one article bundle file."""
    try:
        result = validate_bundle_json(bundle_path.read_text(encoding="utf-8"))
    except ValidationError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2) from error
    typer.echo(
        f"Valid article {result.article_id} with {result.media_count} media items."
    )
