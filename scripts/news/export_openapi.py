"""Write the crud-api and rag-api OpenAPI documents (no database, no network).

    .venv/bin/python scripts/news/export_openapi.py --output docs/openapi

Apps are composed without storage; the schemas describe every route and DTO.
"""

import argparse
import json
from pathlib import Path

from backend.crud.main import create_app as create_crud
from backend.rag.api_main import create_app as create_rag
from backend.rag.composition import compose_briefing, fixture_store, news_runtime


def main() -> None:
    """Export both documents with sorted keys (stable diffs)."""
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("--output", type=Path, default=Path("docs/openapi"))
    output = parser.parse_args().output
    assert isinstance(output, Path)
    output.mkdir(parents=True, exist_ok=True)
    runtime = compose_briefing(fixture_store())
    documents = {
        "crud-api.openapi.json": create_crud().openapi(),
        "rag-api.openapi.json": create_rag(runtime, news_runtime({}, runtime)).openapi(),
    }
    for name, document in documents.items():
        path = output / name
        _ = path.write_text(
            json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {path} ({len(document['paths'])} paths)")  # noqa: T201


if __name__ == "__main__":
    main()
