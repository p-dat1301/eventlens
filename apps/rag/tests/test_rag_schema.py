from collections.abc import Callable
from pathlib import Path

import pytest

from eventlens_rag.schema import (
    article_document_schema_json,
    briefing_answer_schema_json,
    briefing_query_schema_json,
)

SCHEMA_DIRECTORY = Path(__file__).parents[3] / "contracts"


@pytest.mark.parametrize(
    ("artifact_name", "renderer"),
    [
        ("article_document.v1.schema.json", article_document_schema_json),
        ("briefing_query.v1.schema.json", briefing_query_schema_json),
        ("briefing_answer.v1.schema.json", briefing_answer_schema_json),
    ],
)
def test_rag_schema_artifact_matches_model(
    artifact_name: str, renderer: Callable[[], str]
) -> None:
    artifact = (SCHEMA_DIRECTORY / artifact_name).read_text(encoding="utf-8")
    assert artifact == renderer()
