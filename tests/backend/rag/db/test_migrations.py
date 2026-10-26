from pathlib import Path

ROOT = Path(__file__).parents[4]


def test_migrations_define_fts_vector_jobs_and_active_generation() -> None:
    # Given
    migrations = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(
            (ROOT / "src/backend/rag/infra/postgres/migrations").glob("*.sql")
        )
    )

    # When / Then
    for expected in (
        "CREATE EXTENSION IF NOT EXISTS vector",
        "to_tsvector('simple'",
        "vector(1536)",
        "vector_cosine_ops",
        "enrichment_version",
        "claim_generation",
        "active_index_generation",
        "tombstoned_at",
    ):
        assert expected in migrations
